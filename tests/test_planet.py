"""Planet backend tests. The Planet client is faked; nothing touches the network."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import box, mapping

from cssic.config import GatherConfig
from cssic.imagery.planet import (
    FALLBACK_BUNDLES,
    ITEM_TYPE,
    PLANET_CUTOFF,
    OrderRecord,
    PlanetSource,
    _chunk,
    acquired_date_from_filename,
    analytic_nir,
    analytic_rgb,
    as_acquired_datetime,
    classify_planet_file,
    complete_log_path,
    order_log_path,
    read_order_log,
)
from cssic.store import Workspace

PSSCENE_VISUAL = Path("20200925_161029_69_2223_3B_Visual_clip.tif")
PSSCENE_ANALYTIC = Path("20200925_161029_69_2223_3B_AnalyticMS_clip.tif")
PSSCENE_UDM = Path("20200925_161029_69_2223_3B_udm2_clip.tif")
PSSCENE_SR = Path("20221003_002705_38_2461_3B_AnalyticMS_SR.tif")

SITE = box(0.0, 0.0, 0.01, 0.01)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeData:
    def __init__(self, items: list[dict] | None = None) -> None:
        self.items = items or []
        self.calls: list[dict] = []

    def search(self, item_types, search_filter=None, sort=None, limit=None):
        self.calls.append(
            {"item_types": item_types, "filter": search_filter, "sort": sort, "limit": limit}
        )
        return iter(list(self.items))


class FakeOrders:
    def __init__(self, states: dict[str, str] | None = None, deliver=None) -> None:
        self.states = states or {}
        self.deliver = deliver
        self.requests: list[dict] = []
        self.downloaded: list[str] = []
        self._next = 0

    def create_order(self, request):
        self.requests.append(request)
        self._next += 1
        return {"id": f"order-{self._next}", "state": "queued"}

    def get_order(self, order_id):
        return {"id": order_id, "state": self.states.get(order_id, "success")}

    def download_order(self, order_id, directory=Path("."), overwrite=False, progress_bar=False):
        self.downloaded.append(order_id)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if self.deliver is not None:
            return self.deliver(order_id, directory)
        return []


class FakeClient:
    def __init__(self, items=None, states=None, deliver=None) -> None:
        self.data = FakeData(items)
        self.orders = FakeOrders(states, deliver)


def item(item_id: str, geom, acquired: str, cloud: float = 0.1) -> dict:
    return {
        "id": item_id,
        "geometry": dict(mapping(geom)),
        "properties": {"acquired": acquired, "cloud_cover": cloud, "item_type": ITEM_TYPE},
    }


@pytest.fixture
def planet_bundle_spec(monkeypatch):
    """Keep SDK validation offline without replacing request construction."""
    from planet import specs

    names = ("visual", "analytic_udm2", "analytic_sr_udm2")
    monkeypatch.setattr(
        specs,
        "PRODUCT_BUNDLES",
        {
            "item_types": {"PSScene"},
            "bundle_names": names,
            "bundles": {name: {"assets": {"PSScene": []}} for name in names},
        },
    )


def make_source(client=None, **cfg_kwargs) -> PlanetSource:
    cfg = GatherConfig(source="planet", **{"rgb": True, "nir": True, **cfg_kwargs})
    return PlanetSource(credentials=None, cfg=cfg, client=client or FakeClient())


def make_gdf(chain_id: str = "1_2-0", start: str = "2019-01-01", end: str = "2019-06-01"):
    gpd = pytest.importorskip("geopandas")
    return gpd.GeoDataFrame(
        {"chain_id": [chain_id], "start": [start], "end": [end]},
        geometry=[SITE],
        crs="EPSG:4326",
    )


def write_tif(path: Path, array: np.ndarray) -> Path:
    """Write an HxWxC (or HxW) array as a small GeoTIFF."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    if array.ndim == 2:
        array = array[:, :, np.newaxis]
    height, width, bands = array.shape
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=bands,
        dtype=array.dtype,
        crs="EPSG:4326",
        transform=from_origin(0, 0.01, 0.001, 0.001),
    ) as dataset:
        dataset.write(np.moveaxis(array, -1, 0))
    return path


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------
def test_acquired_date_comes_from_the_yyyymmdd_prefix_not_underscore_index_two():
    # split("_")[2] would be "69" / "38" (a satellite fragment), not the date.
    assert acquired_date_from_filename(PSSCENE_VISUAL) == "2020-09-25"
    assert acquired_date_from_filename(PSSCENE_ANALYTIC) == "2020-09-25"
    assert acquired_date_from_filename(PSSCENE_UDM) == "2020-09-25"
    assert acquired_date_from_filename(PSSCENE_SR) == "2022-10-03"
    assert acquired_date_from_filename(Path("not_a_planet_file.tif")) is None


