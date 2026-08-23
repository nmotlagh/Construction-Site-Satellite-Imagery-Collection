"""The ``HistorySource`` protocol: everything ``cssic.chains`` needs from OSM.

Both backends (:mod:`cssic.history.ohsome`, :mod:`cssic.history.osmium`)
implement this protocol, and :func:`cssic.chains.build_chains` uses nothing
else, so the paper algorithm is testable against synthetic snapshots.

**Days, not instants.** The 2020 pipeline read one snapshot per day, taken at
``{day}T00:00:00Z`` (``osmium time-filter``), so "way *w* was under construction
on day *D*" means "the version of *w* that was current at midnight UTC starting
day *D* carried the construction tag". Every backend has to reproduce that:
:func:`first_visible_day` and :func:`last_visible_day` convert the edit
timestamps a history API reports into that daily view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

#: ``(minx, miny, maxx, maxy)`` in EPSG:4326.
BBox = tuple[float, float, float, float]
#: ``(start, end)``, inclusive.
DateWindow = tuple[date, date]

#: OSM tag keys that mark a way as under construction.
CONSTRUCTION_TAGS = ("landuse", "building")

DAY = timedelta(days=1)


def first_visible_day(moment: date | datetime) -> date:
    """The first daily snapshot that shows a version created at ``moment``.

    A version edited at ``2020-01-01T15:00Z`` is not in the ``2020-01-01``
    midnight snapshot -- it first shows up on ``2020-01-02``. A version created
    exactly at midnight is visible the same day.
    """
    if not isinstance(moment, datetime):
        return moment
    if (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0):
        return moment.date()
    return moment.date() + DAY


def last_visible_day(moment: date | datetime) -> date:
    """The last daily snapshot that still shows a version replaced at ``moment``.

    The mirror of :func:`first_visible_day`: a version replaced at
    ``2020-01-10T15:00Z`` is still what the ``2020-01-10`` midnight snapshot
    shows, so its last visible day is ``2020-01-10``; one replaced exactly at
    midnight was already gone that morning.
    """
    if not isinstance(moment, datetime):
        return moment
    if (moment.hour, moment.minute, moment.second, moment.microsecond) == (0, 0, 0, 0):
        return moment.date() - DAY
    return moment.date()


@dataclass(frozen=True)
class GeometryVersion:
    """One footprint a way had, over the days that footprint was current."""

    valid_from: date
    valid_to: date
    geometry: Any


@dataclass(frozen=True)
class Interval:
    """One OSM way's continuous stretch of being tagged construction.

    ``valid_from`` is the first day the way is under construction and
    ``valid_to`` the last such day (inclusive). ``constr_tag`` is the tag key
    that carried the ``construction`` value -- ``landuse`` or ``building``.

    ``geometry`` is the union of every footprint the way had while under
    construction -- the chain's footprint, which is what the collection and
    ``info.txt`` bounds are taken from. ``versions`` keeps those footprints
    apart so :meth:`geometry_on` can answer "what did this site look like on
    *that* day", which is what boundary tags are ranked against by IOU (a site
    re-drawn mid-construction must not be compared against its later shape).
    """

    osm_id: str
    valid_from: date
    valid_to: date
    geometry: Any
    constr_tag: str
    #: Anything backend-specific worth keeping (version, timestamps, ...).
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Per-version footprints, oldest first. Empty when the backend cannot
    #: separate them, in which case :meth:`geometry_on` returns the union.
    versions: tuple[GeometryVersion, ...] = ()

    def geometry_on(self, day: date) -> Any:
        """The footprint this way had on ``day`` (the union when unknown)."""
        for version in self.versions:
            if version.valid_from <= day <= version.valid_to:
                return version.geometry
        return self.geometry


@runtime_checkable
class HistorySource(Protocol):
    """Read-only view of OSM history over a bounding box.

    A backend may additionally offer ``data_until() -> date | None`` (see
    :func:`data_until`), reporting the last day it has data for. It is not part
    of the protocol proper so that a synthetic source stays a two-method object.
    """

    def construction_intervals(self, bbox: BBox, window: DateWindow) -> list[Interval]:
        """Ways tagged ``landuse=construction`` / ``building=construction``.

        Returns every construction interval that overlaps ``window`` inside
        ``bbox``.
        """
        ...

    def snapshot(self, day: date, bbox: BBox) -> Any:
        """All tagged polygons valid on ``day``, as a GeoDataFrame.

        "Tagged" means carrying any tag that describes what a piece of land is
        (see the backends' ``KEY_TAGS``), not only ``construction``: these are
        the candidates that boundary tags (previous tag / final tag) are ranked
        from by IOU confidence. The frame has at least ``osm_id``, ``tag_key``,
        ``tag_value`` and ``geometry`` columns, in EPSG:4326.
        """
        ...


def data_until(history: Any) -> date | None:
    """The last day ``history`` has data for, or ``None`` when it does not say.

    OSM history APIs and dumps trail reality by days to weeks. The forward walk
    for a true end date stops there rather than marching to today over days no
    backend can answer for.
    """
    getter = getattr(history, "data_until", None)
    if not callable(getter):
        return None
    limit = getter()
    return limit if isinstance(limit, date) else None
