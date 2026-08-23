"""Planetary Computer STAC source: search, window sampling and chip fetching.

No network: a fake ``pystac_client.Client`` is injected, and asset hrefs point
at real UTM GeoTIFFs on disk so ``fetch`` exercises the reprojection path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import pytest
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import transform
from shapely.geometry import box

from cssic.imagery import get_image_source
from cssic.imagery.stac import (
    BOA_ADD_OFFSET,
    SCL_CANDIDATES,
    SENTINEL_CUTOFF,
    STACSentinelSource,
    boa_offset,
    covers_aoi,
    least_cloudy,
    scene_from_item,
    scl_cloud_fraction,
)

UTM_17N = "EPSG:32617"
LON, LAT = -80.19, 25.77
AOI = box(LON - 0.001, LAT - 0.001, LON + 0.001, LAT + 0.001)


# -- fakes -----------------------------------------------------------------
@dataclass
class FakeAsset:
    href: str


@dataclass
class FakeItem:
    id: str
    datetime: datetime
    properties: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, FakeAsset] = field(default_factory=dict)
    collection_id: str = "sentinel-2-l2a"
    geometry: dict[str, Any] | None = None


def make_item(item_id: str, day: str, cloud: float | None = None, **assets: str) -> FakeItem:
    props: dict[str, Any] = {"datetime": f"{day}T10:00:00Z"}
    if cloud is not None:
        props["eo:cloud_cover"] = cloud
    return FakeItem(
        id=item_id,
        datetime=datetime.fromisoformat(f"{day}T10:00:00+00:00"),
        properties=props,
        assets={name: FakeAsset(href) for name, href in assets.items()},
    )


class FakeSearch:
    def __init__(self, items):
        self._items = items

    def items(self):
        return iter(self._items)


class FakeClient:
    """Records search kwargs and returns items whose date falls in the window."""

    def __init__(self, items):
        self.items_ = list(items)
        self.searches: list[dict[str, Any]] = []

    def search(self, **kwargs):
        self.searches.append(kwargs)
        start, _, end = kwargs["datetime"].partition("/")
        lo, hi = date.fromisoformat(start), date.fromisoformat(end)
        selected = [item for item in self.items_ if lo <= item.datetime.date() <= hi]
        limit = kwargs.get("max_items")
        return FakeSearch(selected[:limit] if limit else selected)


def write_utm_tif(path, bands: int = 1, fill: int = 3000, values=None):
    import rasterio

    size, pixel = 120, 10.0
    xs, ys = transform("EPSG:4326", UTM_17N, [LON], [LAT])
    half = size / 2 * pixel
    tf = from_origin(xs[0] - half, ys[0] + half, pixel, pixel)
    data = np.full((bands, size, size), fill, dtype="uint16")
    if values is not None:
        for i, value in enumerate(values):
            data[i] = value
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=size,
        height=size,
        count=bands,
        dtype="uint16",
        crs=UTM_17N,
        transform=tf,
    ) as dst:
        dst.write(data)
    return str(path)


# -- Scene mapping ---------------------------------------------------------
def test_scene_from_item_reads_date_and_cloud():
    scene = scene_from_item(make_item("a", "2020-03-04", cloud=12.5))
    assert scene.id == "a"
    assert scene.acquired == date(2020, 3, 4)
    assert scene.source == "sentinel"
    assert scene.cloud_cover == 12.5
    assert scene.metadata["item"].id == "a"


def test_scene_from_item_without_cloud_cover():
    assert scene_from_item(make_item("a", "2020-03-04")).cloud_cover is None


def test_scene_from_item_falls_back_to_the_properties_datetime():
    item = make_item("a", "2020-03-04")
    item.datetime = None
    assert scene_from_item(item).acquired == date(2020, 3, 4)


def test_scene_from_item_without_any_date_raises():
    item = make_item("a", "2020-03-04")
    item.datetime = None
    item.properties = {}
    with pytest.raises(ValueError, match="no acquisition date"):
        scene_from_item(item)


def test_least_cloudy_prefers_the_lowest_cover():
    scenes = [
        scene_from_item(make_item("a", "2020-03-01", cloud=40)),
        scene_from_item(make_item("b", "2020-03-02", cloud=3)),
        scene_from_item(make_item("c", "2020-03-03", cloud=17)),
    ]
    assert least_cloudy(scenes).id == "b"


def test_least_cloudy_of_nothing_is_none():
    assert least_cloudy([]) is None


def test_least_cloudy_falls_back_to_the_earliest_when_cover_is_unknown():
    scenes = [
        scene_from_item(make_item("b", "2020-03-05")),
        scene_from_item(make_item("a", "2020-03-01")),
    ]
    assert least_cloudy(scenes).id == "a"


# -- search ----------------------------------------------------------------
def test_find_scenes_returns_scenes_in_date_order():
    client = FakeClient(
        [make_item("b", "2020-03-09", cloud=5), make_item("a", "2020-03-02", cloud=9)]
    )
    source = STACSentinelSource(client=client)
    scenes = source.find_scenes(AOI, date(2020, 3, 1), date(2020, 3, 31))
    assert [scene.id for scene in scenes] == ["a", "b"]


def test_find_scenes_passes_collection_and_intersects():
    client = FakeClient([])
    STACSentinelSource(client=client, collection="my-collection").find_scenes(
        AOI, date(2020, 3, 1), date(2020, 3, 31)
    )
    kwargs = client.searches[0]
    assert kwargs["collections"] == ["my-collection"]
    assert kwargs["intersects"]["type"] == "Polygon"
    assert kwargs["datetime"] == "2020-03-01/2020-03-31"
    assert kwargs["query"] is None


def test_max_cloud_cover_becomes_a_stac_query():
    client = FakeClient([])
    STACSentinelSource(client=client, max_cloud_cover=40).find_scenes(
        AOI, date(2020, 3, 1), date(2020, 3, 31)
    )
    assert client.searches[0]["query"] == {"eo:cloud_cover": {"lt": 40}}


def test_find_scenes_before_the_sentinel_cutoff_is_empty():
    client = FakeClient([make_item("a", "2014-01-02")])
    scenes = STACSentinelSource(client=client).find_scenes(AOI, date(2014, 1, 1), date(2014, 6, 1))
    assert scenes == []
    assert client.searches == []


def test_find_scenes_clamps_a_start_before_the_cutoff():
    client = FakeClient([])
    STACSentinelSource(client=client).find_scenes(AOI, date(2014, 1, 1), date(2016, 1, 1))
    assert client.searches[0]["datetime"].startswith(SENTINEL_CUTOFF.isoformat())


# -- window sampling -------------------------------------------------------
def test_sample_scenes_takes_the_least_cloudy_per_window():
    client = FakeClient(
        [
            make_item("a1", "2020-03-01", cloud=60),
            make_item("a2", "2020-03-02", cloud=4),
            make_item("b1", "2020-06-01", cloud=30),
            make_item("b2", "2020-06-02", cloud=80),
        ]
    )
    source = STACSentinelSource(client=client)
    windows = [(date(2020, 3, 1), date(2020, 3, 6)), (date(2020, 6, 1), date(2020, 6, 6))]
    assert [scene.id for scene in source.sample_scenes(AOI, windows)] == ["a2", "b1"]


def test_sample_scenes_deduplicates_overlapping_windows():
    client = FakeClient([make_item("a", "2020-03-02", cloud=4)])
    source = STACSentinelSource(client=client)
    windows = [(date(2020, 3, 1), date(2020, 3, 6)), (date(2020, 3, 1), date(2020, 3, 6))]
    assert [scene.id for scene in source.sample_scenes(AOI, windows)] == ["a"]


def test_sample_scenes_with_all_dates_keeps_every_acquisition():
    """``-1`` images: one spanning window, every acquisition in it."""
    client = FakeClient(
        [
            make_item("a", "2020-03-02", cloud=60),
            make_item("b", "2020-04-02", cloud=4),
            make_item("c", "2020-05-02", cloud=20),
        ]
    )
    source = STACSentinelSource(client=client)
    window = [(date(2020, 3, 1), date(2020, 6, 1))]
    scenes = source.sample_scenes(AOI, window, all_dates=True)
    assert [scene.id for scene in scenes] == ["a", "b", "c"]


def test_sample_scenes_without_all_dates_keeps_one_per_window():
    """``-n 1`` also asks for a single window, and must yield a single image."""
    client = FakeClient(
        [
            make_item("a", "2020-03-02", cloud=60),
            make_item("b", "2020-04-02", cloud=4),
            make_item("c", "2020-05-02", cloud=20),
        ]
    )
    source = STACSentinelSource(client=client)
    scenes = source.sample_scenes(AOI, [(date(2020, 3, 1), date(2020, 6, 1))])
    assert [scene.id for scene in scenes] == ["b"]


def test_sample_scenes_with_all_dates_keeps_one_scene_per_date():
    client = FakeClient(
        [
            make_item("a", "2020-03-02", cloud=60),
            make_item("a-twin", "2020-03-02", cloud=4),
            make_item("b", "2020-04-02", cloud=4),
        ]
    )
    source = STACSentinelSource(client=client)
    window = [(date(2020, 3, 1), date(2020, 6, 1))]
    scenes = source.sample_scenes(AOI, window, all_dates=True)
    # The less cloudy of the two same-day products wins, not the first listed.
    assert [scene.id for scene in scenes] == ["a-twin", "b"]


def test_sample_scenes_skips_empty_windows():
    client = FakeClient([make_item("a", "2020-03-02", cloud=4)])
    source = STACSentinelSource(client=client)
    windows = [(date(2020, 1, 1), date(2020, 1, 6)), (date(2020, 3, 1), date(2020, 3, 6))]
    assert [scene.id for scene in source.sample_scenes(AOI, windows)] == ["a"]


# -- fetch -----------------------------------------------------------------
def test_fetch_rgb_from_a_visual_asset(tmp_path):
    href = write_utm_tif(tmp_path / "visual.tif", bands=3, values=[1000, 2000, 3000])
    scene = scene_from_item(make_item("a", "2020-03-02", cloud=1, visual=href))
    array = STACSentinelSource(client=FakeClient([])).fetch(scene, AOI, "rgb")
    assert array.ndim == 3 and array.shape[-1] == 3
    assert array.dtype == np.uint8
    assert [int(array[..., i].max()) for i in range(3)] == [25, 51, 76]


def test_fetch_rgb_stacks_b04_b03_b02_when_there_is_no_visual(tmp_path):
    item = make_item(
        "a",
        "2020-03-02",
        cloud=1,
        B04=write_utm_tif(tmp_path / "b04.tif", fill=1000),
        B03=write_utm_tif(tmp_path / "b03.tif", fill=2000),
        B02=write_utm_tif(tmp_path / "b02.tif", fill=3000),
    )
    array = STACSentinelSource(client=FakeClient([])).fetch(scene_from_item(item), AOI, "rgb")
    assert array.shape[-1] == 3
    assert [int(array[..., i].max()) for i in range(3)] == [25, 51, 76]


def test_fetch_rgb_without_any_usable_asset_raises(tmp_path):
    scene = scene_from_item(make_item("a", "2020-03-02"))
    with pytest.raises(ValueError, match="no B04 or visual asset"):
        STACSentinelSource(client=FakeClient([])).fetch(scene, AOI, "rgb")


def test_fetch_nir_from_b08(tmp_path):
    href = write_utm_tif(tmp_path / "b08.tif", fill=8000)
    scene = scene_from_item(make_item("a", "2020-03-02", B08=href))
    array = STACSentinelSource(client=FakeClient([])).fetch(scene, AOI, "nir")
    assert array.shape[-1] == 1
    assert int(array.max()) == 204


def test_fetch_nir_without_an_asset_raises():
    scene = scene_from_item(make_item("a", "2020-03-02"))
    with pytest.raises(ValueError, match="no NIR asset"):
        STACSentinelSource(client=FakeClient([])).fetch(scene, AOI, "nir")


def test_fetched_chip_writes_a_non_empty_png(tmp_path):
    from cssic.imagery.chips import save_png

    href = write_utm_tif(tmp_path / "visual.tif", bands=3, values=[1000, 2000, 3000])
    scene = scene_from_item(make_item("a", "2020-03-02", visual=href))
    array = STACSentinelSource(client=FakeClient([])).fetch(scene, AOI, "rgb")
    path = save_png(array, tmp_path / "2020-03-02.png")
    assert path.stat().st_size > 0
    with Image.open(path) as img:
        assert img.mode == "RGB" and min(img.size) > 5


# -- wiring ----------------------------------------------------------------
def test_get_image_source_builds_the_stac_backend():
    source = get_image_source("stac", client=FakeClient([]))
    assert isinstance(source, STACSentinelSource)


def test_the_default_client_is_lazy():
    """Constructing the source must not touch the network."""
    source = STACSentinelSource()
    assert source._client is None


def test_utc_datetimes_survive_the_scene_mapping():
    item = make_item("a", "2020-03-04")
    assert item.datetime.tzinfo is timezone.utc
    assert scene_from_item(item).acquired == date(2020, 3, 4)


# -- processing baseline 04.00 offset ---------------------------------------


def baselined(item_id: str, baseline: Any) -> FakeItem:
    item = make_item(item_id, "2022-03-04")
    item.properties["s2:processing_baseline"] = baseline
    return item


def test_a_post_2022_item_carries_the_boa_offset():
    """Baseline 04.00 shifted L2A digital numbers; v2 predates it."""
    assert boa_offset(baselined("a", "04.00")) == BOA_ADD_OFFSET
    assert boa_offset(baselined("b", "05.09")) == BOA_ADD_OFFSET


def test_a_pre_baseline_item_has_no_offset():
    assert boa_offset(baselined("a", "03.01")) == 0.0


def test_an_unknown_baseline_has_no_offset():
    assert boa_offset(baselined("a", None)) == 0.0
    assert boa_offset(baselined("a", "n/a")) == 0.0
    assert boa_offset(make_item("a", "2018-03-04")) == 0.0


def test_the_offset_travels_with_the_scene():
    assert scene_from_item(baselined("a", "05.00")).metadata["offset"] == BOA_ADD_OFFSET


def test_raw_bands_are_offset_corrected(tmp_path):
    """3000 DN on baseline 05.00 is 0.2 reflectance, not 0.3."""
    href = write_utm_tif(tmp_path / "b.tif", fill=3000)
    item = baselined("a", "05.00")
    item.assets = {name: FakeAsset(href) for name in ("B04", "B03", "B02")}
    source = STACSentinelSource(client=FakeClient([]))

    chip = source.fetch(scene_from_item(item), AOI, "rgb")

    assert int(chip[0, 0, 0]) == 51  # (3000 - 1000) / 10000 * 255
    assert int(np.max(chip)) == 51


def test_the_visual_asset_is_left_alone(tmp_path):
    """The pre-rendered ``visual`` asset already has the offset applied."""
    href = write_utm_tif(tmp_path / "visual.tif", bands=3, fill=3000)
    item = baselined("a", "05.00")
    item.assets = {"visual": FakeAsset(href)}
    source = STACSentinelSource(client=FakeClient([]))

    chip = source.fetch(scene_from_item(item), AOI, "rgb")

    assert int(chip[0, 0, 0]) == 76  # 3000 / 10000 * 255, uncorrected


def test_a_pre_baseline_scene_is_not_shifted(tmp_path):
    href = write_utm_tif(tmp_path / "b.tif", fill=3000)
    item = baselined("a", "03.01")
    item.assets = {"B08": FakeAsset(href)}
    source = STACSentinelSource(client=FakeClient([]))

    chip = source.fetch(scene_from_item(item), AOI, "nir")

    assert int(chip[0, 0, 0]) == 76


# -- partial scenes ---------------------------------------------------------


def with_footprint(item_id: str, day: str, footprint, cloud: float | None = None) -> FakeItem:
    from shapely.geometry import mapping

    item = make_item(item_id, day, cloud=cloud)
    item.geometry = mapping(footprint)
    return item


FULL = box(LON - 1, LAT - 1, LON + 1, LAT + 1)
CLIPPED = box(LON, LAT, LON + 1, LAT + 1)  # covers a corner of the AOI only


def test_covers_aoi_recognises_a_full_footprint():
    assert covers_aoi(with_footprint("a", "2020-03-04", FULL), AOI) is True


def test_covers_aoi_rejects_a_corner_clip():
    assert covers_aoi(with_footprint("a", "2020-03-04", CLIPPED), AOI) is False


def test_covers_aoi_of_an_item_without_geometry_is_false():
    assert covers_aoi(make_item("a", "2020-03-04"), AOI) is False
    assert covers_aoi(object(), AOI) is False


def test_find_scenes_prefers_a_scene_that_covers_the_whole_site():
    """The part of a chip outside the scene comes back as black nodata fill."""
    client = FakeClient(
        [
            with_footprint("clipped", "2020-03-04", CLIPPED, cloud=0.0),
            with_footprint("full", "2020-03-05", FULL, cloud=90.0),
        ]
    )
    source = STACSentinelSource(client=client)

    scenes = source.find_scenes(AOI, date(2020, 3, 1), date(2020, 3, 10))

    assert [scene.id for scene in scenes] == ["full"]


def test_find_scenes_keeps_partial_scenes_when_nothing_covers_the_site():
    """Half a chip beats no chip at all."""
    client = FakeClient([with_footprint("clipped", "2020-03-04", CLIPPED)])
    source = STACSentinelSource(client=client)

    scenes = source.find_scenes(AOI, date(2020, 3, 1), date(2020, 3, 10))

    assert [scene.id for scene in scenes] == ["clipped"]


# -- cloud over the site (SCL) ----------------------------------------------
#: Scene-classification codes used below: 4 vegetation, 5 bare soil (both
#: usable), 3 cloud shadow, 9 cloud high probability, 10 cirrus, 11 snow.
CLEAR_SCL, CLOUDY_SCL = 4, 9


def write_scl_tif(path, rows) -> str:
    """A single-band SCL raster whose rows repeat the classes in ``rows``."""
    import rasterio

    size, pixel = 60, 20.0  # SCL is 20 m
    xs, ys = transform("EPSG:4326", UTM_17N, [LON], [LAT])
    half = size / 2 * pixel
    data = np.empty((1, size, size), dtype="uint8")
    for row in range(size):
        data[0, row, :] = rows[row % len(rows)]
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=size,
        height=size,
        count=1,
        dtype="uint8",
        crs=UTM_17N,
        transform=from_origin(xs[0] - half, ys[0] + half, pixel, pixel),
    ) as dst:
        dst.write(data)
    return str(path)


def scl_item(item_id: str, day: str, cloud: float, scl: str | None, **assets: str) -> FakeItem:
    item = make_item(item_id, day, cloud=cloud, **assets)
    if scl is not None:
        item.assets["SCL"] = FakeAsset(scl)
    return item


def test_scl_cloud_fraction_counts_cloud_shadow_cloud_cirrus_and_snow():
    """3, 8, 9, 10 and 11 all hide the site; 4/5 (vegetation, bare soil) do not."""
    assert scl_cloud_fraction(np.array([[3, 8, 9, 10, 11]])) == 1.0
    assert scl_cloud_fraction(np.array([[4, 5, 6, 7]])) == 0.0
    assert scl_cloud_fraction(np.array([[4, 9]])) == 0.5


def test_scl_cloud_fraction_of_an_empty_window_is_none():
    assert scl_cloud_fraction(np.array([], dtype="uint8")) is None


def test_scl_cloud_fraction_ignores_unobserved_pixels():
    """Class 0 is nodata: a window that is three quarters unobserved and one
    quarter cloud is fully cloudy where it was seen, not 25% cloudy."""
    assert scl_cloud_fraction(np.array([[0, 0, 0, 9]])) == 1.0
    assert scl_cloud_fraction(np.array([[0, 0, 4, 9]])) == 0.5
    assert scl_cloud_fraction(np.array([[0, 0]])) is None


def test_aoi_cloud_fraction_reads_the_scl_asset(tmp_path):
    half = write_scl_tif(tmp_path / "scl.tif", (CLEAR_SCL, CLOUDY_SCL))
    scene = scene_from_item(scl_item("a", "2020-03-02", 5.0, half))
    source = STACSentinelSource(client=FakeClient([]))
    assert source.aoi_cloud_fraction(scene, AOI) == pytest.approx(0.5, abs=0.15)


def test_aoi_cloud_fraction_without_an_scl_asset_is_none():
    scene = scene_from_item(scl_item("a", "2020-03-02", 5.0, None))
    assert STACSentinelSource(client=FakeClient([])).aoi_cloud_fraction(scene, AOI) is None


def test_sample_scenes_prefers_the_scene_whose_site_is_clear(tmp_path):
    """The finding: a 21%-cloudy tile can still be solid cloud over the site.

    17 of 69 RGB chips in a smoke run came back constant 255 because the window
    was picked on ``eo:cloud_cover`` alone, so the AOI itself was never checked.
    """
    cloudy = write_scl_tif(tmp_path / "cloudy.tif", (CLOUDY_SCL,))
    clear = write_scl_tif(tmp_path / "clear.tif", (CLEAR_SCL,))
    client = FakeClient(
        [
            scl_item("cloudy-site", "2020-03-02", 21.0, cloudy),
            scl_item("clear-site", "2020-03-03", 60.0, clear),
        ]
    )
    source = STACSentinelSource(client=client)

    scenes = source.sample_scenes(AOI, [(date(2020, 3, 1), date(2020, 3, 6))])

    assert [scene.id for scene in scenes] == ["clear-site"]


def test_clearest_over_aoi_falls_back_to_scene_cloud_without_an_scl_asset():
    scenes = [
        scene_from_item(scl_item("a", "2020-03-02", 40.0, None)),
        scene_from_item(scl_item("b", "2020-03-03", 3.0, None)),
    ]
    assert STACSentinelSource(client=FakeClient([])).clearest_over_aoi(AOI, scenes).id == "b"


def test_clearest_over_aoi_of_nothing_is_none():
    assert STACSentinelSource(client=FakeClient([])).clearest_over_aoi(AOI, []) is None


class CountingSource(STACSentinelSource):
    """Records every SCL ranking so the cost of a busy window can be pinned."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.ranked: list[str] = []

    def aoi_cloud_fraction(self, scene, aoi):
        self.ranked.append(scene.id)
        return super().aoi_cloud_fraction(scene, aoi)


