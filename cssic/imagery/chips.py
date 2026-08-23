"""Shared chip mechanics for every imagery backend.

Three things live here because every backend needs them and they are where v2
got things wrong:

* a padded bounding box derived from the *area* scale factor (centre-invariant,
  see :func:`cssic.dates.padding_scale`);
* reprojection of the AOI into the raster's CRS **before** windowing, squared
  off to the projected bounds. The v2 ``sentinel_stac._read_window`` masked a
  WGS84 AOI straight against a UTM Sentinel-2 COG, which rasterio rejects with
  "Input shapes do not overlap raster"; masking with the *reprojected outline*
  instead succeeds but leaves black nodata wedges, because a lat/lon box is a
  tilted quadrilateral in UTM;
* reflectance to uint8 (v2 cast float 0-1 arrays with ``astype(np.uint8)`` and
  wrote black PNGs) and PNG writing.

``rasterio`` is imported lazily so ``import cssic`` works without it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import box, mapping, shape

from cssic.dates import padding_scale
from cssic.imagery.base import Band

#: The CRS every AOI in this package is expressed in.
WGS84 = "EPSG:4326"


def padded_bounds(geometry: Any, padding: float = 1.0) -> tuple[float, float, float, float]:
    """Bounding box of ``geometry`` scaled about its centre by an area factor.

    ``padding=1`` returns the plain bounds; ``padding=2`` doubles the area of
    the box while keeping its centre.
    """
    minx, miny, maxx, maxy = geometry.bounds
    scale = padding_scale(padding)
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    half_w, half_h = (maxx - minx) / 2.0 * scale, (maxy - miny) / 2.0 * scale
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def padded_box(geometry: Any, padding: float = 1.0) -> Any:
    """The padded bounding box of ``geometry`` as a shapely polygon."""
    return box(*padded_bounds(geometry, padding))


def reproject_aoi(geometry: Any, src_crs: Any = WGS84, dst_crs: Any = WGS84) -> Any:
    """Reproject an AOI geometry from ``src_crs`` into the raster's CRS.

    Accepts and returns shapely geometries. A no-op when the two CRSs are equal
    (or when either is unknown), so callers may pass it unconditionally.
    """
    from rasterio.crs import CRS
    from rasterio.warp import transform_geom

    if src_crs is None or dst_crs is None:
        return geometry
    src = CRS.from_user_input(src_crs)
    dst = CRS.from_user_input(dst_crs)
    if src == dst:
        return geometry
    return shape(transform_geom(src, dst, mapping(geometry)))


def window_bounds(aoi: Any, src_crs: Any = WGS84, dst_crs: Any = WGS84) -> tuple[float, ...]:
    """The axis-aligned extent ``aoi`` covers once reprojected into ``dst_crs``.

    A lat/lon box is *not* a rectangle in a projected CRS -- meridian
    convergence tilts it by about a degree inside a UTM zone -- so the chip
    extent is the bounding box of the reprojected outline, not the outline.
    """
    return reproject_aoi(aoi, src_crs, dst_crs).bounds


def read_window(
    dataset: Any,
    aoi: Any,
    aoi_crs: Any = WGS84,
    indexes: Any = None,
) -> np.ndarray:
    """Read the ``aoi`` window out of an open rasterio dataset.

    The AOI is reprojected into ``dataset.crs`` first -- this is the fix for the
    v2 "Input shapes do not overlap raster" failure on UTM Sentinel-2 COGs --
    and then read as a plain pixel *window* rounded outward to cover the box.

    Windowing rather than masking matters. ``rasterio.mask.mask(crop=True)``
    crops to whole pixels but rasterizes the shape with centre-in-polygon
    semantics, so the outer ring of the cropped window is filled with nodata:
    every chip came out with a one-pixel black frame, which on the small
    bounding boxes this package collects is most of the image (34 of 84 pixels
    on a 12x7 Sentinel-2 chip).

    Returns a ``(bands, rows, cols)`` array; ``indexes`` is a 1-based rasterio
    band index or sequence of them. Raises ``ValueError`` when the AOI misses
    the raster entirely.
    """
    import math

    from rasterio.windows import Window

    minx, miny, maxx, maxy = window_bounds(aoi, aoi_crs, dataset.crs)
    row_start, col_start = dataset.index(minx, maxy, op=math.floor)
    row_stop, col_stop = dataset.index(maxx, miny, op=math.ceil)
    height = max(int(row_stop) - int(row_start), 1)
    width = max(int(col_stop) - int(col_start), 1)
    if (
        col_start + width <= 0
        or row_start + height <= 0
        or col_start >= dataset.width
        or row_start >= dataset.height
    ):
        raise ValueError("Input shapes do not overlap raster")

    window = Window(int(col_start), int(row_start), width, height)
    data = np.asarray(dataset.read(indexes=indexes, window=window, boundless=True, fill_value=0))
    return data[np.newaxis, ...] if data.ndim == 2 else data


#: Digital-number scale of Sentinel-2 / PlanetScope surface reflectance.
REFLECTANCE_SCALE = 10000.0


def reflectance_to_uint8(array: Any, offset: float = 0.0) -> np.ndarray:
    """Scale reflectance values to 0-255 uint8.

    Handles uint16 analytic reflectance (0-10000), float 0-1 reflectance and
    already-scaled uint8 visual chips. Ported from ``planet_helper``.

    ``offset`` is the additive digital-number offset the provider applies to
    raw bands (Sentinel-2 ``BOA_ADD_OFFSET``, see
    :data:`cssic.imagery.stac.BOA_ADD_OFFSET`); it only applies to integer
    digital numbers, never to float reflectance or an already-scaled chip.
    """
    pixels = np.asarray(array)
    if pixels.size == 0:
        return pixels.astype(np.uint8, copy=False)
    if pixels.dtype == np.uint8:
        return pixels
    values = pixels.astype(np.float64, copy=False)
    # Scale on dtype, never on the chip's own peak: a dark chip (water, shadow,
    # deep winter) whose uint16 maximum happens to fall under 255 would
    # otherwise be passed through unscaled and come out ~40x too bright.
    if np.issubdtype(pixels.dtype, np.floating):
        values = values * 255.0
    else:
        values = (values + offset) / REFLECTANCE_SCALE * 255.0
    return np.clip(np.nan_to_num(values), 0, 255).astype(np.uint8)


def to_image_array(data: Any, band: Band, offset: float = 0.0) -> np.ndarray:
    """Turn a rasterio ``(bands, rows, cols)`` read into an HxWx{3,1} uint8 chip."""
    array = np.asarray(data)
    if array.ndim == 2:
        array = array[np.newaxis, ...]
    if band == "rgb":
        if array.shape[0] < 3:
            raise ValueError(f"rgb chip needs 3 bands, got {array.shape[0]}")
        array = array[:3]
    else:
        array = array[:1]
    return reflectance_to_uint8(np.moveaxis(array, 0, -1), offset=offset)


BLANK_TOLERANCE = 8
"""Widest uint8 range that still counts as "no signal" (about 3% of 0..255).