def test_classify_planet_file_visual_analytic_udm():
    assert classify_planet_file(PSSCENE_VISUAL) == "rgb"
    assert classify_planet_file(PSSCENE_ANALYTIC) == "nir"
    assert classify_planet_file(PSSCENE_SR) == "nir"
    assert classify_planet_file(PSSCENE_UDM) == "udm"
    assert classify_planet_file(Path("20200925_161029_69_2223_manifest.json")) is None
    # udm wins even when the name also says analytic
    assert classify_planet_file(Path("20200925_1_3B_AnalyticMS_DN_udm.tif")) == "udm"


def test_as_acquired_datetime_accepts_iso_date_and_datetime():
    expected = datetime(2018, 1, 2, tzinfo=timezone.utc)
    assert as_acquired_datetime("2018-01-02") == expected
    assert as_acquired_datetime(date(2018, 1, 2)) == expected
    assert as_acquired_datetime(datetime(2018, 1, 2)) == expected
    assert as_acquired_datetime(expected) == expected


def test_analytic_band_order_for_four_and_eight_band_psscene():
    four = np.zeros((1, 1, 4), dtype=np.uint16)
    four[0, 0] = [10, 20, 30, 40]  # B, G, R, NIR
    assert analytic_rgb(four)[0, 0].tolist() == [30, 20, 10]
    assert analytic_nir(four)[0, 0] == 40

    eight = np.zeros((1, 1, 8), dtype=np.uint16)
    eight[0, 0] = [1, 2, 3, 4, 5, 6, 7, 8]  # coastal, blue, green-i, green, yellow, red, RE, NIR
    assert analytic_rgb(eight)[0, 0].tolist() == [6, 4, 2]
    assert analytic_nir(eight)[0, 0] == 8

    assert analytic_nir(np.array([[7]], dtype=np.uint16))[0, 0] == 7


def test_item_type_is_psscene_not_the_retired_types():
    assert ITEM_TYPE == "PSScene"
    assert ITEM_TYPE not in {"PSOrthoTile", "PSScene4Band"}


def test_chunking_respects_the_item_cap():
    assert _chunk(list("abcde"), 2) == [["a", "b"], ["c", "d"], ["e"]]
    assert _chunk([], 3) == []


def test_bundles_follow_the_requested_bands():
    assert make_source(rgb=True, nir=False).bundles == ["visual"]
    assert make_source(rgb=False, nir=True).bundles == ["analytic_udm2"]
    assert make_source().bundles == ["visual", "analytic_udm2"]
    assert FALLBACK_BUNDLES["visual"] == "analytic_udm2"
    assert FALLBACK_BUNDLES["analytic_udm2"] == "analytic_sr_udm2"


def test_gather_config_rejects_no_bands():
    with pytest.raises(ValueError):
        GatherConfig(source="planet", rgb=False, nir=False)


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------
def test_find_scenes_prefers_a_covering_scene_and_stops_at_the_first():
    covering = box(-1, -1, 1, 1)
    client = FakeClient(
        [
            item("partial", box(0.005, 0.0, 1.0, 1.0), "2019-03-01T10:00:00Z"),
            item("covering-a", covering, "2019-03-02T10:00:00Z"),
            item("covering-b", covering, "2019-03-03T10:00:00Z"),
        ]
    )
    scenes = make_source(client).find_scenes(SITE, date(2019, 3, 1), date(2019, 3, 5))
    assert [scene.id for scene in scenes] == ["covering-a"]
    assert scenes[0].acquired == date(2019, 3, 2)
    assert scenes[0].source == "planet"
    assert scenes[0].cloud_cover == 0.1
    assert client.data.calls[0]["limit"] == 250
    assert client.data.calls[0]["sort"] == "acquired asc"