def test_clearest_over_aoi_does_not_read_scl_for_a_single_candidate(tmp_path):
    """Most sampled windows offer one acquisition; ranking it changes nothing."""
    cloudy = write_scl_tif(tmp_path / "cloudy.tif", (CLOUDY_SCL,))
    only = scene_from_item(scl_item("only", "2020-03-02", 90.0, cloudy))
    source = CountingSource(client=FakeClient([]))

    assert source.clearest_over_aoi(AOI, [only]).id == "only"
    assert source.ranked == []


def test_clearest_over_aoi_ranks_only_the_least_cloudy_candidates(tmp_path):
    """One extra range request per candidate, so a busy window must be capped."""
    cloudy = write_scl_tif(tmp_path / "cloudy.tif", (CLOUDY_SCL,))
    scenes = [
        scene_from_item(scl_item(f"s{index}", f"2020-03-{2 + index:02d}", float(index), cloudy))
        for index in range(SCL_CANDIDATES + 3)
    ]
    source = CountingSource(client=FakeClient([]))

    source.clearest_over_aoi(AOI, reversed(scenes))

    assert source.ranked == [f"s{index}" for index in range(SCL_CANDIDATES)]


def test_clearest_over_aoi_ranks_one_product_per_acquisition(tmp_path):
    """A reprocessed duplicate of the same overpass must not take a slot from a
    different acquisition, nor cost a second SCL read."""
    cloudy = write_scl_tif(tmp_path / "cloudy.tif", (CLOUDY_SCL,))
    scenes = [
        scene_from_item(scl_item("day1-old", "2020-03-02", 10.0, cloudy)),
        scene_from_item(scl_item("day1-new", "2020-03-02", 9.0, cloudy)),
        scene_from_item(scl_item("day2", "2020-03-07", 50.0, cloudy)),
    ]
    source = CountingSource(client=FakeClient([]))

    source.clearest_over_aoi(AOI, scenes)

    assert source.ranked == ["day1-new", "day2"]


def test_clearest_over_aoi_stops_at_the_first_completely_clear_site(tmp_path):
    clear = write_scl_tif(tmp_path / "clear.tif", (CLEAR_SCL,))
    cloudy = write_scl_tif(tmp_path / "cloudy.tif", (CLOUDY_SCL,))
    scenes = [
        scene_from_item(scl_item("first", "2020-03-02", 1.0, clear)),
        scene_from_item(scl_item("second", "2020-03-03", 2.0, cloudy)),
    ]
    source = CountingSource(client=FakeClient([]))

    assert source.clearest_over_aoi(AOI, scenes).id == "first"
    assert source.ranked == ["first"]
