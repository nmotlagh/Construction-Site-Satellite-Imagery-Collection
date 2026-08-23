"""Construction-chain bookkeeping (port of the v2 way_chain tests)."""

from datetime import date

import pytest

from cssic.sites import Site, SiteCollection


def _site(way_id):
    return Site(way_id, date(2018, 1, 1), date(2018, 2, 1), "building", "N/A", "N/A", None)


def test_find_key_does_not_substring_match():
    """Way 12 must not match a chain whose key/id is 123 (old ``str in key`` bug)."""
    sites = SiteCollection()
    chain_123 = _site(123)
    chain_12 = _site(12)
    sites.add(chain_123)
    sites.add(chain_12)

    assert sites.find_key(12) == f"12-{chain_12.serial_no}"
    assert sites.find_key(123) == f"123-{chain_123.serial_no}"
    assert sites.find_key("12") == f"12-{chain_12.serial_no}"


def test_find_key_missing_way_raises():
    sites = SiteCollection()
    sites.add(_site(123))
    with pytest.raises(KeyError):
        sites.find_key(12)


def test_has_way_reports_membership():
    sites = SiteCollection()
    sites.add(_site(123))
    assert sites.has_way(123)
    assert not sites.has_way(12)


def test_link_forward_appends_id():
    sites = SiteCollection()
    site = _site(10)
    sites.add(site)

    sites.link(10, 20, is_prev=False)

    updated = sites.get(10)
    assert updated.ids == [10, 20]
    assert sites.find_key(20) == f"10_20-{site.serial_no}"


def test_link_backward_inserts_id_at_front():
    sites = SiteCollection()
    site = _site(10)
    sites.add(site)

    sites.link(10, 5, is_prev=True)

    updated = sites.get(10)
    assert updated.ids == [5, 10]
    assert sites.find_key(5) == f"5_10-{site.serial_no}"


def test_link_keeps_dashed_ids_intact():
    """ohsome ids look like ``way-123``; only the trailing serial may be split off."""
    sites = SiteCollection()
    site = _site("way-1077541392")
    sites.add(site)

    key = sites.link("way-1077541392", "way-1235102886")

    assert key == f"way-1077541392_way-1235102886-{site.serial_no}"
    assert sites.get("way-1235102886").ids == ["way-1077541392", "way-1235102886"]
    assert sites.find_key("way-1077541392") == key


def test_link_chain_of_three_dashed_ids():
    sites = SiteCollection()
    site = _site("way-1")
    sites.add(site)
    sites.link("way-1", "way-2")
    key = sites.link("way-2", "relation-3")
    assert key == f"way-1_way-2_relation-3-{site.serial_no}"


def test_update_only_touches_given_fields():
    sites = SiteCollection()
    sites.add(_site(10))

    sites.update(10, end=date(2019, 5, 1), final_tag="residential")

    site = sites.get(10)
    assert site.start == date(2018, 1, 1)
    assert site.end == date(2019, 5, 1)
    assert site.prev_tag == "N/A"
    assert site.final_tag == "residential"


def test_way_ids_spans_all_chains():
    sites = SiteCollection()
    sites.add(_site(1))
    sites.add(_site(2))
    sites.link(1, 3)
    assert sorted(str(i) for i in sites.way_ids()) == ["1", "2", "3"]


def test_map_alias_is_the_same_dict():
    sites = SiteCollection()
    sites.add(_site(7))
    assert sites.map is sites.chains


def test_to_gdf_returns_bounding_boxes_sorted_by_date():
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Polygon

    sites = SiteCollection()
    early = Site(1, date(2018, 1, 1), date(2018, 6, 1), "landuse", "farmland", "residential")
    early.update_geometry(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]))
    late = Site(2, date(2019, 1, 1), date(2019, 6, 1), "building", "NO TAG FOUND", "retail")
    late.update_geometry(Polygon([(5, 5), (5, 6), (6, 6), (6, 5)]))
    sites.add(early)
    sites.add(late)

    gdf = sites.to_gdf()

    assert isinstance(gdf, gpd.GeoDataFrame)
    assert list(gdf.columns) == [
        "chain_id",
        "start",
        "end",
        "constr_tag",
        "prev_tag",
        "final_tag",
        "geometry",
    ]
    assert list(gdf["start"]) == ["2018-01-01", "2019-01-01"]
    assert gdf.crs == "EPSG:4326"
    assert gdf.iloc[0]["geometry"].bounds == (0.0, 0.0, 1.0, 1.0)


def test_to_gdf_of_empty_collection_is_none():
    pytest.importorskip("geopandas")
    assert SiteCollection().to_gdf() is None


def test_update_geometry_unions_footprints():
    pytest.importorskip("shapely")
    from shapely.geometry import Polygon

    site = Site(1)
    site.update_geometry(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]))
    site.update_geometry(Polygon([(1, 0), (1, 1), (2, 1), (2, 0)]))
    assert site.bounds == (0.0, 0.0, 2.0, 1.0)
