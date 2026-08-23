"""Sentinel Hub Process API backend.

Every test mocks at the client boundary: a fake catalog (``.search``) and a
fake Process API request factory are injected into
:class:`~cssic.imagery.sentinelhub.SentinelHubSource`. No network, no
credentials, and no ``unittest.mock.patch`` on module internals.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest
from shapely.geometry import box

from cssic.config import Credentials
from cssic.imagery.base import ImageSource, Scene
from cssic.imagery.sentinelhub import (
    CDSE_BASE,
    CDSE_TOKEN,
    NIR_EVALSCRIPT,
    SENTINEL_CUTOFF,
    TRUE_COLOR_EVALSCRIPT,
    SentinelHubSource,
    build_config,
    sentinel_collection,
)

AOI = box(-83.01, 39.99, -83.0, 40.0)


def creds(provider: str = "cdse") -> Credentials:
    return Credentials(
        sh_client_id="test-id",
        sh_client_secret="test-secret",
        sentinel_provider=provider,
    )


class FakeConfig:
    """Stands in for ``sentinelhub.SHConfig``."""

    def __init__(self, base_url: str = "https://services.sentinel-hub.com") -> None:
        self.sh_client_id = None
        self.sh_client_secret = None
        self.sh_base_url = base_url
        self.sh_token_url = None


class FakeCatalog:
    """Stands in for ``sentinelhub.SentinelHubCatalog``."""

    def __init__(self, items):
        self.items = items
        self.calls = []

    def search(self, collection, *, bbox=None, time=None, **kwargs):
        self.calls.append({"collection": collection, "bbox": bbox, "time": time})
        return iter(self.items)


class FakeRequest:
    def __init__(self, data):
        self.data = data

    def get_data(self):
        return self.data


class FakeRequestFactory:
    """Stands in for building a ``sentinelhub.SentinelHubRequest``."""

    def __init__(self, data):
        self.data = data
        self.calls = []

    def __call__(self, *, bbox, size, time_interval, evalscript, mime):
        self.calls.append(
            {
                "bbox": bbox,
                "size": size,
                "time_interval": time_interval,
                "evalscript": evalscript,
                "mime": mime,
            }
        )
        return FakeRequest(self.data)


def item(item_id, stamp, cloud=None):
    properties = {"datetime": stamp}
    if cloud is not None:
        properties["eo:cloud_cover"] = cloud
    return {"id": item_id, "properties": properties}


def source(items=(), data=None, **kwargs):
    catalog = FakeCatalog(list(items))
    factory = FakeRequestFactory(data)
    src = SentinelHubSource(
        creds(),
        FakeConfig(),
        collection="s2l2a",
        catalog=catalog,
        request_factory=factory,
        **kwargs,
    )
    return src, catalog, factory


# -- configuration ---------------------------------------------------------


def test_build_config_points_cdse_at_the_dataspace_endpoints():
    config = build_config(creds("cdse"), FakeConfig())
    assert config.sh_client_id == "test-id"
    assert config.sh_client_secret == "test-secret"
    assert config.sh_base_url == CDSE_BASE
    assert config.sh_token_url == CDSE_TOKEN


def test_build_config_leaves_commercial_endpoints_alone():
    config = build_config(creds("sentinelhub"), FakeConfig())
    assert config.sh_base_url == "https://services.sentinel-hub.com"
    assert config.sh_token_url is None


def test_build_config_requires_credentials():
    with pytest.raises(ValueError, match="SH_CLIENT_ID"):
        build_config(Credentials(), FakeConfig())
    with pytest.raises(ValueError):
        build_config(Credentials(sh_client_id="only-id"), FakeConfig())


def test_sentinel_collection_redefines_l2a_for_cdse():
    from sentinelhub import DataCollection

    cdse = sentinel_collection(FakeConfig(CDSE_BASE))
    assert cdse.service_url == CDSE_BASE
    assert cdse.api_id == DataCollection.SENTINEL2_L2A.api_id
    assert sentinel_collection(FakeConfig()) is DataCollection.SENTINEL2_L2A


def test_source_implements_the_image_source_protocol():
    src, _, _ = source()
    assert isinstance(src, ImageSource)
    assert src.source_name == "sentinel"


def test_source_builds_its_config_from_credentials():
    src = SentinelHubSource(
        creds("cdse"), FakeConfig(), collection="s2l2a", catalog=FakeCatalog([])
    )
    assert src.config.sh_base_url == CDSE_BASE
    assert src.config.sh_client_id == "test-id"


# -- find_scenes -----------------------------------------------------------


def test_find_scenes_returns_one_scene_per_acquisition_day():
    src, catalog, _ = source(
        [
            item("a", "2020-03-04T16:00:00Z", 12.0),
            item("b", "2020-03-09T16:00:00Z", 3.0),
        ]
    )
    scenes = src.find_scenes(AOI, date(2020, 3, 1), date(2020, 3, 10))
    assert [s.acquired for s in scenes] == [date(2020, 3, 4), date(2020, 3, 9)]
    assert [s.source for s in scenes] == ["sentinel", "sentinel"]
    assert [s.cloud_cover for s in scenes] == [12.0, 3.0]
    assert catalog.calls[0]["time"] == ("2020-03-01", "2020-03-10")
    assert catalog.calls[0]["bbox"].crs.epsg == 4326
    assert tuple(catalog.calls[0]["bbox"]) == pytest.approx(AOI.bounds)


def test_find_scenes_collapses_tiles_of_the_same_day_keeping_the_clearest():
    src, _, _ = source(
        [
            item("tile-a", "2020-03-04T16:00:00Z", 40.0),
            item("tile-b", "2020-03-04T16:00:10Z", 5.0),
            item("tile-c", "2020-03-04T16:00:20Z", 60.0),
        ]
    )
    (scene,) = src.find_scenes(AOI, date(2020, 3, 1), date(2020, 3, 10))
    assert scene.id == "tile-b"
    assert scene.cloud_cover == 5.0
    assert sorted(scene.metadata["ids"]) == ["tile-a", "tile-b", "tile-c"]


def test_find_scenes_sorts_by_date_and_drops_items_outside_the_window():
    src, _, _ = source(
        [
            item("late", "2020-03-20T00:00:00Z"),
            item("b", "2020-03-09T16:00:00Z"),
            item("a", "2020-03-04T16:00:00Z"),
            item("undated", None),
            item("junk", "not-a-date"),
        ]
    )
    scenes = src.find_scenes(AOI, date(2020, 3, 1), date(2020, 3, 10))
    assert [s.acquired for s in scenes] == [date(2020, 3, 4), date(2020, 3, 9)]


def test_find_scenes_honours_max_cloud_cover():
    src, _, _ = source(
        [item("a", "2020-03-04T16:00:00Z", 90.0), item("b", "2020-03-09T16:00:00Z", 4.0)],
        max_cloud_cover=20,
    )
    scenes = src.find_scenes(AOI, date(2020, 3, 1), date(2020, 3, 10))
    assert [s.id for s in scenes] == ["b"]


def test_find_scenes_is_empty_before_the_sentinel_2_cutoff():
    src, catalog, _ = source([item("a", "2014-01-02T00:00:00Z")])
    assert src.find_scenes(AOI, date(2013, 1, 1), date(2014, 6, 1)) == []
    assert catalog.calls == []


def test_find_scenes_clamps_the_search_window_to_the_cutoff():
    src, catalog, _ = source([item("a", "2015-07-01T00:00:00Z")])
    scenes = src.find_scenes(AOI, date(2015, 1, 1), date(2015, 8, 1))
    assert catalog.calls[0]["time"] == (SENTINEL_CUTOFF.isoformat(), "2015-08-01")
    assert [s.acquired for s in scenes] == [date(2015, 7, 1)]


def test_dates_lists_acquisition_days():
    src, _, _ = source([item("a", "2020-03-04T16:00:00Z"), item("b", "2020-03-09T16:00:00Z")])
    assert src.dates(AOI, date(2020, 3, 1), date(2020, 3, 10)) == [
        date(2020, 3, 4),
        date(2020, 3, 9),
    ]


# -- fetch -----------------------------------------------------------------


def scene_on(day=date(2020, 3, 4)):
    return Scene(id="s", acquired=day, source="sentinel")


def test_fetch_rgb_returns_hwc3_uint8_and_requests_the_true_colour_evalscript():
    from sentinelhub import MimeType

    pixels = np.zeros((4, 5, 3), dtype=np.uint8)
    pixels[..., 0] = 200
    src, _, factory = source(data=[pixels])
    chip = src.fetch(scene_on(), AOI, "rgb")

    assert chip.shape == (4, 5, 3)
    assert chip.dtype == np.uint8
    assert chip[0, 0, 0] == 200
    call = factory.calls[0]
    assert call["evalscript"] == TRUE_COLOR_EVALSCRIPT
    assert call["mime"] is MimeType.PNG
    assert call["time_interval"] == ("2020-03-04", "2020-03-04")
    from sentinelhub import CRS, BBox, bbox_to_dimensions

    assert call["size"] == bbox_to_dimensions(BBox(AOI.bounds, CRS.WGS84), resolution=10)


def test_fetch_nir_squeezes_and_scales_float_reflectance():
    from sentinelhub import MimeType

    src, _, factory = source(data=[np.full((4, 5, 1), 0.5, dtype=np.float32)])
    chip = src.fetch(scene_on(), AOI, "nir")

    assert chip.shape == (4, 5, 1)
    assert chip.dtype == np.uint8
    assert chip[0, 0, 0] == 127
    call = factory.calls[0]
    assert call["evalscript"] == NIR_EVALSCRIPT
    assert call["mime"] is MimeType.TIFF


def test_fetch_nir_accepts_a_plain_2d_response():
    src, _, _ = source(data=[np.full((3, 3), 0.25, dtype=np.float32)])
    chip = src.fetch(scene_on(), AOI, "nir")
    assert chip.shape == (3, 3, 1)
    assert chip[0, 0, 0] == 63


def test_fetch_rgb_drops_an_alpha_channel():
    src, _, _ = source(data=[np.full((2, 2, 4), 10, dtype=np.uint8)])
    assert src.fetch(scene_on(), AOI, "rgb").shape == (2, 2, 3)


def test_fetch_raises_on_an_empty_response():
    src, _, _ = source(data=[])
    with pytest.raises(RuntimeError, match="no rgb data"):
        src.fetch(scene_on(), AOI, "rgb")
    src, _, _ = source(data=[None])
    with pytest.raises(RuntimeError):
        src.fetch(scene_on(), AOI, "nir")


def test_fetch_rejects_an_unknown_band():
    src, _, _ = source(data=[np.zeros((2, 2, 3), dtype=np.uint8)])
    with pytest.raises(ValueError, match="band must be"):
        src.fetch(scene_on(), AOI, "swir")


def test_fetch_request_size_is_at_least_one_pixel():
    src, _, factory = source(data=[np.zeros((1, 1, 3), dtype=np.uint8)])
    tiny = box(-83.0, 40.0, -83.0 + 1e-9, 40.0 + 1e-9)
    src.fetch(scene_on(), tiny, "rgb")
    assert factory.calls[0]["size"] == (1, 1)


def test_fetch_chip_can_be_written_by_chips_save_png(tmp_path):
    from cssic.imagery.chips import save_png

    src, _, _ = source(data=[np.full((4, 5, 1), 0.5, dtype=np.float32)])
    written = save_png(src.fetch(scene_on(), AOI, "nir"), tmp_path / "2020-03-04.png")
    assert written.exists()
