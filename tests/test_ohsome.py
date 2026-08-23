"""ohsome backend tests. No network: a fake session replays recorded JSON."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from shapely.geometry import box

from cssic.history.base import Interval
from cssic.history.ohsome import (
    CONSTRUCTION_FILTER,
    OhsomeError,
    OhsomeHistory,
    descriptive_tag,
    features_valid_on,
    intervals_from_features,
    split_bbox,
)


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {"features": []}
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Records posts and answers them from a queue or a callable."""

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, dict(data or {})))
        if self.handler is not None:
            return self.handler(url, dict(data or {}))
        if not self.responses:
            return FakeResponse()
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _poly(minx, miny, maxx, maxy):
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [minx, miny],
                [maxx, miny],
                [maxx, maxy],
                [minx, maxy],
                [minx, miny],
            ]
        ],
    }


def _version(osm_id, valid_from, valid_to, bounds=(0, 0, 1, 1), **tags):
    props = {"@osmId": osm_id, "@validFrom": valid_from, "@validTo": valid_to}
    props.update(tags)
    return {"type": "Feature", "geometry": _poly(*bounds), "properties": props}


HISTORY_FEATURES = [
    _version(
        "way/1",
        "2018-01-01T00:00:00Z",
        "2018-03-01T00:00:00Z",
        (0, 0, 1, 1),
        landuse="construction",
    ),
    _version(
        "way/1",
        "2018-03-01T00:00:00Z",
        "2018-06-01T00:00:00Z",
        (0, 0, 2, 2),
        landuse="construction",
    ),
    _version(
        "way/2",
        "2018-04-01T00:00:00Z",
        "2020-07-01T00:00:00Z",
        (10, 10, 11, 11),
        building="construction",
    ),
]


def _sleepless(seconds):  # pragma: no cover - trivial
    return None


# -- pure helpers ---------------------------------------------------------


def test_intervals_from_features_merges_contiguous_versions():
    intervals = intervals_from_features(HISTORY_FEATURES, window_end=date(2020, 7, 1))
    assert [i.osm_id for i in intervals] == ["way-1", "way-2"]
    first = intervals[0]
    assert isinstance(first, Interval)
    assert first.valid_from == date(2018, 1, 1)
    # @validTo is the instant the next version replaced it: the last day the
    # midnight snapshot still showed construction is the day before.
    assert first.valid_to == date(2018, 5, 31)
    assert first.constr_tag == "landuse"
    assert first.metadata["versions"] == 2
    assert first.metadata["open"] is False
    # the merged geometry is the union of both versions
    assert first.geometry.bounds == (0.0, 0.0, 2.0, 2.0)
    second = intervals[1]
    assert second.constr_tag == "building"
    assert second.metadata["open"] is True


def test_intervals_from_features_splits_on_a_gap():
    features = [
        _version("way/9", "2018-01-01T00:00:00Z", "2018-02-01T00:00:00Z", landuse="construction"),
        _version("way/9", "2019-01-01T00:00:00Z", "2019-04-01T00:00:00Z", landuse="construction"),
    ]
    intervals = intervals_from_features(features, window_end=date(2020, 1, 1))
    assert len(intervals) == 2
    assert [i.metadata["interval_index"] for i in intervals] == [0, 1]
    assert intervals[1].valid_from == date(2019, 1, 1)


def test_intervals_skip_features_without_id_or_geometry():
    features = [
        {"geometry": _poly(0, 0, 1, 1), "properties": {"@validFrom": "x", "@validTo": "y"}},
        {
            "geometry": None,
            "properties": {
                "@osmId": "way/3",
                "@validFrom": "2018-01-01T00:00:00Z",
                "@validTo": "2018-02-01T00:00:00Z",
            },
        },
    ]
    assert intervals_from_features(features) == []


