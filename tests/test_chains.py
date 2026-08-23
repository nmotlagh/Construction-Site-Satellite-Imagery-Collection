"""The paper algorithm over synthetic snapshots (no network, no osmium).

Each test builds a :class:`FakeHistory` -- a hand-written
:class:`~cssic.history.base.HistorySource` -- and checks one behaviour of
:func:`cssic.chains.build_chains`: backward start recovery, forward end
recovery, boundary tags by IOU confidence, ``NO TAG FOUND`` on a weak overlap,
chain linking across an OSM id change, and a chain still in progress at the end
of the window.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from shapely.geometry import box

from cssic.chains import (
    FINAL_TAG,
    FORWARD_LAG_DAYS,
    SEARCH_FLOOR,
    UNKNOWN_TAG,
    _ConstructionIndex,
    _Context,
    build_chains,
    generate_boundary_tags,
)
from cssic.config import NO_TAG, ExtractConfig
from cssic.history.base import GeometryVersion, Interval
from cssic.sites import Site, SiteCollection

DAY = timedelta(days=1)
BBOX = (-1.0, -1.0, 3.0, 3.0)
# The site footprint every test uses, and a few neighbours around it.
SITE = box(0.0, 0.0, 1.0, 1.0)
EXACT_MATCH = box(0.0, 0.0, 1.0, 1.0)
ENCLOSING = box(-1.0, -1.0, 2.0, 2.0)  # IOU 1/9 with SITE
CORNER = box(0.8, 0.8, 2.0, 2.0)  # IOU ~0.017 with SITE
HALF_OVERLAP = box(0.0, 0.0, 0.5, 1.0)  # IOU exactly 0.5 with SITE
TALLER = box(0.0, 0.0, 1.0, 1.2)  # IOU 1/1.2 with SITE
WIDER = box(0.0, 0.0, 1.4, 1.0)  # IOU 1/1.4 with SITE


@pytest.fixture
def poly_file(tmp_path):
    path = tmp_path / "aoi.poly"
    path.write_text(
        "aoi\n1\n   -1.0  -1.0\n   3.0  -1.0\n   3.0  3.0\n   -1.0  3.0\n   -1.0  -1.0\nEND\nEND\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def triangle_poly_file(tmp_path):
    """A non-rectangular region: the half of :data:`BBOX` below ``x + y = 2``."""
    path = tmp_path / "triangle.poly"
    path.write_text(
        "aoi\n1\n   -1.0  -1.0\n   3.0  -1.0\n   -1.0  3.0\n   -1.0  -1.0\nEND\nEND\n",
        encoding="utf-8",
    )
    return path


class FakeHistory:
    """A synthetic ``HistorySource``: fixed intervals and per-day tagged polygons."""

    def __init__(self, intervals, snapshots=None):
        self.intervals = list(intervals)
        self.snapshots = dict(snapshots or {})
        self.snapshot_calls: list[date] = []

    def construction_intervals(self, bbox, window):
        start, end = window
        return [i for i in self.intervals if i.valid_to >= start and i.valid_from <= end]

    def snapshot(self, day, bbox):
        self.snapshot_calls.append(day)
        clip = box(*bbox)
        return [row for row in self.snapshots.get(day, []) if row["geometry"].intersects(clip)]


def interval(osm_id, valid_from, valid_to, geometry=SITE, constr_tag="landuse"):
    return Interval(
        osm_id=str(osm_id),
        valid_from=valid_from,
        valid_to=valid_to,
        geometry=geometry,
        constr_tag=constr_tag,
    )


def tagged(osm_id, key, value, geometry, element_type="way"):
    return {
        "osm_id": str(osm_id),
        "tag_key": key,
        "tag_value": value,
        "geometry": geometry,
        "element_type": element_type,
    }


def config(poly_file, start, end, **kwargs):
    return ExtractConfig(start=start, end=end, poly=poly_file, **kwargs)


def only(collection):
    """The single chain in a collection (asserting there is exactly one)."""
    assert len(collection) == 1, f"expected one chain, got {list(collection.keys())}"
    return next(iter(collection.values()))


# ---------------------------------------------------------------------------


def test_backward_fill_recovers_true_start_and_previous_tag(poly_file):
    """The start predates the window; the previous tag comes from the day before it."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    true_start = date(2018, 2, 10)
    history = FakeHistory(
        intervals=[interval(1, true_start, date(2018, 3, 20))],
        snapshots={
            true_start - DAY: [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            date(2018, 3, 21): [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(wip) == 0
    site = only(completed)
    assert site.start == true_start
    assert site.prev_tag == "landuse=farmland"
    assert site.constr_tag == "landuse"


def test_forward_fill_recovers_true_end_and_final_tag(poly_file):
    """The end follows the window; the final tag comes from the day after it."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    true_end = date(2018, 3, 20)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), true_end)],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            true_end + DAY: [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.end == true_end
    assert site.final_tag == "building=retail"


def test_boundary_tags_rank_candidates_by_iou_confidence(poly_file):
    """A tight overlap beats a large enclosing polygon for both boundary tags."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 20))],
        snapshots={
            date(2018, 2, 9): [
                tagged("district", "landuse", "residential", ENCLOSING),
                tagged("farm", "landuse", "farmland", EXACT_MATCH),
            ],
            date(2018, 3, 21): [
                tagged("district", "landuse", "residential", ENCLOSING),
                tagged("mall", "building", "retail", EXACT_MATCH),
            ],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.prev_tag == "landuse=farmland"
    assert site.final_tag == "building=retail"


def test_weak_overlap_yields_no_tag_found(poly_file):
    """Nothing over ``tag_confidence`` overlaps the site, so the tags stay unknown."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 20))],
        snapshots={
            date(2018, 2, 9): [tagged("corner", "landuse", "farmland", CORNER)],
            date(2018, 3, 21): [tagged("corner", "landuse", "farmland", CORNER)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.prev_tag == NO_TAG
    assert site.final_tag == NO_TAG


def test_low_tag_confidence_accepts_the_weak_overlap(poly_file):
    """The threshold is configuration, not a constant: lower it and the tag lands."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 20))],
        snapshots={
            date(2018, 2, 9): [tagged("corner", "landuse", "farmland", CORNER)],
            date(2018, 3, 21): [tagged("corner", "landuse", "farmland", CORNER)],
        },
    )
    cfg = config(poly_file, start, end, tag_confidence=0.01)

    completed, _wip = build_chains(history, cfg, bbox=BBOX)

    assert only(completed).prev_tag == "landuse=farmland"


def test_chain_links_across_an_id_change(poly_file):
    """One site re-drawn under a new way id stays a single construction chain."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    swap_day = date(2018, 3, 6)
    true_end = date(2018, 3, 20)
    history = FakeHistory(
        intervals=[
            interval(1, date(2018, 2, 10), swap_day - DAY),
            interval(2, swap_day, true_end),
        ],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            # The day way 1 disappears, way 2 is there as construction on the
            # same footprint: IOU 1.0 > construction_chain_confidence.
            swap_day: [tagged(2, "landuse", "construction", EXACT_MATCH)],
            true_end + DAY: [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(wip) == 0
    site = only(completed)
    assert site.ids == ["1", "2"]
    assert next(iter(completed.keys())).startswith("1_2-")
    assert site.start == date(2018, 2, 10)
    assert site.end == true_end
    assert site.prev_tag == "landuse=farmland"
    # A boundary tag is never literally construction: the link consumed it.
    assert site.final_tag == "building=retail"


def test_a_relation_is_never_chain_linked_even_with_a_dashed_id(poly_file):
    """v2 chained ways only; ohsome ids (``relation-3``) must not fool the check."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    swap_day = date(2018, 3, 6)
    history = FakeHistory(
        intervals=[interval("way-1", date(2018, 2, 10), swap_day - DAY)],
        snapshots={
            date(2018, 2, 9): [tagged("way-9", "landuse", "farmland", EXACT_MATCH)],
            # A perfectly overlapping construction *relation*: no element_type
            # column, so the type has to be read off the id itself.
            swap_day: [
                {
                    "osm_id": "relation-3",
                    "tag_key": "landuse",
                    "tag_value": "construction",
                    "geometry": EXACT_MATCH,
                }
            ],
        },
    )

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(wip) == 0
    site = only(completed)
    assert site.ids == ["way-1"]
    assert next(iter(completed.keys())).startswith("way-1-")
    # The relation was rejected as a chain link and, being construction, is not
    # allowed to become a boundary tag either.
    assert site.final_tag == NO_TAG


def test_dashed_chain_link_keeps_both_ids_in_the_key(poly_file):
    """An ohsome-style id change keeps ``way-1_way-2`` in the chain id."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    swap_day = date(2018, 3, 6)
    true_end = date(2018, 3, 20)
    history = FakeHistory(
        intervals=[
            interval("way-1", date(2018, 2, 10), swap_day - DAY),
            interval("way-2", swap_day, true_end),
        ],
        snapshots={
            date(2018, 2, 9): [tagged("way-9", "landuse", "farmland", EXACT_MATCH)],
            swap_day: [tagged("way-2", "landuse", "construction", EXACT_MATCH)],
            true_end + DAY: [tagged("way-8", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.ids == ["way-1", "way-2"]
    assert next(iter(completed.keys())).startswith("way-1_way-2-")


def test_weak_overlap_does_not_link_a_neighbouring_construction_site(poly_file):
    """Below ``construction_chain_confidence`` the neighbour is not the same site."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    swap_day = date(2018, 3, 6)
    history = FakeHistory(
        intervals=[
            interval(1, date(2018, 2, 10), swap_day - DAY),
            interval(2, swap_day, date(2018, 3, 20), geometry=CORNER),
        ],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            swap_day: [tagged(2, "landuse", "construction", CORNER)],
            date(2018, 3, 21): [],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    keys = sorted(completed.keys())
    assert len(keys) == 2, keys
    first = completed.get("1")
    assert first.ids == ["1"]
    assert first.end == swap_day - DAY
    assert first.final_tag == NO_TAG


def test_site_still_under_construction_at_the_window_end_is_wip(poly_file):
    """A chain with no end inside the searched period stays in progress."""
    end = date.today() - timedelta(days=30)
    start = end - timedelta(days=10)
    history = FakeHistory(
        intervals=[interval(1, start - timedelta(days=5), date.today())],
        snapshots={start - timedelta(days=6): [tagged("farm", "landuse", "farmland", SITE)]},
    )

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(completed) == 0
    site = only(wip)
    assert site.start == start - timedelta(days=5)
    assert site.prev_tag == "landuse=farmland"
    assert site.final_tag == "N/A"


def test_restrict_window_does_not_search_outside_the_window(poly_file):
    """``--restrict-window`` keeps the walk inside ``[start, end]``."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 20))],
        snapshots={date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)]},
    )

    completed, wip = build_chains(
        history, config(poly_file, start, end, restrict_window=True), bbox=BBOX
    )

    assert len(completed) == 0
    site = only(wip)
    assert site.start == start
    assert site.prev_tag == "N/A"
    assert history.snapshot_calls == []


def test_new_site_inside_the_window_is_picked_up(poly_file):
    """A site that appears mid-window gets a chain and a previous tag."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    appears = date(2018, 3, 4)
    history = FakeHistory(
        intervals=[interval(7, appears, date(2018, 3, 8))],
        snapshots={
            appears - DAY: [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            date(2018, 3, 9): [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(wip) == 0
    site = only(completed)
    assert site.ids == ["7"]
    assert site.start == appears
    assert site.end == date(2018, 3, 8)
    assert site.prev_tag == "landuse=farmland"
    assert site.final_tag == "building=retail"


def test_snapshots_are_requested_over_the_padded_site_bbox(poly_file):
    """Boundary snapshots ask for the site's bbox plus the 0.001 degree pad."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    seen: list[tuple] = []

    history = FakeHistory(intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 20))])
    original = history.snapshot

    def recording(day, bbox):
        seen.append(bbox)
        return original(day, bbox)

    history.snapshot = recording

    build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert seen
    for minx, miny, maxx, maxy in seen:
        assert minx == pytest.approx(-0.001)
        assert miny == pytest.approx(-0.001)
        assert maxx == pytest.approx(1.001)
        assert maxy == pytest.approx(1.001)


def test_bbox_defaults_to_the_poly_file(poly_file):
    """Without an explicit bbox the polygon's bounds are used."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    seen: list[tuple] = []

    class RecordingHistory(FakeHistory):
        def construction_intervals(self, bbox, window):
            seen.append(bbox)
            return super().construction_intervals(bbox, window)

    history = RecordingHistory(intervals=[])

    build_chains(history, config(poly_file, start, end))

    assert seen == [(-1.0, -1.0, 3.0, 3.0)]


# --- footprint of the day, not of the whole chain ---------------------------

EARLY_FOOTPRINT = box(0.0, 0.0, 1.0, 1.0)
LATE_FOOTPRINT = box(0.5, 0.0, 1.5, 1.0)
BOTH_FOOTPRINTS = box(0.0, 0.0, 1.5, 1.0)


def redrawn_interval(osm_id, valid_from, redrawn_on, valid_to):
    """A site re-drawn mid-construction: two footprints, one union geometry."""
    return Interval(
        osm_id=str(osm_id),
        valid_from=valid_from,
        valid_to=valid_to,
        geometry=BOTH_FOOTPRINTS,
        constr_tag="landuse",
        versions=(
            GeometryVersion(valid_from, redrawn_on - DAY, EARLY_FOOTPRINT),
            GeometryVersion(redrawn_on, valid_to, LATE_FOOTPRINT),
        ),
    )


def test_boundary_tags_are_ranked_against_the_footprint_of_the_boundary_day(poly_file):
    """A site re-drawn mid-construction is IOU-ranked against its shape *then*.

    Ranking against the chain's union footprint instead would pick the polygon
    that covers ground the site only occupied earlier.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[redrawn_interval(1, start, date(2018, 3, 3), date(2018, 3, 5))],
        snapshots={
            date(2018, 2, 28): [tagged("farm", "landuse", "farmland", EARLY_FOOTPRINT)],
            date(2018, 3, 6): [
                tagged("union", "landuse", "residential", BOTH_FOOTPRINTS),
                tagged("mall", "building", "retail", LATE_FOOTPRINT),
            ],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.start == start
    assert site.end == date(2018, 3, 5)
    # ``mall`` matches the 2018-03-05 footprint exactly; ``union`` would only
    # win against the union of both footprints.
    assert site.final_tag == "building=retail"


def test_the_previous_tag_uses_the_first_days_footprint(poly_file):
    """Backwards from the start, the boundary day is ranked against footprint one."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[redrawn_interval(1, start, date(2018, 3, 3), date(2018, 3, 5))],
        snapshots={
            date(2018, 2, 28): [
                tagged("union", "landuse", "residential", BOTH_FOOTPRINTS),
                tagged("farm", "landuse", "farmland", EARLY_FOOTPRINT),
            ],
            date(2018, 3, 6): [tagged("mall", "building", "retail", LATE_FOOTPRINT)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert only(completed).prev_tag == "landuse=farmland"


# --- the forward walk stops where the backend runs out of data --------------


class HorizonHistory(FakeHistory):
    """A ``FakeHistory`` that also reports the last day it has data for."""

    def __init__(self, intervals, snapshots=None, horizon=None):
        super().__init__(intervals, snapshots)
        self.horizon = horizon

    def data_until(self):
        return self.horizon


def _late_end_history(cls, **kwargs):
    """A chain whose construction ends 10 days ago, with a window 30 days back."""
    end = date.today() - timedelta(days=30)
    start = end - timedelta(days=10)
    true_end = date.today() - timedelta(days=10)
    history = cls(
        intervals=[interval(1, start - timedelta(days=5), true_end)],
        snapshots={
            start - timedelta(days=6): [tagged("farm", "landuse", "farmland", SITE)],
            true_end + DAY: [tagged("mall", "building", "retail", SITE)],
        },
        **kwargs,
    )
    return history, start, end, true_end


def test_the_forward_walk_reaches_an_end_a_backend_has_data_for(poly_file):
    """Baseline for the horizon test: without a horizon the end is found."""
    history, start, end, true_end = _late_end_history(HorizonHistory, horizon=date.today())

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(wip) == 0
    assert only(completed).end == true_end
    assert only(completed).final_tag == "building=retail"


def test_the_forward_walk_stops_at_the_backend_data_horizon(poly_file):
    """Past ``data_until`` every snapshot repeats the last real one.

    Walking into that dead zone would make every still-open site look finished
    on the same day, so the chain has to stay in progress instead.
    """
    history, start, end, true_end = _late_end_history(HorizonHistory)
    history.horizon = true_end - timedelta(days=5)

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(completed) == 0
    site = only(wip)
    assert site.final_tag == "N/A"
    assert true_end + DAY not in history.snapshot_calls
    assert max(history.snapshot_calls) <= history.horizon + DAY


# --- v2 tie-breaking --------------------------------------------------------


def test_an_exact_tie_is_broken_in_reverse_snapshot_order(poly_file):
    """v2 sorted ascending and reversed, so the later of two equals wins.

    ``sort(reverse=True)`` would keep the tied pair in snapshot order and pick
    the other tag; this is a faithfulness test, not a preference.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 20))],
        snapshots={
            date(2018, 2, 9): [
                tagged("farm", "landuse", "farmland", EXACT_MATCH),
                tagged("meadow", "landuse", "meadow", EXACT_MATCH),
            ],
            date(2018, 3, 21): [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert only(completed).prev_tag == "landuse=meadow"


# --- the region, not its bounding box ---------------------------------------

INSIDE_POLY = box(0.0, 0.0, 0.5, 0.5)
OUTSIDE_POLY = box(2.5, 2.5, 2.9, 2.9)  # inside BBOX, outside the triangle


def test_a_site_inside_the_bbox_but_outside_the_poly_is_not_collected(triangle_poly_file):
    """Backends are queried by bbox, but the ``.poly`` region decides what counts.

    v2 clipped both ways -- ``osmium extract -p`` and
    ``shape(geom).intersects(polygon)`` -- so a corner of the bounding box that
    the region excludes must not turn into a construction chain.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[
            interval(1, date(2018, 2, 10), date(2018, 3, 5), geometry=INSIDE_POLY),
            interval(2, date(2018, 2, 10), date(2018, 3, 5), geometry=OUTSIDE_POLY),
        ]
    )

    completed, wip = build_chains(history, config(triangle_poly_file, start, end))

    assert len(wip) == 0
    assert only(completed).ids == ["1"]


def test_chain_serials_do_not_depend_on_backend_ordering(poly_file):
    """ohsome yields ids string-sorted, osmium numerically; live, the same 23
    campus chains came back with different serials from the two backends.
    The chain serial must follow one order regardless of who produced it."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    spans = [
        interval("way-1077541390", date(2018, 2, 10), date(2018, 3, 5)),
        interval("way-693909168", date(2018, 2, 10), date(2018, 3, 5)),
        interval("way-770472412", date(2018, 2, 10), date(2018, 3, 5)),
    ]
    ids = []
    for order in (spans, list(reversed(spans)), sorted(spans, key=lambda i: i.osm_id)):
        completed, _wip = build_chains(FakeHistory(intervals=order), config(poly_file, start, end))
        ids.append([site.chain_id for site in completed.values()])

    assert ids[0] == ids[1] == ids[2]
    assert ids[0] == ["way-693909168-0", "way-770472412-1", "way-1077541390-2"]


# --- boundary tags ----------------------------------------------------------


def test_the_way_itself_on_the_boundary_day_supplies_the_tag(poly_file):
    """The commonest real path: the way keeps its id and is simply re-tagged.

    Nothing has to be IOU-ranked then -- the site's own polygon carries what it
    was before, and what it became after, construction.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 5))],
        snapshots={
            date(2018, 2, 9): [tagged(1, "landuse", "farmland", EXACT_MATCH)],
            date(2018, 3, 6): [tagged(1, "building", "apartments", EXACT_MATCH)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.ids == ["1"]  # the way is itself, not a link to a new chain
    assert site.prev_tag == "landuse=farmland"
    assert site.final_tag == "building=apartments"


def test_a_candidate_exactly_at_the_tag_threshold_is_rejected(poly_file):
    """The confidence must be *cleared*, not merely matched (``>``, not ``>=``)."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 5))],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", HALF_OVERLAP)],
            date(2018, 3, 6): [tagged("mall", "building", "retail", HALF_OVERLAP)],
        },
    )

    completed, _wip = build_chains(
        history, config(poly_file, start, end, tag_confidence=0.5), bbox=BBOX
    )

    site = only(completed)
    assert site.prev_tag == NO_TAG
    assert site.final_tag == NO_TAG


def test_a_neighbour_exactly_at_the_chain_threshold_is_not_linked(poly_file):
    """Same strictness for chain linking: a half-overlap is a different site."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    swap_day = date(2018, 3, 6)
    history = FakeHistory(
        intervals=[
            interval(1, date(2018, 2, 10), swap_day - DAY),
            interval(2, swap_day, date(2018, 3, 20), geometry=HALF_OVERLAP),
        ],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            swap_day: [tagged(2, "landuse", "construction", HALF_OVERLAP)],
            date(2018, 3, 21): [],
        },
    )

    completed, _wip = build_chains(
        history, config(poly_file, start, end, construction_chain_confidence=0.5), bbox=BBOX
    )

    assert sorted(site.ids for site in completed.values()) == [["1"], ["2"]]


def test_a_way_already_on_a_chain_is_never_re_used(poly_file):
    """Two neighbouring sites stay two chains: a known way is not a candidate."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[
            interval(1, date(2018, 2, 10), date(2018, 3, 5)),
            interval(2, date(2018, 2, 10), date(2018, 3, 20)),
        ],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            # Way 2 is still construction here and covers way 1 exactly, but it
            # already has a chain of its own, so it is neither link nor tag.
            date(2018, 3, 6): [tagged(2, "landuse", "construction", EXACT_MATCH)],
            date(2018, 3, 21): [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(wip) == 0
    assert sorted(site.ids for site in completed.values()) == [["1"], ["2"]]
    assert completed.get("1").final_tag == NO_TAG


# --- the chain footprint ----------------------------------------------------


def test_a_chain_link_grows_the_chain_footprint(poly_file):
    """A linked way brings its own ground into the chain.

    The chain's bounds are what ``info.txt`` and the imagery AOI are cut from,
    so they have to cover every footprint the chain ever had.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    swap_day = date(2018, 3, 6)
    true_end = date(2018, 3, 20)
    history = FakeHistory(
        intervals=[
            interval(1, date(2018, 2, 10), swap_day - DAY),
            interval(2, swap_day, true_end, geometry=TALLER),
        ],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            swap_day: [tagged(2, "landuse", "construction", TALLER)],
            true_end + DAY: [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.ids == ["1", "2"]
    assert site.bounds == (0.0, 0.0, 1.0, 1.2)


def test_an_accepted_boundary_tag_grows_the_chain_footprint(poly_file):
    """The polygon a site turned into is part of the site, so it joins the union."""
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 5))],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            date(2018, 3, 6): [tagged("mall", "building", "retail", WIDER)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.final_tag == "building=retail"
    assert site.bounds == (0.0, 0.0, 1.4, 1.0)


TALL_FOOTPRINT = box(0.0, 0.0, 1.0, 1.5)


def test_the_backward_walk_unions_the_older_ways_whole_footprint(poly_file):
    """Walking back over a linked way collects every footprint it had.

    Way 1 was re-drawn while under construction; the snapshot that linked it
    only shows its *last* shape, so the earlier, taller one can enter the chain
    only through the backward walk.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    older = Interval(
        osm_id="1",
        valid_from=date(2018, 2, 20),
        valid_to=date(2018, 2, 28),
        geometry=TALL_FOOTPRINT,
        constr_tag="landuse",
        versions=(
            GeometryVersion(date(2018, 2, 20), date(2018, 2, 23), TALL_FOOTPRINT),
            GeometryVersion(date(2018, 2, 24), date(2018, 2, 28), SITE),
        ),
    )
    history = FakeHistory(
        intervals=[older, interval(2, start, date(2018, 3, 20))],
        snapshots={
            date(2018, 2, 19): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            date(2018, 2, 28): [tagged(1, "landuse", "construction", SITE)],
            date(2018, 3, 21): [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.ids == ["1", "2"]
    assert site.start == date(2018, 2, 20)
    assert site.bounds == (0.0, 0.0, 1.0, 1.5)


# --- a new site that continues an older chain -------------------------------


def test_a_new_site_walks_back_through_the_chain_it_links_to(poly_file):
    """A mid-window site linking to an older way inherits the whole chain's start.

    Way 8's chain finished (and moved to ``completed``) on the same day way 7
    appeared, which is what makes way 8 available as a link target again; the
    backward walk then has to keep going all the way to way 9's true start.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[
            interval(9, date(2018, 2, 10), date(2018, 2, 20)),
            interval(8, date(2018, 2, 21), date(2018, 3, 3)),
            interval(7, date(2018, 3, 4), date(2018, 3, 12)),
        ],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            date(2018, 2, 20): [tagged(9, "landuse", "construction", EXACT_MATCH)],
            date(2018, 3, 3): [tagged(8, "landuse", "construction", EXACT_MATCH)],
            date(2018, 3, 4): [tagged(8, "building", "retail", EXACT_MATCH)],
            date(2018, 3, 13): [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(wip) == 0
    chains = {tuple(site.ids): site for site in completed.values()}
    assert set(chains) == {("9", "8"), ("9", "8", "7")}
    relinked = chains[("9", "8", "7")]
    assert relinked.start == date(2018, 2, 10)
    assert relinked.end == date(2018, 3, 12)
    assert relinked.prev_tag == "landuse=farmland"


# --- how far the walks reach ------------------------------------------------


def test_the_interval_query_covers_the_whole_searchable_range(poly_file):
    """Without ``--restrict-window`` the backend is asked for everything.

    The true start may predate the window by years and the true end may follow
    it, so the query runs from the 2010 floor to the seven-day lag.
    """
    seen: list[tuple] = []

    class RecordingHistory(FakeHistory):
        def construction_intervals(self, bbox, window):
            seen.append(window)
            return super().construction_intervals(bbox, window)

    start, end = date(2018, 3, 1), date(2018, 3, 10)

    build_chains(RecordingHistory(intervals=[]), config(poly_file, start, end), bbox=BBOX)
    assert seen == [(SEARCH_FLOOR, date.today() - timedelta(days=FORWARD_LAG_DAYS))]

    seen.clear()
    build_chains(
        RecordingHistory(intervals=[]),
        config(poly_file, start, end, restrict_window=True),
        bbox=BBOX,
    )
    assert seen == [(start, end)]


def test_the_forward_walk_stops_one_day_short_of_the_seven_day_lag(poly_file):
    """v2's ceiling: an end date read inside ``today - 7`` is not an end date.

    A way still tagged construction that close to today may simply not have been
    edited yet, so the chain stays in progress instead of being called finished.
    """
    last_construction_day = date.today() - timedelta(days=FORWARD_LAG_DAYS + 1)
    end = date.today() - timedelta(days=20)
    start = end - timedelta(days=10)
    history = FakeHistory(
        intervals=[interval(1, start - timedelta(days=5), last_construction_day)],
        snapshots={start - timedelta(days=6): [tagged("farm", "landuse", "farmland", SITE)]},
    )

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(completed) == 0
    assert only(wip).final_tag == UNKNOWN_TAG


def test_the_forward_walk_stops_one_day_past_the_last_construction_day(poly_file):
    """Beyond the last day the intervals cover there is nothing left to read.

    The walk resolves ends on that one extra day and then stops, even when a
    chain link made there would otherwise keep it marching forward.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    last_day = date(2018, 3, 20)
    history = FakeHistory(
        intervals=[interval(1, date(2018, 2, 10), last_day)],
        snapshots={
            date(2018, 2, 9): [tagged("farm", "landuse", "farmland", EXACT_MATCH)],
            last_day + DAY: [tagged(2, "landuse", "construction", EXACT_MATCH)],
            last_day + 2 * DAY: [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    assert len(completed) == 0
    site = only(wip)
    assert site.ids == ["1", "2"]
    assert site.final_tag == UNKNOWN_TAG


# --- reproducibility --------------------------------------------------------


def test_two_identical_runs_produce_the_same_chain_ids(poly_file):
    """Chain serials belong to the collection, not the process.

    A notebook that calls ``build_chains`` twice must get ``1-0`` both times,
    not ``1-0`` and then ``1-1``.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)

    def run():
        history = FakeHistory(
            intervals=[interval(1, date(2018, 2, 10), date(2018, 3, 5))],
            snapshots={date(2018, 3, 6): [tagged("mall", "building", "retail", EXACT_MATCH)]},
        )
        completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)
        return sorted(completed.keys())

    assert run() == ["1-0"]
    assert run() == ["1-0"]


# --- footprints when the boundary day is not a construction day -------------


def test_nearest_geometry_uses_the_footprint_of_the_closest_construction_day():
    """Outside the construction span the index falls back to the nearest day.

    A day before the span gets the first footprint, a day after it gets the
    last, and a day inside the span is simply looked up.
    """
    index = _ConstructionIndex(
        [redrawn_interval(1, date(2018, 3, 1), date(2018, 3, 4), date(2018, 3, 10))]
    )

    assert index.nearest_geometry(date(2018, 2, 1), 1).equals(EARLY_FOOTPRINT)
    assert index.nearest_geometry(date(2018, 4, 1), 1).equals(LATE_FOOTPRINT)
    assert index.nearest_geometry(date(2018, 3, 2), 1).equals(EARLY_FOOTPRINT)
    assert index.nearest_geometry(date(2018, 3, 2), 99) is None


def test_a_boundary_day_outside_the_construction_span_uses_the_nearest_footprint(poly_file):
    """Without the fallback the tag would be lost, not merely ranked differently.

    ``generate_boundary_tags`` is called here with a day well past the end of
    the construction span, which is what the walks' clamps normally prevent.
    """
    ctx = _Context(
        history=FakeHistory(
            intervals=[],
            snapshots={date(2018, 3, 1): [tagged("mall", "building", "retail", EXACT_MATCH)]},
        ),
        cfg=config(poly_file, date(2018, 1, 1), date(2018, 2, 1)),
        bbox=BBOX,
        index=_ConstructionIndex([interval(1, date(2018, 2, 1), date(2018, 2, 20))]),
    )
    sites = SiteCollection()
    sites.add(Site("1", prev_tag=UNKNOWN_TAG, final_tag=UNKNOWN_TAG, geometry=SITE))

    complete, incomplete = generate_boundary_tags(
        ctx, date(2018, 3, 1), ["1"], sites, mode=FINAL_TAG
    )

    assert (complete, incomplete) == (["1"], [])
    assert sites.get("1").final_tag == "building=retail"


GAP_FOOTPRINT = box(0.0, 0.0, 1.0, 2.0)


def test_the_previous_tag_is_ranked_against_the_first_construction_day(poly_file):
    """Mode 0 ranks against ``current_date + 1`` -- the first day of construction.

    The site was under construction once before, with a different footprint,
    and the walk stops on the day between the two spans; ranking against that
    day instead would fall back to the *older* footprint and pick the wrong tag.
    """
    start, end = date(2018, 3, 1), date(2018, 3, 10)
    history = FakeHistory(
        intervals=[
            interval(1, date(2018, 2, 1), date(2018, 2, 20), geometry=GAP_FOOTPRINT),
            interval(1, date(2018, 2, 22), date(2018, 3, 20)),
        ],
        snapshots={
            date(2018, 2, 21): [
                tagged("farm", "landuse", "farmland", EXACT_MATCH),
                tagged("meadow", "landuse", "meadow", GAP_FOOTPRINT),
            ],
            date(2018, 3, 21): [tagged("mall", "building", "retail", EXACT_MATCH)],
        },
    )

    completed, _wip = build_chains(history, config(poly_file, start, end), bbox=BBOX)

    site = only(completed)
    assert site.start == date(2018, 2, 22)
    assert site.prev_tag == "landuse=farmland"
