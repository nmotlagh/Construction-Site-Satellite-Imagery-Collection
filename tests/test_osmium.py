"""The osmium history backend.

The pure helpers (descriptor choice, construction detection, interval
reconstruction) run everywhere; anything that shells out to ``osmium-tool`` or
imports ``pyosmium`` is skipped when the optional stack is absent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from cssic.history.osmium import (
    NO_DESCRIPTOR,
    OsmiumHistory,
    WayHistory,
    _collapse_same_day,
    _effective_day,
    available,
    construction_key,
    descriptor_for,
    spans_from_versions,
)
from cssic.store import Workspace

needs_osmium = pytest.mark.skipif(not available(), reason="osmium-tool / pyosmium not installed")


def test_available_is_a_boolean():
    assert isinstance(available(), bool)


def test_descriptor_prefers_a_specific_tag():
    assert descriptor_for({"building": "yes", "amenity": "school"}) == "amenity=school"
    assert descriptor_for({"landuse": "residential", "leisure": "park"}) == "leisure=park"


def test_descriptor_falls_back_to_a_generic_tag():
    assert descriptor_for({"building": "yes"}) == "building=yes"
    assert descriptor_for({"landuse": "residential"}) == "landuse=residential"


def test_descriptor_without_any_known_key():
    assert descriptor_for({"name": "somewhere"}) == NO_DESCRIPTOR
    assert descriptor_for({}) == NO_DESCRIPTOR


def test_construction_key_reads_the_paper_tags():
    assert construction_key({"landuse": "construction"}) == "landuse"
    assert construction_key({"building": "construction"}) == "building"
    # building wins when both are present, as in the v2 handler.
    assert construction_key({"landuse": "construction", "building": "construction"}) == "building"
    assert construction_key({"landuse": "farmland"}) is None


def test_spans_close_the_day_before_the_retag():
    versions = [
        (date(2018, 1, 1), None),
        (date(2018, 2, 10), "landuse"),
        (date(2018, 3, 20), None),
    ]
    assert spans_from_versions(versions, date(2018, 12, 31)) == [
        (date(2018, 2, 10), date(2018, 3, 19), "landuse")
    ]


def test_spans_still_open_run_to_the_window_end():
    versions = [(date(2018, 2, 10), "building")]
    assert spans_from_versions(versions, date(2018, 6, 1)) == [
        (date(2018, 2, 10), date(2018, 6, 1), "building")
    ]


def test_spans_handle_repeated_construction_periods():
    versions = [
        (date(2018, 1, 1), "landuse"),
        (date(2018, 2, 1), None),
        (date(2018, 5, 1), "building"),
        (date(2018, 6, 1), None),
    ]
    assert spans_from_versions(versions, date(2018, 12, 31)) == [
        (date(2018, 1, 1), date(2018, 1, 31), "landuse"),
        (date(2018, 5, 1), date(2018, 5, 31), "building"),
    ]


def test_spans_of_a_single_day_do_not_invert():
    versions = [(date(2018, 2, 10), "landuse"), (date(2018, 2, 10), None)]
    assert spans_from_versions(versions, date(2018, 12, 31)) == [
        (date(2018, 2, 10), date(2018, 2, 10), "landuse")
    ]


def test_paths_are_workspace_relative(tmp_path):
    region = tmp_path / "region.osh.pbf"
    region.write_bytes(b"")
    poly = tmp_path / "aoi.poly"
    poly.write_text("aoi\n1\n 0 0\n 1 0\n 1 1\n 0 0\nEND\nEND\n", encoding="utf-8")
    history = OsmiumHistory(region, poly, workspace=Workspace(tmp_path))

    assert history.poly_history == tmp_path / "temp" / "snapshots" / "outputpoly.osh.pbf"
    assert history.construction_history == tmp_path / "temp" / "snapshots" / "filtered.osh.pbf"


@needs_osmium
def test_prepare_runs_the_v2_osmium_pipeline(tmp_path):
    """``prepare`` clips to the poly, then isolates ever-construction ways."""
    region = tmp_path / "region.osh.pbf"
    region.write_bytes(b"")
    poly = tmp_path / "aoi.poly"
    poly.write_text("aoi\n1\n 0 0\n 1 0\n 1 1\n 0 0\nEND\nEND\n", encoding="utf-8")

    calls: list[tuple[str, ...]] = []
    history = OsmiumHistory(
        region, poly, workspace=Workspace(tmp_path), runner=lambda *args: calls.append(args)
    )
    history.prepare()
    history.prepare()  # idempotent

    assert [call[0] for call in calls] == ["extract", "tags-filter", "getid"]
    assert "--with-history" in calls[0]
    assert "w/landuse=construction" in calls[1]
    assert "--add-referenced" in calls[2]


@needs_osmium
def test_snapshot_extracts_then_time_filters(tmp_path, monkeypatch):
    """A boundary snapshot is a bbox extract, time-filtered to the day."""
    import cssic.history.osmium as osmium_backend

    region = tmp_path / "region.osh.pbf"
    region.write_bytes(b"")
    poly = tmp_path / "aoi.poly"
    poly.write_text("aoi\n1\n 0 0\n 1 0\n 1 1\n 0 0\nEND\nEND\n", encoding="utf-8")

    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        osmium_backend,
        "_area_rows",
        lambda path: [
            {
                "osm_id": "7",
                "element_type": "relation",
                "tag_key": "landuse",
                "tag_value": "residential",
                "descriptor": "landuse=residential",
                "tags": {"landuse": "residential"},
                "geometry": None,
            }
        ],
    )
    history = OsmiumHistory(
        region, poly, workspace=Workspace(tmp_path), runner=lambda *args: calls.append(args)
    )
    history.snapshot_dir.mkdir(parents=True, exist_ok=True)

    frame = history.snapshot(date(2018, 3, 1), (0.0, 0.0, 1.0, 1.0))
    history.snapshot(date(2018, 3, 1), (0.0, 0.0, 1.0, 1.0))  # cached

    assert list(frame.columns) == [
        "osm_id",
        "element_type",
        "tag_key",
        "tag_value",
        "descriptor",
        "geometry",
    ]
    assert list(frame["osm_id"]) == ["relation-7"]
    verbs = [call[0] for call in calls]
    assert verbs == ["extract", "tags-filter", "getid", "extract", "time-filter"]
    assert "2018-03-01T00:00:00Z" in calls[-1]


# --- daily snapshot semantics ----------------------------------------------


def test_an_afternoon_edit_lands_in_the_next_days_snapshot():
    """``time-filter {day}T00:00:00Z`` cannot see an edit made later that day."""
    assert _effective_day(datetime(2018, 2, 10, 14, 30, tzinfo=timezone.utc)) == date(2018, 2, 11)
    assert _effective_day(datetime(2018, 2, 10, 0, 0, tzinfo=timezone.utc)) == date(2018, 2, 10)


def test_two_edits_before_the_same_midnight_collapse_to_the_last_one():
    """A day's snapshot shows the final state, so a same-day retag is invisible."""
    versions = [
        (date(2018, 2, 10), "landuse"),
        (date(2018, 2, 10), None),
        (date(2018, 3, 1), "building"),
    ]

    assert _collapse_same_day(versions) == [
        (date(2018, 2, 10), None),
        (date(2018, 3, 1), "building"),
    ]