def test_only_ways_become_construction_intervals():
    """A construction *relation* is not a site.

    The paper, v2 (``osmium`` ``ways_list``) and the osmium backend all seed
    chains from ways alone; the ohsome filter asks for ``type:way``, and an id
    that says otherwise is dropped here too rather than trusted.
    """
    features = [
        _version(
            "relation/7", "2018-01-01T00:00:00Z", "2018-03-01T00:00:00Z", landuse="construction"
        ),
        _version("node/8", "2018-01-01T00:00:00Z", "2018-03-01T00:00:00Z", landuse="construction"),
        _version("way/9", "2018-01-01T00:00:00Z", "2018-03-01T00:00:00Z", landuse="construction"),
    ]

    intervals = intervals_from_features(features, window_end=date(2020, 1, 1))

    assert [i.osm_id for i in intervals] == ["way-9"]
    assert "type:way" in CONSTRUCTION_FILTER


def test_an_untyped_id_is_trusted_but_an_osm_type_of_relation_is_not():
    """``@osmType`` wins over the id: some responses carry bare numeric ids."""
    bare = _version("12", "2018-01-01T00:00:00Z", "2018-03-01T00:00:00Z", landuse="construction")
    typed = _version("13", "2018-01-01T00:00:00Z", "2018-03-01T00:00:00Z", landuse="construction")
    typed["properties"]["@osmType"] = "RELATION"

    intervals = intervals_from_features([bare, typed], window_end=date(2020, 1, 1))

    assert [i.osm_id for i in intervals] == ["12"]


def test_overlapping_versions_are_trimmed_so_each_day_has_one_footprint():
    """ohsome versions can overlap; a day must still resolve to one shape.

    Without trimming the earlier version, ``geometry_on`` for a day inside the
    overlap returns the footprint the way had *before* it was re-drawn.
    """
    features = [
        _version(
            "way/1",
            "2018-01-01T00:00:00Z",
            "2018-04-01T00:00:00Z",
            (0, 0, 1, 1),
            landuse="construction",
        ),
        _version(
            "way/1",
            "2018-02-01T00:00:00Z",
            "2018-06-01T00:00:00Z",
            (0, 0, 2, 2),
            landuse="construction",
        ),
    ]

    span = intervals_from_features(features, window_end=date(2020, 1, 1))[0]

    assert [(v.valid_from, v.valid_to) for v in span.versions] == [
        (date(2018, 1, 1), date(2018, 1, 31)),
        (date(2018, 2, 1), date(2018, 5, 31)),
    ]
    assert span.versions[0].valid_to == span.versions[1].valid_from - timedelta(days=1)
    assert span.geometry_on(date(2018, 3, 1)).bounds == (0.0, 0.0, 2.0, 2.0)


def test_descriptive_tag_prefers_informative_tags():
    assert descriptive_tag({"building": "yes", "leisure": "park"}) == ("leisure", "park")
    assert descriptive_tag({"building": "yes"}) == ("building", "yes")
    assert descriptive_tag({"landuse": "residential"}) == ("landuse", "residential")
    assert descriptive_tag({"landuse": "farmland"}) == ("landuse", "farmland")
    assert descriptive_tag({"name": "nowhere"}) is None


def test_split_bbox_quadrants_and_degenerate():
    assert len(split_bbox((0, 0, 2, 2))) == 4
    assert split_bbox((0, 0, 2, 2))[0] == (0.0, 0.0, 1.0, 1.0)
    assert split_bbox((1, 1, 1, 1)) == [(1, 1, 1, 1)]


# -- construction_intervals ----------------------------------------------


def test_construction_intervals_posts_expected_payload():
    session = FakeSession([FakeResponse(body={"features": HISTORY_FEATURES})])
    history = OhsomeHistory(client=session, base_url="https://example.test/v1")
    intervals = history.construction_intervals(
        (-84.0, 39.0, -83.0, 40.0), (date(2018, 1, 1), date(2020, 7, 1))
    )
    assert len(intervals) == 2
    url, payload = session.calls[0]
    assert url == "https://example.test/v1/elementsFullHistory/geometry"
    assert payload["bboxes"] == "-84.0,39.0,-83.0,40.0"
    assert payload["time"] == "2018-01-01,2020-07-01"
    assert "landuse=construction" in payload["filter"]
    assert "building=construction" in payload["filter"]
    assert payload["clipGeometry"] == "false"