def test_find_scenes_with_all_dates_keeps_one_scene_per_acquisition_date():
    covering = box(-1, -1, 1, 1)
    client = FakeClient(
        [
            item("a1", covering, "2019-03-02T10:00:00Z"),
            item("a2", covering, "2019-03-02T12:00:00Z"),
            item("b1", covering, "2019-03-04T10:00:00Z"),
        ]
    )
    source = make_source(client)
    scenes = source.find_scenes(SITE, date(2019, 3, 1), date(2019, 3, 5), all_dates=True)
    assert [scene.id for scene in scenes] == ["a1", "b1"]
    assert client.data.calls[0]["limit"] == 0


def test_find_scenes_falls_back_to_the_largest_overlap_when_nothing_covers():
    client = FakeClient(
        [
            item("small", box(0.0, 0.0, 0.002, 0.01), "2019-03-02T10:00:00Z"),
            item("big", box(0.0, 0.0, 0.008, 0.01), "2019-03-03T10:00:00Z"),
            item("elsewhere", box(5, 5, 6, 6), "2019-03-04T10:00:00Z"),
        ]
    )
    scenes = make_source(client).find_scenes(SITE, date(2019, 3, 1), date(2019, 3, 5))
    assert [scene.id for scene in scenes] == ["big"]


def test_find_scenes_is_empty_when_the_search_fails():
    from planet.exceptions import APIError

    class Boom(FakeData):
        def search(self, *args, **kwargs):
            raise APIError("nope")

    client = FakeClient()
    client.data = Boom()
    assert make_source(client).find_scenes(SITE, date(2019, 3, 1), date(2019, 3, 5)) == []


def test_search_filter_carries_geometry_dates_and_permission():
    source = make_source()
    built = source.search_filter(SITE, date(2019, 3, 1), date(2019, 3, 6))
    kinds = {sub["type"] for sub in built["config"]}
    assert built["type"] == "AndFilter"
    assert kinds == {"GeometryFilter", "DateRangeFilter", "PermissionFilter"}
    dates = next(sub for sub in built["config"] if sub["type"] == "DateRangeFilter")
    assert dates["config"]["gte"].startswith("2019-03-01")
    assert dates["config"]["lte"].startswith("2019-03-06")


def test_search_filter_honours_max_cloud_cover():
    """``--max-cloud`` is a percentage; PSScene ``cloud_cover`` is a 0-1 fraction."""
    built = make_source(max_cloud_cover=10.0).search_filter(
        SITE, date(2019, 3, 1), date(2019, 3, 6)
    )
    clouds = next(sub for sub in built["config"] if sub["type"] == "RangeFilter")
    assert clouds["field_name"] == "cloud_cover"
    assert clouds["config"] == {"lte": 0.1}


def test_search_filter_without_max_cloud_cover_has_no_range_filter():
    built = make_source().search_filter(SITE, date(2019, 3, 1), date(2019, 3, 6))
    assert {sub["type"] for sub in built["config"]} == {
        "GeometryFilter",
        "DateRangeFilter",
        "PermissionFilter",
    }


def test_fetch_is_not_available_for_planet():
    from cssic.imagery.base import Scene

    source = make_source()

    with pytest.raises(NotImplementedError, match="bulk_order"):
        source.fetch(Scene(id="x", acquired=date(2019, 1, 1), source="planet"), SITE, "rgb")


# --------------------------------------------------------------------------
# ordering
# --------------------------------------------------------------------------
def test_create_order_skips_sites_that_start_before_planetscope_exists(tmp_path):
    ws = Workspace(tmp_path)
    client = FakeClient([item("a", box(-1, -1, 1, 1), "2016-03-02T10:00:00Z")])
    source = make_source(client)
    row = make_gdf(start=str(PLANET_CUTOFF.replace(year=2016)), end="2016-06-01").iloc[0]
    assert source.create_order(row, ws) == []
    assert client.orders.requests == []


