"""The day semantics and per-version geometry every history backend shares.

v2 read one snapshot per day, taken at ``{day}T00:00:00Z`` with ``osmium
time-filter``. :func:`~cssic.history.base.first_visible_day` and
:func:`~cssic.history.base.last_visible_day` are what turn the instant-precise
timestamps a modern history API reports back into that daily view, and
:meth:`~cssic.history.base.Interval.geometry_on` is what keeps a site re-drawn
mid-construction from being IOU-ranked against a footprint it never had.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from shapely.geometry import box

from cssic.history.base import (
    GeometryVersion,
    Interval,
    data_until,
    first_visible_day,
    last_visible_day,
)

UTC = timezone.utc
EARLY = box(0.0, 0.0, 1.0, 1.0)
LATE = box(0.5, 0.0, 1.5, 1.0)
UNION = box(0.0, 0.0, 1.5, 1.0)


# -- daily snapshot semantics ----------------------------------------------


def test_a_midday_edit_first_shows_up_the_next_morning():
    """The 2020-01-01 midnight snapshot predates an edit made that afternoon."""
    assert first_visible_day(datetime(2020, 1, 1, 15, 0, tzinfo=UTC)) == date(2020, 1, 2)


def test_an_edit_exactly_at_midnight_is_visible_the_same_day():
    assert first_visible_day(datetime(2020, 1, 1, 0, 0, tzinfo=UTC)) == date(2020, 1, 1)


def test_a_midday_replacement_is_still_in_that_mornings_snapshot():
    """A version replaced at 15:00 is what the 15:00-day snapshot showed."""
    assert last_visible_day(datetime(2020, 1, 10, 15, 0, tzinfo=UTC)) == date(2020, 1, 10)


def test_a_replacement_exactly_at_midnight_was_already_gone():
    assert last_visible_day(datetime(2020, 1, 10, 0, 0, tzinfo=UTC)) == date(2020, 1, 9)


def test_a_microsecond_after_midnight_still_costs_a_day():
    moment = datetime(2020, 1, 1, 0, 0, 0, 1, tzinfo=UTC)
    assert first_visible_day(moment) == date(2020, 1, 2)
    assert last_visible_day(moment) == date(2020, 1, 1)


def test_plain_dates_pass_through_untouched():
    """A backend that already speaks days must not be shifted twice."""
    assert first_visible_day(date(2020, 1, 1)) == date(2020, 1, 1)
    assert last_visible_day(date(2020, 1, 1)) == date(2020, 1, 1)


def test_first_and_last_visible_days_never_overlap():
    """Consecutive versions tile the days: no day shows both, none is lost."""
    for hour in (0, 1, 12, 23):
        moment = datetime(2020, 6, 15, hour, 0, tzinfo=UTC)
        assert last_visible_day(moment) < first_visible_day(moment)
        assert (first_visible_day(moment) - last_visible_day(moment)).days == 1


# -- per-version geometry ---------------------------------------------------


def construction(versions=()):
    return Interval(
        osm_id="way/1",
        valid_from=date(2020, 1, 1),
        valid_to=date(2020, 1, 10),
        geometry=UNION,
        constr_tag="landuse",
        versions=tuple(versions),
    )


def test_geometry_on_picks_the_footprint_of_that_day():
    span = construction(
        [
            GeometryVersion(date(2020, 1, 1), date(2020, 1, 5), EARLY),
            GeometryVersion(date(2020, 1, 6), date(2020, 1, 10), LATE),
        ]
    )

    assert span.geometry_on(date(2020, 1, 1)) is EARLY
    assert span.geometry_on(date(2020, 1, 5)) is EARLY
    assert span.geometry_on(date(2020, 1, 6)) is LATE
    assert span.geometry_on(date(2020, 1, 10)) is LATE


def test_geometry_on_falls_back_to_the_union_outside_the_versions():
    span = construction([GeometryVersion(date(2020, 1, 1), date(2020, 1, 5), EARLY)])

    assert span.geometry_on(date(2020, 1, 9)) is UNION


def test_geometry_on_without_versions_is_always_the_union():
    """A backend that cannot separate footprints still answers the question."""
    assert construction().geometry_on(date(2020, 1, 3)) is UNION


def test_the_interval_geometry_stays_the_union_of_every_footprint():
    """``geometry`` is the chain footprint the collection and info.txt use."""
    span = construction(
        [
            GeometryVersion(date(2020, 1, 1), date(2020, 1, 5), EARLY),
            GeometryVersion(date(2020, 1, 6), date(2020, 1, 10), LATE),
        ]
    )

    assert span.geometry.bounds == UNION.bounds


# -- the optional data_until extension --------------------------------------


class Horizon:
    def __init__(self, value):
        self.value = value

    def data_until(self):
        return self.value


class NoHorizon:
    """A minimal two-method history source: the extension is optional."""


def test_data_until_reads_the_backend_horizon():
    assert data_until(Horizon(date(2026, 7, 27))) == date(2026, 7, 27)


def test_data_until_of_a_source_without_the_extension_is_none():
    assert data_until(NoHorizon()) is None


def test_data_until_ignores_a_non_date_answer():
    assert data_until(Horizon("2026-07-27")) is None
    assert data_until(Horizon(None)) is None