Exact equality misses the common near-miss: a cloud that renders as 220..227 in
NIR while its RGB sibling saturates flat at 255. Ground at 10 m spans well over
a hundred levels inside any site-sized window, so a single-digit spread is
never terrain.
"""


def is_blank(array: Any, tolerance: int = BLANK_TOLERANCE) -> bool:
    """True when a chip carries no signal: every pixel sits within ``tolerance``.

    Two ways that happens, and both are worse than useless in a training set
    because they look like a valid observation of the site:

    * nodata fill. Windowing pads with zeros, so a scene that only clips the
      corner of a site comes back as a fully black PNG;
    * saturation. Solid cloud or snow over the AOI pins Sentinel-2's
      pre-rendered ``visual`` asset at 255, which is how a quarter of the RGB
      chips in a smoke run came out uniformly white. Scene-level
      ``eo:cloud_cover`` cannot see it -- a 110 km tile can be 20% cloudy and
      still be cloud over a 200 m site -- so the flat chip is the last line
      of defence behind the SCL ranking in :mod:`cssic.imagery.stac`.

    Callers pass the uint8 pixels that would be written, so ``tolerance`` is in
    PNG levels.
    """
    pixels = np.asarray(array)
    if pixels.size == 0:
        return True
    return bool(int(pixels.max()) - int(pixels.min()) <= tolerance)


def save_png(array: Any, path: str | Path) -> Path:
    """Write an HxWx3 (rgb), HxWx1 or HxW (nir) uint8 array to ``path``."""
    from PIL import Image

    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pixels = reflectance_to_uint8(array)
    if pixels.ndim == 3 and pixels.shape[-1] == 1:
        pixels = pixels[:, :, 0]
    if pixels.size == 0 or min(pixels.shape[:2]) == 0:
        raise ValueError(f"refusing to write an empty chip to {dest}")
    Image.fromarray(pixels).save(dest)
    return dest


def chip_and_save(
    dataset: Any,
    aoi: Any,
    band: Band,
    path: str | Path,
    aoi_crs: Any = WGS84,
    indexes: Any = None,
) -> Path:
    """Read, convert and write one chip; returns the written path."""
    data = read_window(dataset, aoi, aoi_crs=aoi_crs, indexes=indexes)
    return save_png(to_image_array(data, band), path)