def test_create_order_builds_a_clipped_partial_order_with_both_bundles(
    tmp_path, planet_bundle_spec
):
    ws = Workspace(tmp_path)
    covering = box(-1, -1, 1, 1)
    client = FakeClient(
        [
            item("a", covering, "2019-01-01T10:00:00Z"),
            item("b", covering, "2019-03-15T10:00:00Z"),
        ]
    )
    source = make_source(client, num_images=3, padding=2.0)
    records = source.create_order(make_gdf().iloc[0], ws)

    assert [record.order_id for record in records] == ["order-1"]
    assert records[0].chain_id == "1_2-0"
    request = client.orders.requests[0]
    assert request["name"] == "1_2-0"
    assert request["order_type"] == "partial"
    # the SDK folds the fallback into product_bundle as "<bundle>,<fallback>"
    assert [product["product_bundle"] for product in request["products"]] == [
        "visual,analytic_udm2",
        "analytic_udm2,analytic_sr_udm2",
    ]
    assert all(product["item_type"] == ITEM_TYPE for product in request["products"])
    tool_names = [next(iter(tool)) for tool in request["tools"]]
    assert tool_names == ["clip", "reproject"]
    # the clip AOI is the padded bounding box, wider than the raw site
    clip_bounds = _bounds(request["tools"][0]["clip"]["aoi"])
    assert clip_bounds[0] < SITE.bounds[0] and clip_bounds[2] > SITE.bounds[2]
    # the order is logged so --download can find it
    assert read_order_log(ws) == [OrderRecord("order-1", "1_2-0")]
    assert order_log_path(ws) == tmp_path / "temp" / "order_log.txt"


def test_create_order_without_scenes_creates_nothing(tmp_path, capsys):
    ws = Workspace(tmp_path)
    client = FakeClient([])
    source = make_source(client)
    assert source.create_order(make_gdf().iloc[0], ws) == []
    assert client.orders.requests == []
    assert read_order_log(ws) == []
    assert "not permitted" not in capsys.readouterr().out


def test_an_unentitled_account_is_told_why_every_search_is_empty(tmp_path, capsys):
    """Live: a new account sees PSScene items with ``_permissions: []`` and the
    permission filter hides all of them, which looked like "no imagery"."""
    ws = Workspace(tmp_path)

    class UnentitledData(FakeData):
        def search(self, item_types, search_filter=None, sort=None, limit=None):
            super().search(item_types, search_filter, sort, limit)
            filtered = "permission" in str(search_filter).lower()
            if filtered:
                return iter([])
            found = item("a", box(-1, -1, 1, 1), "2019-01-01T10:00:00Z")
            found["_permissions"] = []
            return iter([found])

    client = FakeClient([])
    client.data = UnentitledData()
    source = make_source(client)
    frame = make_gdf()

    assert source.create_order(frame.iloc[0], ws) == []
    assert source.create_order(frame.iloc[0], ws) == []

    out = capsys.readouterr().out
    assert out.count("not permitted to download") == 1
    # Only the first empty site pays for the diagnostic search.
    assert (
        sum(1 for call in client.data.calls if "permission" not in str(call["filter"]).lower()) == 1
    )


def test_email_notifications_only_when_requested(tmp_path, planet_bundle_spec):
    ws = Workspace(tmp_path)
    covering = box(-1, -1, 1, 1)
    items = [item("a", covering, "2019-01-01T10:00:00Z")]

    quiet = make_source(FakeClient(items))
    quiet.create_order(make_gdf().iloc[0], ws)
    assert "notifications" not in quiet.client.orders.requests[0]

    loud = make_source(FakeClient(items), email=True)
    loud.create_order(make_gdf().iloc[0], ws)
    assert loud.client.orders.requests[0]["notifications"] == {"email": True}


def test_bulk_order_walks_every_chain_and_logs_each_order(tmp_path, planet_bundle_spec):
    gpd = pytest.importorskip("geopandas")
    ws = Workspace(tmp_path)
    covering = box(-1, -1, 1, 1)
    client = FakeClient([item("a", covering, "2019-01-01T10:00:00Z")])
    gdf = gpd.GeoDataFrame(
        {
            "chain_id": ["chain-a", "chain-b"],
            "start": ["2019-01-01", "2019-01-01"],
            "end": ["2019-06-01", "2019-06-01"],
        },
        geometry=[SITE, SITE],
        crs="EPSG:4326",
    )
    records = make_source(client, num_images=1).bulk_order(gdf, ws)
    assert [(r.order_id, r.chain_id) for r in records] == [
        ("order-1", "chain-a"),
        ("order-2", "chain-b"),
    ]
    assert read_order_log(ws) == records


# --------------------------------------------------------------------------
# downloading
# --------------------------------------------------------------------------
def _bounds(geojson: dict) -> tuple[float, float, float, float]:
    from shapely.geometry import shape

    return shape(geojson).bounds


def _deliver_visual(order_id: str, directory: Path):
    array = np.zeros((4, 4, 4), dtype=np.uint8)
    array[..., 0] = 200  # red
    array[..., 3] = 255  # alpha/mask
    return [write_tif(directory / f"{order_id}/20190115_161029_69_2223_3B_Visual_clip.tif", array)]


