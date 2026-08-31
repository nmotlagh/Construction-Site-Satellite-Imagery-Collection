"""Chip mechanics: area padding, CRS reprojection, reflectance scaling, PNG output.

The reprojection tests build a real in-memory UTM GeoTIFF (EPSG:32617) and read
a WGS84 AOI out of it, which is exactly the path v2's ``sentinel_stac`` got
wrong ("Input shapes do not overlap raster").
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import transform
from shapely.geometry import box

from cssic.imagery import chips

UTM_17N = "EPSG:32617"
# A point in UTM zone 17N (central meridian -81), near Miami.
LON, LAT = -80.19, 25.77
PIXEL = 10.0
SIZE = 120


def _utm_origin() -> tuple[float, float]:
    xs, ys = transform("EPSG:4326", UTM_17N, [LON], [LAT])
    return xs[0], ys[0]


def _utm_raster(bands: int = 3, dtype: str = "uint16", fill: int = 3000) -> MemoryFile:
    """A small UTM GeoTIFF centred on ``(LON, LAT)``, held in memory."""
    cx, cy = _utm_origin()
    half = SIZE / 2 * PIXEL
    transform_ = from_origin(cx - half, cy + half, PIXEL, PIXEL)
    data = np.full((bands, SIZE, SIZE), fill, dtype=dtype)
    memfile = MemoryFile()
    with memfile.open(
        driver="GTiff",
        width=SIZE,
        height=SIZE,
        count=bands,
        dtype=dtype,
        crs=UTM_17N,
        transform=transform_,
    ) as dst:
        dst.write(data)
    return memfile


def _gradient_raster() -> MemoryFile:
    """A UTM raster whose every pixel value encodes its own ``row``/``col``.

    ``1``-based, so nodata fill (0) can never be mistaken for pixel (0, 0).
    """
    rows, cols = np.meshgrid(np.arange(SIZE), np.arange(SIZE), indexing="ij")
    data = (rows * SIZE + cols + 1).astype("uint16")[np.newaxis, ...]
    cx, cy = _utm_origin()
    half = SIZE / 2 * PIXEL
    memfile = MemoryFile()
    with memfile.open(
        driver="GTiff",
        width=SIZE,
        height=SIZE,
        count=1,
        dtype="uint16",
        crs=UTM_17N,
        transform=from_origin(cx - half, cy + half, PIXEL, PIXEL),
    ) as dst:
        dst.write(data)
    return memfile


def _rowcol(value: int) -> tuple[int, int]:
    """Decode a :func:`_gradient_raster` pixel value back to ``(row, col)``."""
    return divmod(int(value) - 1, SIZE)


@pytest.fixture()
def gradient_dataset():
    memfile = _gradient_raster()
    with memfile.open() as dataset:
        yield dataset
    memfile.close()


@pytest.fixture()
def utm_dataset():
    memfile = _utm_raster()
    with memfile.open() as dataset:
        yield dataset
    memfile.close()


@pytest.fixture()
def wgs84_aoi():
    """A small WGS84 box well inside the raster (~200 m across)."""
    return box(LON - 0.001, LAT - 0.001, LON + 0.001, LAT + 0.001)


# -- padding ---------------------------------------------------------------
def test_padded_bounds_identity():
    geom = box(0, 0, 2, 4)
    assert chips.padded_bounds(geom, 1.0) == pytest.approx(geom.bounds)


def test_padded_bounds_doubles_area_and_keeps_centre():
    geom = box(0, 0, 2, 4)
    minx, miny, maxx, maxy = chips.padded_bounds(geom, 2.0)
    area = (maxx - minx) * (maxy - miny)
    assert area == pytest.approx(geom.area * 2.0)
    assert ((minx + maxx) / 2, (miny + maxy) / 2) == pytest.approx((1.0, 2.0))


def test_padded_bounds_rejects_non_positive_padding():
    with pytest.raises(ValueError):
        chips.padded_bounds(box(0, 0, 1, 1), 0)


def test_padded_box_is_the_bounds_polygon():
    geom = box(0, 0, 2, 4)
    assert chips.padded_box(geom, 3.0).bounds == pytest.approx(chips.padded_bounds(geom, 3.0))


# -- reprojection ----------------------------------------------------------
def test_reproject_aoi_is_a_noop_for_equal_crs(wgs84_aoi):
    assert chips.reproject_aoi(wgs84_aoi, "EPSG:4326", "EPSG:4326") is wgs84_aoi


def test_reproject_aoi_moves_wgs84_into_utm_metres(wgs84_aoi):
    projected = chips.reproject_aoi(wgs84_aoi, "EPSG:4326", UTM_17N)
    cx, cy = _utm_origin()
    minx, miny, maxx, maxy = projected.bounds
    assert minx < cx < maxx and miny < cy < maxy
    # ~0.002 degrees of longitude is a couple of hundred metres, not degrees.
    assert 100 < (maxx - minx) < 400


def test_v2_bug_masking_wgs84_against_a_utm_raster_fails(utm_dataset, wgs84_aoi):
    """The v2 failure this module exists to fix."""
    from rasterio.mask import mask as rio_mask

    with pytest.raises(ValueError, match="do not overlap"):
        rio_mask(utm_dataset, [wgs84_aoi], crop=True)


def test_read_window_reads_a_wgs84_aoi_from_a_utm_raster(utm_dataset, wgs84_aoi):
    data = chips.read_window(utm_dataset, wgs84_aoi)
    assert data.ndim == 3
    assert data.shape[0] == 3
    assert min(data.shape[1:]) > 5
    assert data.max() == 3000


def test_read_window_honours_band_indexes(utm_dataset, wgs84_aoi):
    data = chips.read_window(utm_dataset, wgs84_aoi, indexes=[1])
    assert data.shape[0] == 1


def test_read_window_accepts_an_aoi_already_in_the_raster_crs(utm_dataset, wgs84_aoi):
    projected = chips.reproject_aoi(wgs84_aoi, "EPSG:4326", UTM_17N)
    same = chips.read_window(utm_dataset, projected, aoi_crs=UTM_17N)
    assert same.shape == chips.read_window(utm_dataset, wgs84_aoi).shape


def test_read_window_leaves_no_black_border_ring(utm_dataset, wgs84_aoi):
    """Windowing, not masking: no synthetic nodata frame around the chip.

    ``rasterio.mask.mask(crop=True)`` rasterises with centre-in-polygon
    semantics, so it zeroes the outer ring of the cropped window -- most of the
    image on the small boxes this package collects. Every pixel of a chip cut
    from a uniform raster must carry the fill value.
    """
    data = chips.read_window(utm_dataset, wgs84_aoi)
    assert data.min() == 3000
    for edge in (data[:, 0, :], data[:, -1, :], data[:, :, 0], data[:, :, -1]):
        assert edge.min() > 0


def test_masking_would_have_left_that_border_ring(utm_dataset, wgs84_aoi):
    """The behaviour the window read replaces, pinned so the contrast is explicit."""
    from rasterio.mask import mask as rio_mask

    projected = chips.reproject_aoi(wgs84_aoi, "EPSG:4326", UTM_17N)
    masked, _ = rio_mask(utm_dataset, [projected], crop=True)
    assert masked[:, 0, :].max() == 0
    assert masked.min() == 0


def test_read_window_rejects_an_aoi_that_misses_the_raster(utm_dataset):
    far_away = box(LON + 1.0, LAT + 1.0, LON + 1.001, LAT + 1.001)
    with pytest.raises(ValueError, match="do not overlap"):
        chips.read_window(utm_dataset, far_away)


def test_read_window_rounds_the_window_outward_to_cover_the_aoi(gradient_dataset, wgs84_aoi):
    """Floor the near corner, ceil the far one: the window must *contain* the AOI.

    Rounding the top-left the other way slides the chip a pixel off the site and
    clips the very edge the padding was there to keep.
    """
    chip = chips.read_window(gradient_dataset, wgs84_aoi, indexes=[1])
    minx, miny, maxx, maxy = chips.window_bounds(wgs84_aoi, "EPSG:4326", UTM_17N)
    left, top = gradient_dataset.xy(*_rowcol(chip[0, 0, 0]), offset="ul")
    right, bottom = gradient_dataset.xy(*_rowcol(chip[0, -1, -1]), offset="lr")

    assert left <= minx and top >= maxy
    assert right >= maxx and bottom <= miny
    # ...and no further: outward rounding is at most one pixel of slack a side.
    assert minx - left < PIXEL and top - maxy < PIXEL
    assert right - maxx < PIXEL and miny - bottom < PIXEL


def test_read_window_zero_pads_an_aoi_that_hangs_off_the_raster_edge(gradient_dataset):
    """A scene clipping the site still yields the whole chip, nodata-filled.

    Reading with ``boundless=False`` would quietly return only the overlapping
    part -- a smaller chip, mis-registered against the site, and one that
    ``is_blank`` could not recognise as a partial observation.
    """
    straddling = box(LON - 0.009, LAT - 0.001, LON - 0.005, LAT + 0.001)
    chip = chips.read_window(gradient_dataset, straddling, indexes=[1])

    minx, _, maxx, _ = chips.window_bounds(straddling, "EPSG:4326", UTM_17N)
    assert chip.shape[2] * PIXEL == pytest.approx(maxx - minx, abs=2 * PIXEL)
    assert chip[0, :, 0].max() == 0  # west of the raster: fill_value=0
    assert chip[0, :, -1].min() > 0  # inside it: real pixels


def test_read_window_keeps_bands_first_for_a_scalar_index(utm_dataset, wgs84_aoi):
    """A scalar ``indexes`` still yields ``(bands, rows, cols)``, not ``(rows, cols, 1)``."""
    scalar = chips.read_window(utm_dataset, wgs84_aoi, indexes=1)
    three = chips.read_window(utm_dataset, wgs84_aoi)
    assert scalar.shape == (1, *three.shape[1:])


# -- reflectance scaling ---------------------------------------------------
def test_reflectance_uint8_passes_through():
    array = np.array([[0, 128, 255]], dtype=np.uint8)
    assert chips.reflectance_to_uint8(array) is array


def test_reflectance_float_0_1_is_not_floored_to_black():
    """v2 did ``astype(np.uint8)`` here and wrote black PNGs."""
    array = np.array([[0.0, 0.5, 1.0]], dtype=np.float32)
    assert chips.reflectance_to_uint8(array).tolist() == [[0, 127, 255]]


def test_reflectance_uint16_analytic_is_scaled_from_10000():
    array = np.array([[0, 5000, 10000]], dtype=np.uint16)
    assert chips.reflectance_to_uint8(array).tolist() == [[0, 127, 255]]


def test_reflectance_handles_empty_and_nan():
    assert chips.reflectance_to_uint8(np.array([], dtype=np.float32)).dtype == np.uint8
    out = chips.reflectance_to_uint8(np.array([[np.nan, 0.5]], dtype=np.float32))
    assert out.tolist() == [[0, 127]]


# -- array shaping and PNG -------------------------------------------------
def test_to_image_array_rgb_is_hwc():
    data = np.zeros((4, 6, 7), dtype=np.uint8)
    assert chips.to_image_array(data, "rgb").shape == (6, 7, 3)


def test_to_image_array_nir_keeps_one_band():
    data = np.zeros((3, 6, 7), dtype=np.uint8)
    assert chips.to_image_array(data, "nir").shape == (6, 7, 1)


def test_to_image_array_nir_takes_the_first_band_not_the_last():
    """NIR reads ask for one band, but a fallback asset can arrive multi-band.

    The requested plane is always the first one; taking the last would silently
    hand back whatever the provider stacked at the end (an alpha mask, SWIR).
    """
    data = np.stack([np.full((2, 3), value, dtype="uint16") for value in (8000, 10, 20, 30)])
    chip = chips.to_image_array(data, "nir")
    assert chip.shape == (2, 3, 1)
    assert int(chip[0, 0, 0]) == 204  # 8000 / 10000 * 255, i.e. band 0


def test_to_image_array_rgb_needs_three_bands():
    with pytest.raises(ValueError, match="3 bands"):
        chips.to_image_array(np.zeros((2, 4, 4), dtype=np.uint8), "rgb")


def test_save_png_writes_rgb(tmp_path):
    array = np.zeros((4, 5, 3), dtype=np.uint8)
    path = chips.save_png(array, tmp_path / "nested" / "2020-01-01.png")
    assert path.is_file() and path.stat().st_size > 0
    with Image.open(path) as img:
        assert img.size == (5, 4) and img.mode == "RGB"


def test_save_png_squeezes_single_band_nir(tmp_path):
    path = chips.save_png(np.zeros((4, 5, 1), dtype=np.uint8), tmp_path / "nir.png")
    with Image.open(path) as img:
        assert img.mode == "L" and img.size == (5, 4)


def test_save_png_refuses_an_empty_chip(tmp_path):
    with pytest.raises(ValueError, match="empty chip"):
        chips.save_png(np.zeros((0, 5, 3), dtype=np.uint8), tmp_path / "empty.png")


def test_chip_and_save_round_trip(tmp_path, utm_dataset, wgs84_aoi):
    path = chips.chip_and_save(utm_dataset, wgs84_aoi, "rgb", tmp_path / "2020-05-01.png")
    assert path.is_file()
    with Image.open(path) as img:
        assert img.mode == "RGB"
        assert np.asarray(img).max() == 76  # 3000 / 10000 * 255


def test_chip_and_save_nir_from_a_single_band_raster(tmp_path, wgs84_aoi):
    memfile = _utm_raster(bands=1, fill=8000)
    with memfile.open() as dataset:
        path = chips.chip_and_save(dataset, wgs84_aoi, "nir", tmp_path / "nir.png")
    memfile.close()
    with Image.open(path) as img:
        assert img.mode == "L"
        assert np.asarray(img).max() == 204  # 8000 / 10000 * 255


def test_raster_fixture_is_really_utm(utm_dataset):
    assert rasterio.crs.CRS.from_user_input(UTM_17N) == utm_dataset.crs


# --- provider digital-number offset ----------------------------------------


def test_the_offset_is_applied_to_integer_digital_numbers():
    """Sentinel-2 baseline 04.00: 3000 DN with a -1000 offset is 0.2 reflectance."""
    chip = chips.reflectance_to_uint8(np.array([[3000]], dtype="uint16"), offset=-1000.0)
    assert int(chip[0, 0]) == 51


def test_the_offset_never_touches_float_reflectance():
    """Float arrays are already reflectance; the offset is in DN units."""
    chip = chips.reflectance_to_uint8(np.array([[0.3]], dtype="float32"), offset=-1000.0)
    assert int(chip[0, 0]) == 76


def test_the_offset_never_touches_an_already_scaled_chip():
    chip = chips.reflectance_to_uint8(np.array([[200]], dtype="uint8"), offset=-1000.0)
    assert int(chip[0, 0]) == 200


def test_the_offset_cannot_push_a_dark_pixel_below_zero():
    chip = chips.reflectance_to_uint8(np.array([[500]], dtype="uint16"), offset=-1000.0)
    assert int(chip[0, 0]) == 0


def test_to_image_array_forwards_the_offset():
    data = np.full((3, 2, 2), 3000, dtype="uint16")
    assert int(chips.to_image_array(data, "rgb", offset=-1000.0)[0, 0, 0]) == 51
    assert int(chips.to_image_array(data, "rgb")[0, 0, 0]) == 76


# --- all-nodata chips -------------------------------------------------------


def test_an_all_zero_chip_is_blank():
    """Windowing pads with zeros, so a corner-clipping scene comes back black."""
    assert chips.is_blank(np.zeros((4, 4, 3), dtype="uint8")) is True


def test_a_chip_with_any_signal_is_not_blank():
    pixels = np.zeros((4, 4, 3), dtype="uint8")
    pixels[2, 2, 1] = 40
    assert chips.is_blank(pixels) is False


def test_an_empty_chip_is_blank():
    assert chips.is_blank(np.zeros((0, 4, 3), dtype="uint8")) is True


def test_a_saturated_all_white_chip_is_blank():
    """Solid cloud or snow pins the pre-rendered ``visual`` asset at 255.

    17 of the 69 RGB chips in a smoke run looked like this. Scene-level
    ``eo:cloud_cover`` cannot see it, so the constant chip itself is the signal.
    """
    assert chips.is_blank(np.full((6, 6, 3), 255, dtype="uint8")) is True


def test_a_chip_constant_at_any_other_value_is_blank():
    assert chips.is_blank(np.full((6, 6), 137, dtype="uint8")) is True


def test_a_chip_spread_beyond_the_tolerance_is_not_blank():
    pixels = np.full((6, 6, 3), 255, dtype="uint8")
    pixels[3, 3, 2] = 255 - chips.BLANK_TOLERANCE - 1
    assert chips.is_blank(pixels) is False


def test_a_near_flat_cloud_chip_is_blank():
    """Live OSU run: a cloud rendered as 220..227 in NIR while RGB sat at 255."""
    rng = np.random.default_rng(0)
    pixels = rng.integers(220, 228, size=(54, 44), dtype="uint8")
    assert chips.is_blank(pixels) is True
    assert chips.is_blank(pixels, tolerance=0) is False


def test_save_png_preserves_existing_chip_when_encoding_fails(tmp_path, monkeypatch):
    pixels = np.arange(60, dtype="uint8").reshape(4, 5, 3)
    replacement = np.flip(pixels, axis=0).copy()
    path = chips.save_png(pixels, tmp_path / "2020-01-01.png")
    path.chmod(0o640)
    previous = path.read_bytes()
    previous_mode = path.stat().st_mode

    def fail_encoder(image, stream, filename, **kwargs):
        stream.write(b"partial PNG")
        raise OSError("synthetic encoder write failure")

    with monkeypatch.context() as patch:
        patch.setitem(Image.SAVE, "PNG", fail_encoder)
        with pytest.raises(OSError, match="synthetic encoder write failure"):
            chips.save_png(replacement, path)

    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]
    chips.save_png(replacement, path)
    with Image.open(path) as image:
        np.testing.assert_array_equal(np.asarray(image), replacement)
    assert path.stat().st_mode == previous_mode