def test_413_splits_the_bbox_into_quadrants_and_dedupes():
    def handler(url, payload):
        if payload["bboxes"] == "0.0,0.0,2.0,2.0":
            return FakeResponse(status_code=413)
        # every quadrant sees the same element straddling the tiles
        return FakeResponse(body={"features": HISTORY_FEATURES[:1]})

    session = FakeSession(handler=handler)
    history = OhsomeHistory(client=session, sleep=_sleepless)
    intervals = history.construction_intervals(
        (0.0, 0.0, 2.0, 2.0), (date(2018, 1, 1), date(2019, 1, 1))
    )
    assert len(session.calls) == 5  # the original plus four quadrants
    assert len(intervals) == 1


def test_413_that_cannot_be_split_further_raises():
    session = FakeSession(handler=lambda url, payload: FakeResponse(status_code=413))
    history = OhsomeHistory(client=session, max_tile_depth=0, sleep=_sleepless)
    with pytest.raises(OhsomeError):
        history.construction_intervals((0.0, 0.0, 2.0, 2.0), (date(2018, 1, 1), date(2019, 1, 1)))


# -- retries --------------------------------------------------------------


def test_rate_limit_is_retried_then_succeeds():
    slept: list[float] = []
    session = FakeSession(
        [
            FakeResponse(status_code=429, headers={"Retry-After": "2"}),
            FakeResponse(status_code=503),
            FakeResponse(body={"features": HISTORY_FEATURES[:1]}),
        ]
    )
    history = OhsomeHistory(client=session, backoff=0.5, sleep=slept.append)
    intervals = history.construction_intervals((0, 0, 1, 1), (date(2018, 1, 1), date(2019, 1, 1)))
    assert len(intervals) == 1
    assert len(session.calls) == 3
    assert slept == [2.0, 1.0]  # Retry-After honoured, then exponential backoff


def test_persistent_server_error_raises_after_max_retries():
    session = FakeSession(handler=lambda url, payload: FakeResponse(status_code=503))
    history = OhsomeHistory(client=session, max_retries=2, sleep=_sleepless)
    with pytest.raises(OhsomeError, match="HTTP 503"):
        history.construction_intervals((0, 0, 1, 1), (date(2018, 1, 1), date(2019, 1, 1)))
    assert len(session.calls) == 3


def test_timeout_exception_is_retried():
    session = FakeSession([TimeoutError("slow"), FakeResponse(body={"features": []})])
    history = OhsomeHistory(client=session, sleep=_sleepless)
    assert history.construction_intervals((0, 0, 1, 1), (date(2018, 1, 1), date(2019, 1, 1))) == []
    assert len(session.calls) == 2


def test_error_object_inside_a_200_body_raises():
    session = FakeSession(
        [FakeResponse(body={"type": "FeatureCollection", "error": "bad filter", "status": 400})]
    )
    history = OhsomeHistory(client=session, sleep=_sleepless)
    with pytest.raises(OhsomeError, match="bad filter"):
        history.construction_intervals((0, 0, 1, 1), (date(2018, 1, 1), date(2019, 1, 1)))


# -- snapshots ------------------------------------------------------------


SNAPSHOT_FEATURES = [
    {
        "geometry": _poly(0, 0, 1, 1),
        "properties": {"@osmId": "way/10", "building": "yes", "leisure": "park"},
    },
    {
        "geometry": _poly(2, 2, 3, 3),
        "properties": {"@osmId": "way/11", "landuse": "farmland"},
    },
    {
        "geometry": _poly(4, 4, 5, 5),
        "properties": {"@osmId": "way/12", "name": "untagged"},
    },
]


def test_snapshot_frame_columns_and_tag_choice():
    session = FakeSession([FakeResponse(body={"features": SNAPSHOT_FEATURES})])
    history = OhsomeHistory(client=session, base_url="https://example.test/v1")
    frame = history.snapshot(date(2019, 5, 4), (0, 0, 5, 5))
    assert list(frame.columns) == ["osm_id", "element_type", "tag_key", "tag_value", "geometry"]
    assert str(frame.crs) == "EPSG:4326"
    assert list(frame["osm_id"]) == ["way-10", "way-11"]  # the untagged way is dropped
    assert list(frame["tag_value"]) == ["park", "farmland"]
    url, payload = session.calls[0]
    assert url == "https://example.test/v1/elements/geometry"
    assert payload["time"] == "2019-05-04"
    assert payload["filter"].startswith("(landuse=*")
    # every key tag a boundary tag may come from, not just landuse/building
    assert "leisure=*" in payload["filter"] and "amenity=*" in payload["filter"]
    assert payload["filter"].endswith("and geometry:polygon")