def test_download_orders_without_a_log_does_nothing(tmp_path, capsys):
    ws = Workspace(tmp_path)
    assert make_source().download_orders(ws) == []
    assert "order_log.txt" in capsys.readouterr().out


def test_download_orders_writes_chips_and_clears_the_log(tmp_path):
    ws = Workspace(tmp_path)
    ws.setup()
    client = FakeClient(states={"order-9": "success"}, deliver=_deliver_visual)
    source = make_source(client, rgb=True, nir=False)
    order_log_path(ws).parent.mkdir(parents=True, exist_ok=True)
    order_log_path(ws).write_text("order-9,chain-a\n", encoding="utf-8")

    completed = source.download_orders(ws)

    assert [(r.order_id, r.state) for r in completed] == [("order-9", "success")]
    assert client.orders.downloaded == ["order-9"]
    chip = ws.chip_path("chain-a", "planet", "rgb", "2019-01-15")
    assert chip.is_file()
    assert (chip.parent / "2019-01-15_mask.png").is_file()
    # staging directory is cleaned up, the log is gone, the audit log is written
    assert not (ws.images_dir("chain-a", "planet") / "temp").exists()
    assert not order_log_path(ws).exists()
    assert "order-9,success,chain-a" in complete_log_path(ws).read_text(encoding="utf-8")


def test_download_orders_keeps_unfinished_orders_in_the_log(tmp_path):
    ws = Workspace(tmp_path)
    ws.setup()
    client = FakeClient(
        states={"order-1": "running", "order-2": "success"}, deliver=_deliver_visual
    )
    source = make_source(client, rgb=True, nir=False)
    order_log_path(ws).parent.mkdir(parents=True, exist_ok=True)
    order_log_path(ws).write_text("order-1,chain-a\norder-2,chain-b\n", encoding="utf-8")

    completed = source.download_orders(ws)

    assert [r.order_id for r in completed] == ["order-2"]
    assert client.orders.downloaded == ["order-2"]
    assert read_order_log(ws) == [OrderRecord("order-1", "chain-a")]


def test_an_order_that_delivered_nothing_usable_stays_pending(tmp_path, capsys):
    """A download can succeed and still produce no imagery.

    Retiring the order to the complete log would leave the chain silently
    without chips and no way to notice: the delivery is gone, the log is clean.
    """

    def deliver_unreadable(order_id, directory):
        path = directory / "20190115_161029_69_2223_3B_Visual_clip.tif"
        path.write_bytes(b"not a GeoTIFF")
        return [path]

    ws = Workspace(tmp_path)
    ws.setup()
    client = FakeClient(states={"order-1": "success"}, deliver=deliver_unreadable)
    source = make_source(client, rgb=True, nir=False)
    order_log_path(ws).parent.mkdir(parents=True, exist_ok=True)
    order_log_path(ws).write_text("order-1,chain-a\n", encoding="utf-8")

    completed = source.download_orders(ws)

    assert completed == []
    assert read_order_log(ws) == [OrderRecord("order-1", "chain-a")]
    assert not complete_log_path(ws).exists()
    assert list(tmp_path.rglob("*.png")) == []
    out = capsys.readouterr().out
    assert "delivered no usable chip" in out
    assert "still 1 order(s) outstanding" in out


def test_an_order_delivering_only_blank_chips_stays_pending(tmp_path):
    """The blank guard must not turn "everything was cloud" into "collected"."""

    def deliver_blank(order_id, directory):
        array = np.zeros((3, 3, 4), dtype=np.uint8)
        return [write_tif(directory / "20190115_1_3B_Visual_clip.tif", array)]

    ws = Workspace(tmp_path)
    ws.setup()
    client = FakeClient(states={"order-1": "success"}, deliver=deliver_blank)
    source = make_source(client, rgb=True, nir=False)
    order_log_path(ws).parent.mkdir(parents=True, exist_ok=True)
    order_log_path(ws).write_text("order-1,chain-a\n", encoding="utf-8")

    assert source.download_orders(ws) == []
    assert read_order_log(ws) == [OrderRecord("order-1", "chain-a")]


