"""Output layout: output/collection, output/{chain_id}/info.txt, image dirs, resets."""

import pytest

from cssic.store import Workspace


def test_setup_creates_workspace_dirs(tmp_path):
    Workspace(tmp_path).setup()
    assert (tmp_path / "temp" / "snapshots").is_dir()
    assert (tmp_path / "output" / "collection").is_dir()


def test_paths_follow_the_v2_layout(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.site_dir("10_20-0") == tmp_path / "output" / "10_20-0"
    assert ws.info_path("10_20-0").name == "info.txt"
    assert ws.chip_path("10_20-0", "sentinel", "rgb", "2018-04-15") == (
        tmp_path / "output" / "10_20-0" / "images" / "sentinel" / "rgb" / "2018-04-15.png"
    )


def test_chip_path_accepts_a_date(tmp_path):
    from datetime import date

    ws = Workspace(tmp_path)
    assert ws.chip_path("c", "planet", "nir", date(2019, 3, 4)).name == "2019-03-04.png"


def test_write_and_read_info(tmp_path):
    ws = Workspace(tmp_path)
    path = ws.write_info(
        "10-0", "2018-01-01", "2018-06-01", "farmland", "residential", (1.0, 2.0, 3.0, 4.0)
    )
    assert path.read_text() == "2018-01-01,2018-06-01,farmland,residential,1.0,2.0,3.0,4.0"

    info = ws.read_info("10-0")
    assert info.start == "2018-01-01"
    assert info.prev_tag == "farmland"
    assert info.final_tag == "residential"
    assert info.bounds == (1.0, 2.0, 3.0, 4.0)


def test_collection_path_is_none_before_anything_is_saved(tmp_path):
    assert Workspace(tmp_path).collection_path() is None


def _collection_gdf():
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    return gpd.GeoDataFrame(
        {
            "chain_id": ["10-0", "20-1"],
            "start": ["2018-01-01", "2019-01-01"],
            "end": ["2018-06-01", "2019-06-01"],
            "constr_tag": ["landuse", "building"],
            "prev_tag": ["farmland", "NO TAG FOUND"],
            "final_tag": ["residential", "retail"],
        },
        geometry=[box(0, 0, 1, 1), box(5, 5, 6, 6)],
        crs="EPSG:4326",
    )


def test_save_and_load_collection_roundtrip(tmp_path):
    ws = Workspace(tmp_path)
    written = ws.save_collection(_collection_gdf())

    assert [p.name for p in written] == ["collection.gpkg", "collection.geojson", "collection.shp"]
    assert ws.collection_path().name == "collection.gpkg"
    loaded = ws.load_collection()
    assert sorted(loaded["chain_id"]) == ["10-0", "20-1"]


def test_create_dataset_writes_one_info_per_chain(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.create_dataset(_collection_gdf()) == 2
    assert ws.read_info("10-0").bounds == (0.0, 0.0, 1.0, 1.0)
    assert ws.read_info("20-1").final_tag == "retail"


def test_create_dataset_of_none_is_a_noop(tmp_path):
    assert Workspace(tmp_path).create_dataset(None) == 0


def test_prepare_image_dirs_creates_requested_bands(tmp_path):
    ws = Workspace(tmp_path)
    ws.prepare_image_dirs(_collection_gdf(), "sentinel", ("rgb",))
    assert ws.images_dir("10-0", "sentinel", "rgb").is_dir()
    assert not ws.images_dir("10-0", "sentinel", "nir").exists()


def test_reset_extract_clears_snapshots_and_outputs(tmp_path):
    ws = Workspace(tmp_path)
    ws.setup()
    (ws.snapshot_dir / "2018-01-01-candid.osm").write_text("x")
    (ws.collection_dir / "collection.gpkg").write_text("x")
    site = ws.site_dir("10-0")
    site.mkdir(parents=True)
    (site / "info.txt").write_text("x")
    (tmp_path / "outputpoly.osh.pbf").write_text("x")

    assert ws.reset_extract() == 4

    assert list(ws.snapshot_dir.glob("*")) == []
    assert list(ws.collection_dir.glob("*")) == []
    assert not site.exists()
    assert not (tmp_path / "outputpoly.osh.pbf").exists()
    assert ws.snapshot_dir.is_dir()
    assert ws.collection_dir.is_dir()


def test_reset_images_keeps_sites_and_collection(tmp_path):
    ws = Workspace(tmp_path)
    ws.setup()
    (ws.collection_dir / "collection.gpkg").write_text("x")
    ws.write_info("10-0", "2018-01-01", "2018-06-01", "farmland", "retail", (0, 0, 1, 1))
    chip = ws.chip_path("10-0", "sentinel", "rgb", "2018-01-01")
    chip.parent.mkdir(parents=True)
    chip.write_text("png")

    assert ws.reset_images() == 1

    assert not ws.images_dir("10-0", "sentinel").exists()
    assert ws.info_path("10-0").is_file()
    assert (ws.collection_dir / "collection.gpkg").is_file()


def test_reset_images_on_empty_workspace_is_a_noop(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.reset_images() == 0
    assert not ws.output_dir.exists()


def test_a_tag_with_a_comma_does_not_add_a_field(tmp_path):
    """``info.txt`` is one comma-separated line; OSM tag values are free text."""
    ws = Workspace(tmp_path)
    ws.write_info(
        "10-0",
        "2018-01-01",
        "2018-06-01",
        "name=Smith, Jones and Co",
        "building=yes",
        (1.0, 2.0, 3.0, 4.0),
    )

    info = ws.read_info("10-0")
    assert info.prev_tag == "name=Smith; Jones and Co"
    assert info.bounds == (1.0, 2.0, 3.0, 4.0)


def test_a_tag_with_a_newline_stays_on_one_line(tmp_path):
    ws = Workspace(tmp_path)
    path = ws.write_info(
        "10-0", "2018-01-01", "2018-06-01", "name=two\nlines", "N/A", (1.0, 2.0, 3.0, 4.0)
    )

    assert "\n" not in path.read_text()
    assert ws.read_info("10-0").prev_tag == "name=two lines"


def test_read_info_rejects_a_malformed_line(tmp_path):
    """An info.txt with the wrong field count is corrupt, not a silent short read."""
    ws = Workspace(tmp_path)
    path = ws.info_path("10-0")
    path.parent.mkdir(parents=True)
    path.write_text("2018-01-01,2018-06-01,farmland", encoding="utf-8")

    with pytest.raises(ValueError, match="expected 8"):
        ws.read_info("10-0")


def test_the_no_tag_sentinel_is_the_same_string_in_both_history_backends():
    """v2 wrote this literal into info.txt; the two backends must not drift apart."""
    from cssic.config import NO_TAG
    from cssic.history.osmium import NO_DESCRIPTOR

    assert NO_TAG == NO_DESCRIPTOR == "NO TAG FOUND"