def test_snapshot_is_memoised_per_day_and_bbox():
    session = FakeSession(
        handler=lambda url, payload: FakeResponse(body={"features": SNAPSHOT_FEATURES})
    )
    history = OhsomeHistory(client=session)
    first = history.snapshot(date(2019, 5, 4), (0, 0, 5, 5))
    second = history.snapshot(date(2019, 5, 4), (0, 0, 5, 5))
    assert second is first
    assert len(session.calls) == 1
    history.snapshot(date(2019, 5, 5), (0, 0, 5, 5))
    history.snapshot(date(2019, 5, 4), (0, 0, 6, 6))
    assert len(session.calls) == 3


def test_snapshot_disk_cache_is_reused_by_a_new_instance(tmp_path):
    session = FakeSession([FakeResponse(body={"features": SNAPSHOT_FEATURES})])
    history = OhsomeHistory(client=session, cache_dir=tmp_path)
    history.snapshot(date(2019, 5, 4), (0, 0, 5, 5))
    assert len(list(tmp_path.glob("snapshot_2019-05-04_*.geojson"))) == 1

    cold_session = FakeSession(handler=lambda url, payload: pytest.fail("hit the network"))
    reloaded = OhsomeHistory(client=cold_session, cache_dir=tmp_path)
    frame = reloaded.snapshot(date(2019, 5, 4), (0, 0, 5, 5))
    assert list(frame["osm_id"]) == ["way-10", "way-11"]
    assert cold_session.calls == []


def test_snapshot_empty_result_is_an_empty_frame():
    session = FakeSession([FakeResponse(body={"features": []})])
    history = OhsomeHistory(client=session)
    frame = history.snapshot(date(2019, 5, 4), (0, 0, 5, 5))
    assert len(frame) == 0
    assert list(frame.columns) == ["osm_id", "element_type", "tag_key", "tag_value", "geometry"]


def test_workspace_style_cache_dir_uses_snapshot_dir(tmp_path):
    class FakeWorkspace:
        snapshot_dir = tmp_path / "temp" / "snapshots"

    session = FakeSession([FakeResponse(body={"features": SNAPSHOT_FEATURES})])
    history = OhsomeHistory(client=session, cache_dir=FakeWorkspace())
    history.snapshot(date(2019, 5, 4), (0, 0, 5, 5))
    assert list((tmp_path / "temp" / "snapshots").glob("*.geojson"))


def test_snapshot_bbox_is_a_geometry_friendly_contract():
    """chains.py passes bounds straight from shapely; floats must round-trip."""
    session = FakeSession([FakeResponse(body={"features": SNAPSHOT_FEATURES})])
    history = OhsomeHistory(client=session)
    history.snapshot(date(2019, 5, 4), box(0, 0, 5, 5).bounds)
    assert session.calls[0][1]["bboxes"] == "0.0,0.0,5.0,5.0"


# -- the elements/geometry fallback ---------------------------------------


class FakeMetadataSession(FakeSession):
    """A session that also answers ``GET /metadata`` with a temporal extent."""

    def __init__(self, last_day: str, **kwargs):
        super().__init__(**kwargs)
        self.last_day = last_day

    def get(self, url, timeout=None):
        return FakeResponse(
            body={
                "extractRegion": {
                    "temporalExtent": {
                        "fromTimestamp": "2007-10-08T00:00:00Z",
                        "toTimestamp": self.last_day,
                    }
                }
            }
        )