def test_an_order_delivering_only_udm_masks_stays_pending(tmp_path):
    """A UDM is metadata about a chip, not a chip."""

    def deliver_udm(order_id, directory):
        array = np.ones((2, 2, 1), dtype=np.uint8)
        return [write_tif(directory / "20190115_1_3B_udm2_clip.tif", array)]

    ws = Workspace(tmp_path)
    ws.setup()
    client = FakeClient(states={"order-1": "success"}, deliver=deliver_udm)
    source = make_source(client, rgb=True, nir=False)
    order_log_path(ws).parent.mkdir(parents=True, exist_ok=True)
    order_log_path(ws).write_text("order-1,chain-a\n", encoding="utf-8")

    assert source.download_orders(ws) == []
    assert read_order_log(ws) == [OrderRecord("order-1", "chain-a")]


def test_failed_orders_leave_the_log_without_being_downloaded(tmp_path):
    ws = Workspace(tmp_path)
    ws.setup()
    client = FakeClient(states={"order-1": "failed"}, deliver=_deliver_visual)
    source = make_source(client, rgb=True, nir=False)
    order_log_path(ws).parent.mkdir(parents=True, exist_ok=True)
    order_log_path(ws).write_text("order-1,chain-a\n", encoding="utf-8")

    completed = source.download_orders(ws)
    assert [(r.order_id, r.state) for r in completed] == [("order-1", "failed")]
    assert client.orders.downloaded == []
    assert read_order_log(ws) == []


def test_order_record_round_trips_through_the_log():
    record = OrderRecord("abc", "1_2-3")
    assert record.log_line == "abc,1_2-3\n"
    assert OrderRecord.parse(record.log_line) == record
    assert OrderRecord.parse("\n") is None
    assert OrderRecord.parse("only-one-field") is None


# --------------------------------------------------------------------------
# writing chips
# --------------------------------------------------------------------------
def test_rewrite_tif_visual_writes_rgb_and_the_alpha_mask(tmp_path):
    array = np.zeros((3, 3, 4), dtype=np.uint8)
    array[..., 0] = 10
    array[..., 1] = 20
    array[..., 2] = 30
    array[..., 3] = 255
    source_tif = write_tif(tmp_path / "20190115_161029_69_2223_3B_Visual_clip.tif", array)
    planet_dir = tmp_path / "planet"

    written = make_source().rewrite_tif(source_tif, planet_dir)

    assert [path.name for path in written] == ["2019-01-15.png", "2019-01-15_mask.png"]
    from PIL import Image

    with Image.open(planet_dir / "rgb" / "2019-01-15.png") as handle:
        assert np.asarray(handle)[0, 0].tolist() == [10, 20, 30]


def test_rewrite_tif_analytic_writes_nir_and_synthesises_rgb_when_requested(tmp_path):
    array = np.zeros((3, 3, 4), dtype=np.uint16)
    array[..., 0] = 1000  # blue
    array[..., 1] = 2000  # green
    array[..., 2] = 3000  # red
    array[..., 3] = 9000  # nir
    array[0, 0, 3] = 10000  # a chip with one value everywhere reads as blank
    source_tif = write_tif(tmp_path / "20190115_161029_69_2223_3B_AnalyticMS_clip.tif", array)
    planet_dir = tmp_path / "planet"
    (planet_dir / "rgb").mkdir(parents=True)  # --rgb was requested

    written = make_source().rewrite_tif(source_tif, planet_dir)

    assert {path.parent.name for path in written} == {"nir", "rgb"}
    from PIL import Image

    with Image.open(planet_dir / "nir" / "2019-01-15.png") as handle:
        nir = np.asarray(handle)
    assert nir.ndim == 2 and nir[0, 0] == 255  # 10000 reflectance -> full scale
    with Image.open(planet_dir / "rgb" / "2019-01-15.png") as handle:
        rgb = np.asarray(handle)
    assert rgb.shape == (3, 3, 3)
    assert rgb[0, 0].tolist() == [76, 51, 25]  # R, G, B from the B,G,R,NIR order


def test_rewrite_tif_does_not_synthesise_rgb_when_rgb_was_not_requested(tmp_path):
    array = np.zeros((3, 3, 4), dtype=np.uint16)
    array[..., 3] = 5000
    array[0, 0, 3] = 6000  # a chip with one value everywhere reads as blank
    source_tif = write_tif(tmp_path / "20190115_1_3B_AnalyticMS_clip.tif", array)
    planet_dir = tmp_path / "planet"

    written = make_source(rgb=False, nir=True).rewrite_tif(source_tif, planet_dir)

    assert [path.parent.name for path in written] == ["nir"]
    assert not (planet_dir / "rgb").exists()