def test_collapsing_sorts_versions_by_day():
    versions = [(date(2018, 3, 1), None), (date(2018, 2, 10), "landuse")]

    assert _collapse_same_day(versions) == [
        (date(2018, 2, 10), "landuse"),
        (date(2018, 3, 1), None),
    ]


# --- per-change-day geometry ------------------------------------------------


def geometry_row(osm_id, geometry):
    return {
        "osm_id": osm_id,
        "element_type": "way",
        "descriptor": "landuse=construction",
        "tags": {"landuse": "construction"},
        "geometry": geometry,
    }


def stub_history(tmp_path, monkeypatch, way_history):
    """An ``OsmiumHistory`` with the osmium stack stubbed out."""
    import cssic.history.osmium as osmium_backend

    monkeypatch.setattr(osmium_backend, "require_osmium", lambda: None)
    monkeypatch.setattr(osmium_backend, "_way_versions", lambda path: {"1": way_history})
    region = tmp_path / "region.osh.pbf"
    region.write_bytes(b"")
    poly = tmp_path / "aoi.poly"
    poly.write_text("aoi\n1\n 0 0\n 1 0\n 1 1\n 0 0\nEND\nEND\n", encoding="utf-8")
    return OsmiumHistory(region, poly, workspace=Workspace(tmp_path), runner=lambda *args: None)