def test_features_valid_on_keeps_the_version_current_at_the_start_of_the_day():
    early = _version("way/1", "2019-05-03T00:00:00Z", "2019-05-04T00:00:00Z", landuse="grass")
    late = _version("way/1", "2019-05-04T00:00:00Z", "2019-05-05T00:00:00Z", landuse="meadow")
    later = _version("way/1", "2019-05-05T00:00:00Z", "2019-05-06T00:00:00Z", landuse="forest")
    other = _version("way/2", "2019-05-04T00:00:00Z", "2019-05-05T00:00:00Z", landuse="park")

    kept = features_valid_on([early, late, later, other], date(2019, 5, 4))

    assert sorted(f["properties"]["@osmId"] for f in kept) == ["way/1", "way/2"]
    by_id = {f["properties"]["@osmId"]: f for f in kept}
    assert by_id["way/1"]["properties"]["landuse"] == "meadow"


def test_features_valid_on_drops_versions_that_ended_before_the_day():
    """A way deleted at noon the day before is not in the next day's snapshot."""
    gone = _version("way/1", "2019-05-03T00:00:00Z", "2019-05-03T12:00:00Z", landuse="grass")
    alive = _version("way/2", "2019-05-03T00:00:00Z", "2019-05-04T00:00:00Z", landuse="park")

    kept = features_valid_on([gone, alive], date(2019, 5, 4))

    assert [f["properties"]["@osmId"] for f in kept] == ["way/2"]


def test_snapshot_falls_back_to_full_history_when_elements_is_forbidden():
    """The public deployment answers 403 for ``elements/geometry``."""
    history_features = [
        _version("way/10", "2019-05-04T00:00:00Z", "2019-05-05T00:00:00Z", leisure="park"),
    ]

    def handler(url, payload):
        if url.endswith("elements/geometry"):
            return FakeResponse(status_code=403)
        return FakeResponse(body={"features": history_features})

    session = FakeSession(handler=handler)
    history = OhsomeHistory(client=session, base_url="https://example.test/v1")

    frame = history.snapshot(date(2019, 5, 4), (0, 0, 5, 5))

    assert list(frame["osm_id"]) == ["way-10"]
    urls = [url for url, _payload in session.calls]
    assert urls == [
        "https://example.test/v1/elements/geometry",
        "https://example.test/v1/elementsFullHistory/geometry",
    ]
    assert session.calls[-1][1]["time"] == "2019-05-04,2019-05-05"

    # the dead endpoint is not tried again for later days
    history.snapshot(date(2019, 5, 6), (0, 0, 5, 5))
    assert [url for url, _ in session.calls].count("https://example.test/v1/elements/geometry") == 1


def test_snapshot_fallback_window_stays_inside_the_temporal_extent():
    session = FakeMetadataSession(
        "2026-07-27T09:00Z",
        handler=lambda url, payload: (
            FakeResponse(status_code=403)
            if url.endswith("elements/geometry")
            else FakeResponse(body={"features": []})
        ),
    )
    history = OhsomeHistory(client=session)

    history.snapshot(date(2026, 8, 15), (0, 0, 1, 1))

    assert session.calls[-1][1]["time"] == "2026-07-26,2026-07-27"


def test_open_intervals_run_to_the_requested_window_end():
    """Data lagging today must not look like the site finished."""
    session = FakeMetadataSession(
        "2026-07-27T09:00Z",
        responses=[
            FakeResponse(
                body={
                    "features": [
                        _version(
                            "way/1",
                            "2020-01-01T00:00:00Z",
                            "2026-07-27T00:00:00Z",
                            landuse="construction",
                        )
                    ]
                }
            )
        ],
    )
    history = OhsomeHistory(client=session)

    intervals = history.construction_intervals((0, 0, 1, 1), (date(2010, 1, 1), date(2026, 8, 15)))

    assert len(intervals) == 1
    assert intervals[0].valid_to == date(2026, 8, 15)
    assert intervals[0].metadata["data_until"] == date(2026, 7, 27)
    # the query itself was clamped to what the API has
    assert session.calls[0][1]["time"] == "2010-01-01,2026-07-27"