def test_rewrite_tif_refuses_an_all_nodata_visual_chip(tmp_path, capsys):
    """The Planet path skips blank chips like the Sentinel ones, not unconditionally.

    An all-zero clip is the delivery of a scene that missed the site; writing it
    puts a black frame in the sequence that looks like a real observation.
    """
    array = np.zeros((3, 3, 4), dtype=np.uint8)
    source_tif = write_tif(tmp_path / "20190115_1_3B_Visual_clip.tif", array)
    planet_dir = tmp_path / "planet"

    assert make_source().rewrite_tif(source_tif, planet_dir) == []
    assert list(planet_dir.rglob("*.png")) == []
    assert "SKIPPED blank chip rgb/2019-01-15.png" in capsys.readouterr().out


def test_rewrite_tif_refuses_a_chip_saturated_by_cloud(tmp_path):
    """Solid cloud over the clip arrives as one constant value, not as nodata."""
    array = np.full((3, 3, 4), 255, dtype=np.uint8)
    source_tif = write_tif(tmp_path / "20190115_1_3B_Visual_clip.tif", array)
    planet_dir = tmp_path / "planet"

    assert make_source().rewrite_tif(source_tif, planet_dir) == []
    assert list(planet_dir.rglob("*.png")) == []


def test_rewrite_tif_keeps_a_uniform_alpha_mask_beside_a_real_chip(tmp_path):
    """A mask that is 255 everywhere means every pixel is valid; it is not blank."""
    array = np.zeros((3, 3, 4), dtype=np.uint8)
    array[..., 0] = 10
    array[0, 0, 0] = 20
    array[..., 3] = 255
    source_tif = write_tif(tmp_path / "20190115_1_3B_Visual_clip.tif", array)
    planet_dir = tmp_path / "planet"

    written = make_source().rewrite_tif(source_tif, planet_dir)

    assert [path.name for path in written] == ["2019-01-15.png", "2019-01-15_mask.png"]


def test_rewrite_tif_copies_udm_masks_and_ignores_other_files(tmp_path):
    array = np.ones((2, 2, 1), dtype=np.uint8)
    udm = write_tif(tmp_path / "20190115_1_3B_udm2_clip.tif", array)
    planet_dir = tmp_path / "planet"
    source = make_source()

    written = source.rewrite_tif(udm, planet_dir)
    assert [path.name for path in written] == ["2019-01-15_udm.tif"]
    assert (planet_dir / "nir" / "2019-01-15_udm.tif").is_file()

    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    assert source.rewrite_tif(manifest, planet_dir) == []

    undated = write_tif(tmp_path / "no_date_here_Visual.tif", array)
    assert source.rewrite_tif(undated, planet_dir) == []


def test_write_empty_placeholders_fills_dates_with_no_acquisition(tmp_path):
    from cssic.imagery.chips import save_png

    planet_dir = tmp_path / "planet"
    rgb_dir = planet_dir / "rgb"
    rgb_dir.mkdir(parents=True)
    save_png(np.full((2, 2, 3), 128, dtype=np.uint8), rgb_dir / "2019-01-02.png")

    source = make_source(rgb=True, nir=False, num_images=-1)
    gdf = make_gdf(chain_id="chain-a", start="2019-01-01", end="2019-01-04")
    written = source.write_empty_placeholders("chain-a", planet_dir, gdf)

    assert sorted(path.name for path in written) == [
        "2019-01-01_empty.png",
        "2019-01-03_empty.png",
        "2019-01-04_empty.png",
    ]
    from PIL import Image

    with Image.open(rgb_dir / "2019-01-01_empty.png") as handle:
        blank = np.asarray(handle)
    assert blank.shape == (2, 2, 3) and blank.max() == 0


def test_write_empty_placeholders_needs_an_existing_chip_and_a_known_chain(tmp_path):
    planet_dir = tmp_path / "planet"
    (planet_dir / "rgb").mkdir(parents=True)
    source = make_source(rgb=True, nir=False, num_images=-1)
    gdf = make_gdf(chain_id="chain-a", start="2019-01-01", end="2019-01-04")
    assert source.write_empty_placeholders("chain-a", planet_dir, gdf) == []
    assert source.write_empty_placeholders("missing", planet_dir, gdf) == []
    assert source.write_empty_placeholders("chain-a", planet_dir, None) == []