def test_geometry_is_sampled_on_every_day_the_footprint_could_have_moved(tmp_path, monkeypatch):
    """A way re-drawn mid-construction keeps both footprints, not just the first."""
    from shapely.geometry import box

    redrawn = date(2018, 3, 1)
    history = stub_history(
        tmp_path,
        monkeypatch,
        WayHistory(
            versions=[(date(2018, 2, 10), "landuse"), (date(2018, 3, 20), None)],
            change_days={date(2018, 2, 10), redrawn},
        ),
    )
    asked: list[date] = []

    def geometries(day):
        asked.append(day)
        shape = box(0, 0, 1, 1) if day < redrawn else box(0, 0, 2, 1)
        return {"1": geometry_row("1", shape)}

    history._construction_geometries = geometries

    intervals = history.construction_intervals(
        (-1.0, -1.0, 3.0, 3.0), (date(2018, 1, 1), date(2018, 12, 31))
    )

    # Only the days an edit could have moved the footprint are snapshotted.
    assert asked == [date(2018, 2, 10), redrawn]
    span = intervals[0]
    assert span.valid_from == date(2018, 2, 10)
    assert span.valid_to == date(2018, 3, 19)
    assert [(v.valid_from, v.valid_to) for v in span.versions] == [
        (date(2018, 2, 10), redrawn - timedelta(days=1)),
        (redrawn, date(2018, 3, 19)),
    ]
    assert span.geometry_on(date(2018, 2, 15)).bounds == (0.0, 0.0, 1.0, 1.0)
    assert span.geometry_on(date(2018, 3, 15)).bounds == (0.0, 0.0, 2.0, 1.0)
    # The interval geometry is the union: the chain's whole footprint.
    assert span.geometry.bounds == (0.0, 0.0, 2.0, 1.0)


def test_an_unchanged_footprint_stays_one_version(tmp_path, monkeypatch):
    """Sampling a day the way did not really move must not split the interval."""
    from shapely.geometry import box

    history = stub_history(
        tmp_path,
        monkeypatch,
        WayHistory(
            versions=[(date(2018, 2, 10), "landuse"), (date(2018, 3, 20), None)],
            change_days={date(2018, 2, 10), date(2018, 3, 1)},
        ),
    )
    history._construction_geometries = lambda day: {"1": geometry_row("1", box(0, 0, 1, 1))}

    intervals = history.construction_intervals(
        (-1.0, -1.0, 3.0, 3.0), (date(2018, 1, 1), date(2018, 12, 31))
    )

    span = intervals[0]
    assert len(span.versions) == 1
    assert span.versions[0].valid_from == date(2018, 2, 10)
    assert span.versions[0].valid_to == date(2018, 3, 19)
    # Same id form as the ohsome backend, so both produce identical chain ids
    # (live: osmium wrote ``693909168-5`` where ohsome wrote ``way-693909168-5``).
    assert span.osm_id == "way-1"


def test_a_way_whose_footprint_misses_the_bbox_is_dropped(tmp_path, monkeypatch):
    from shapely.geometry import box

    history = stub_history(
        tmp_path,
        monkeypatch,
        WayHistory(
            versions=[(date(2018, 2, 10), "landuse"), (date(2018, 3, 20), None)],
            change_days={date(2018, 2, 10)},
        ),
    )
    history._construction_geometries = lambda day: {"1": geometry_row("1", box(50, 50, 51, 51))}

    assert (
        history.construction_intervals(
            (-1.0, -1.0, 3.0, 3.0), (date(2018, 1, 1), date(2018, 12, 31))
        )
        == []
    )