def test_closed_intervals_are_left_alone():
    session = FakeMetadataSession(
        "2026-07-27T09:00Z",
        responses=[
            FakeResponse(
                body={
                    "features": [
                        _version(
                            "way/1",
                            "2020-01-01T00:00:00Z",
                            "2021-01-01T00:00:00Z",
                            landuse="construction",
                        )
                    ]
                }
            )
        ],
    )
    history = OhsomeHistory(client=session)

    intervals = history.construction_intervals((0, 0, 1, 1), (date(2010, 1, 1), date(2026, 8, 15)))

    assert intervals[0].valid_to == date(2020, 12, 31)
    assert "data_until" not in intervals[0].metadata


def test_snapshot_reports_element_type_so_relations_are_never_chain_linked():
    features = [
        {
            "geometry": _poly(0, 0, 1, 1),
            "properties": {"@osmId": "way/10", "landuse": "grass"},
        },
        {
            "geometry": _poly(2, 2, 3, 3),
            "properties": {"@osmId": "relation/18116986", "building": "apartments"},
        },
    ]
    session = FakeSession([FakeResponse(body={"features": features})])
    history = OhsomeHistory(client=session)

    frame = history.snapshot(date(2019, 5, 4), (0, 0, 5, 5))

    assert list(frame["element_type"]) == ["way", "relation"]


# -- daily snapshot semantics ----------------------------------------------


def test_a_midday_edit_first_counts_from_the_next_day():
    """v2 read ``{day}T00:00:00Z`` snapshots: an afternoon edit misses that day."""
    features = [
        _version("way/5", "2018-01-01T15:00:00Z", "2018-06-01T15:00:00Z", landuse="construction")
    ]

    span = intervals_from_features(features, window_end=date(2020, 1, 1))[0]

    assert span.valid_from == date(2018, 1, 2)
    assert span.valid_to == date(2018, 6, 1)


def test_a_version_never_visible_in_any_snapshot_is_dropped():
    """Created and replaced between two midnights: no snapshot ever showed it."""
    features = [
        _version("way/5", "2018-01-01T09:00:00Z", "2018-01-01T17:00:00Z", landuse="construction")
    ]

    assert intervals_from_features(features, window_end=date(2020, 1, 1)) == []


def test_each_version_keeps_its_own_footprint():
    """A site re-drawn mid-construction must stay rankable against either shape."""
    features = [
        _version(
            "way/1",
            "2018-01-01T00:00:00Z",
            "2018-03-01T00:00:00Z",
            (0, 0, 1, 1),
            landuse="construction",
        ),
        _version(
            "way/1",
            "2018-03-01T00:00:00Z",
            "2018-06-01T00:00:00Z",
            (0, 0, 2, 2),
            landuse="construction",
        ),
    ]

    span = intervals_from_features(features, window_end=date(2020, 1, 1))[0]

    assert [(v.valid_from, v.valid_to) for v in span.versions] == [
        (date(2018, 1, 1), date(2018, 2, 28)),
        (date(2018, 3, 1), date(2018, 5, 31)),
    ]
    assert span.geometry_on(date(2018, 2, 1)).bounds == (0.0, 0.0, 1.0, 1.0)
    assert span.geometry_on(date(2018, 4, 1)).bounds == (0.0, 0.0, 2.0, 2.0)
    # ``geometry`` stays the union: it is the chain footprint.
    assert span.geometry.bounds == (0.0, 0.0, 2.0, 2.0)


def test_an_open_intervals_last_version_runs_to_the_window_end():
    features = [
        _version(
            "way/1",
            "2018-01-01T00:00:00Z",
            "2020-01-01T00:00:00Z",
            (0, 0, 1, 1),
            landuse="construction",
        )
    ]

    span = intervals_from_features(features, window_end=date(2020, 1, 1))[0]

    assert span.metadata["open"] is True
    assert span.valid_to == date(2020, 1, 1)
    assert span.versions[-1].valid_to == date(2020, 1, 1)
    assert span.geometry_on(date(2020, 1, 1)).bounds == (0.0, 0.0, 1.0, 1.0)


def test_data_until_reports_the_backends_temporal_extent():
    """``cssic.chains`` stops the forward walk here instead of at today."""
    history = OhsomeHistory(client=FakeMetadataSession("2026-07-27T09:00Z"))

    assert history.data_until() == date(2026, 7, 27)
