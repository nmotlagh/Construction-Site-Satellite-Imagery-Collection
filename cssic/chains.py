"""The paper algorithm, backend-agnostic.

Builds **construction chains** from a :class:`~cssic.history.base.HistorySource`:
backward fill of the true start date, forward fill of the true end date,
boundary-tag (previous tag / final tag) selection by IOU confidence, and chain
linking of a finished site that is immediately re-tagged construction.

This is a port of the 2020 pipeline -- ``extract_sites.initialize_construction_map``
/ ``fill_start`` / ``fill_start_helper`` / ``fill_end`` / ``fill_end_helper`` /
``add_new_sites`` / ``update_existing_sites`` / ``generate_boundary_tags`` and
``osmhandler.OSMHandler.update_prev_post_tag`` -- with the osmium file plumbing
replaced by the :class:`~cssic.history.base.HistorySource` protocol. The day
loops, the IOU thresholds and the chain-linking semantics are unchanged.

Two data flows feed the algorithm:

* which ways are *under construction* on a given day, taken from
  :meth:`~cssic.history.base.HistorySource.construction_intervals` and indexed
  per day by :class:`_ConstructionIndex` (v2 read a daily
  ``*_only_construction.osm`` snapshot for this); and
* which *tagged* polygons surround a site on a boundary day, taken from
  :meth:`~cssic.history.base.HistorySource.snapshot` over the site's padded
  bounding box (v2 ran ``osmium extract -b`` + ``time-filter`` per site).

Everything here is pure computation over that protocol -- no network, no file
IO -- so the algorithm is unit-testable with synthetic snapshots.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from cssic.config import NO_TAG, ExtractConfig
from cssic.history.base import BBox, HistorySource, Interval, data_until
from cssic.sites import Site, SiteCollection, intersection_over_union, prepare_geom

DAY = timedelta(days=1)

#: Boundary tag of a chain whose boundary day has not been examined yet.
UNKNOWN_TAG = "N/A"
#: The backward walk never looks further back than this (v2 ``fill_start``).
SEARCH_FLOOR = date(2010, 1, 1)
#: The forward walk never looks closer to today than this (v2 ``fill_end``).
FORWARD_LAG_DAYS = 7
#: Degrees of padding added to a site's bbox when asking for its neighbourhood.
BOUNDARY_PADDING = 0.001
#: Descriptors that mean "this candidate is itself a construction site".
CONSTRUCTION_DESCRIPTORS = ("landuse=construction", "building=construction")

# ``generate_boundary_tags`` modes, kept from v2.
PREV_FROM_FILL_START = 0
PREV_FROM_NEW_SITE = 1
FINAL_TAG = 2

Progress = Callable[[str], None]


def _norm(osm_id: Any) -> str:
    """Way ids arrive as ``int`` (osmium) or ``str`` (ohsome); compare as text."""
    return str(osm_id)


# ---------------------------------------------------------------------------
# Per-day view of "what is under construction"
# ---------------------------------------------------------------------------


class _ConstructionIndex:
    """Day-indexed view of the construction intervals of a bounding box.

    Replaces v2's per-day ``w/*=construction`` snapshot: asking whether a way
    was under construction on a day is an interval containment test.
    """

    def __init__(self, intervals: Iterable[Interval]) -> None:
        self._by_id: dict[str, list[Interval]] = {}
        for interval in intervals:
            self._by_id.setdefault(_norm(interval.osm_id), []).append(interval)
        for spans in self._by_id.values():
            spans.sort(key=lambda item: item.valid_from)
        starts = [s.valid_from for spans in self._by_id.values() for s in spans]
        ends = [s.valid_to for spans in self._by_id.values() for s in spans]
        self.min_day: date | None = min(starts) if starts else None
        self.max_day: date | None = max(ends) if ends else None
        self._days: dict[date, dict[str, Interval]] = {}

    def on_day(self, day: date) -> dict[str, Interval]:
        """Ways under construction on ``day``, keyed by normalised way id."""
        cached = self._days.get(day)
        if cached is None:
            cached = {
                osm_id: span
                for osm_id, spans in self._by_id.items()
                for span in spans
                if span.valid_from <= day <= span.valid_to
            }
            self._days[day] = cached
        return cached

    def interval(self, day: date, way_id: Any) -> Interval | None:
        return self.on_day(day).get(_norm(way_id))

    def geometry(self, day: date, way_id: Any) -> Any | None:
        """The way's whole construction footprint on ``day`` (union of versions)."""
        span = self.interval(day, way_id)
        return None if span is None else span.geometry

    def geometry_on(self, day: date, way_id: Any) -> Any | None:
        """The footprint the way actually had on ``day``.

        A site re-drawn while under construction has more than one footprint;
        boundary tags must be ranked against the one of the boundary day, not
        against the union of all of them.
        """
        span = self.interval(day, way_id)
        return None if span is None else span.geometry_on(day)

    def nearest_geometry(self, day: date, way_id: Any) -> Any | None:
        """The way's footprint on the construction day closest to ``day``.

        Only used when ``day`` itself is not a construction day for that way,
        which the walks make unlikely; ``None`` when the way is unknown.
        """
        spans = self._by_id.get(_norm(way_id))
        if not spans:
            return None

        def distance(span: Interval) -> int:
            before = abs((span.valid_from - day).days)
            after = abs((span.valid_to - day).days)
            return min(before, after)

        span = min(spans, key=distance)
        return span.geometry_on(min(max(day, span.valid_from), span.valid_to))

    def constr_tag(self, day: date, way_id: Any) -> str | None:
        span = self.interval(day, way_id)
        return None if span is None else span.constr_tag


# ---------------------------------------------------------------------------
# Boundary-day candidates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """One tagged polygon on a boundary day, ranked against the site by IOU."""

    osm_id: str
    descriptor: str
    geometry: Any
    is_way: bool = True


def _rows(frame: Any) -> list[dict[str, Any]]:
    """Rows of a snapshot as plain dicts.

    Accepts a GeoDataFrame (the protocol's return type) or any iterable of
    mappings, so tests can hand ``build_chains`` synthetic snapshots without
    geopandas.
    """
    if frame is None:
        return []
    if hasattr(frame, "iterrows"):
        return [dict(row) for _, row in frame.iterrows()]
    return [dict(row) for row in frame]


#: OSM element types a snapshot id may be prefixed with.
ELEMENT_TYPES = ("way", "relation", "node")


def _element_type_prefix(osm_id: Any) -> str:
    """``way/123`` / ``relation-456`` -> the element type, else ``""``.

    ohsome normalises ids to ``way-123`` and osmium hands back bare integers,
    so both separators have to be understood here.
    """
    text = _norm(osm_id).lower()
    for separator in ("/", "-"):
        if separator in text:
            prefix = text.split(separator, 1)[0]
            if prefix in ELEMENT_TYPES:
                return prefix
    return ""


def _candidates(frame: Any) -> list[_Candidate]:
    """Turn a snapshot frame into ranked-candidate inputs.

    ``descriptor`` is used when the backend supplies one (it ports v2's
    ``OSMHandler._update_descriptors``, which prefers a specific tag over
    ``building=yes`` / ``landuse=residential``); otherwise it is built from
    ``tag_key``/``tag_value``.
    """
    candidates: list[_Candidate] = []
    for row in _rows(frame):
        osm_id = row.get("osm_id")
        if osm_id is None:
            continue
        descriptor = row.get("descriptor")
        if not descriptor:
            key, value = row.get("tag_key"), row.get("tag_value")
            descriptor = f"{key}={value}" if key and value else NO_TAG
        element_type = str(row.get("element_type") or "").lower()
        if not element_type:
            element_type = _element_type_prefix(osm_id)
        candidates.append(
            _Candidate(
                osm_id=_norm(osm_id),
                descriptor=str(descriptor),
                geometry=row.get("geometry"),
                is_way=element_type != "relation",
            )
        )
    return candidates


def padded_bbox(geometry: Any, padding: float = BOUNDARY_PADDING) -> BBox | None:
    """The geometry's bounding box grown by ``padding`` degrees, or None."""
    prepared = prepare_geom(geometry)
    if prepared is None:
        return None
    minx, miny, maxx, maxy = prepared.bounds
    if any(value != value for value in (minx, miny, maxx, maxy)):  # NaN
        return None
    return (minx - padding, miny - padding, maxx + padding, maxy + padding)


# ---------------------------------------------------------------------------
# Run context
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    """Everything the ported functions used to reach for through globals."""

    history: HistorySource
    cfg: ExtractConfig
    bbox: BBox
    index: _ConstructionIndex
    progress: Progress | None = None
    #: Snapshot frames already fetched, keyed by ``(day, bbox)``.
    _snapshots: dict[tuple[date, BBox], Any] = field(default_factory=dict)

    def log(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def snapshot(self, day: date, bbox: BBox) -> Any:
        key = (day, tuple(round(value, 7) for value in bbox))
        if key not in self._snapshots:
            self._snapshots[key] = self.history.snapshot(day, bbox)
        return self._snapshots[key]


# ---------------------------------------------------------------------------
# Boundary tags (port of OSMHandler.update_prev_post_tag)
# ---------------------------------------------------------------------------


def update_prev_final_tag(
    sites: SiteCollection,
    way_id: Any,
    construction_geometry: Any,
    candidates: Sequence[_Candidate],
    cfg: ExtractConfig,
    prev: bool = True,
) -> Any | None:
    """Set a chain's **previous tag** or **final tag** from IOU-ranked candidates.

    Port of ``osmhandler.OSMHandler.update_prev_post_tag``. Candidates are
    ranked by IOU confidence, then by inverse area (a tighter polygon wins a
    tie). A candidate that is itself construction and clears
    ``construction_chain_confidence`` links the chain and its id is returned --
    that way a boundary tag is never literally ``construction``. Otherwise the
    best candidate over ``tag_confidence`` supplies the tag; anything weaker
    leaves :data:`~cssic.config.NO_TAG`.

    :return: the way id newly linked into the chain, or ``None`` when the
        chain's boundary has been resolved.
    """

    def set_tag(tag: str) -> None:
        site = sites.get(way_id)
        if prev:
            site.prev_tag = tag
        else:
            site.final_tag = tag

    site_geometry = prepare_geom(construction_geometry)
    if site_geometry is None:
        set_tag(NO_TAG)
        return None

    by_id = {candidate.osm_id: candidate for candidate in candidates}

    # Best case: the way itself is still present on the boundary day, carrying
    # whatever it was tagged before (or after) construction.
    same_way = by_id.get(_norm(way_id))
    if same_way is not None:
        set_tag(same_way.descriptor)
        return None

    known_ids = {_norm(item) for item in sites.way_ids()}
    # Polygons that overlap the site, ranked best first.
    locations_of_interest: list[tuple[_Candidate, float, float]] = []
    for candidate in candidates:
        if candidate.osm_id in known_ids:
            continue
        geometry = prepare_geom(candidate.geometry)
        if geometry is None:
            continue
        # ``intersection_over_union`` is 0.0 for disjoint geometries and for any
        # GEOS failure, which is v2's "skip this candidate" case.
        iou = intersection_over_union(site_geometry, geometry)
        if iou <= 0.0:
            continue
        area = geometry.area
        if area <= 0:
            continue
        locations_of_interest.append((candidate, iou, 1.0 / area))
    # v2 sorted ascending and reversed the list, so tied candidates come out in
    # the reverse of snapshot order. ``sort(reverse=True)`` would keep them in
    # snapshot order instead, which picks a different tag on an exact tie.
    locations_of_interest.sort(key=lambda item: (item[1], item[2]))
    locations_of_interest.reverse()

    # Pass 1: a neighbouring construction site continues this chain.
    rejected = []
    for entry in locations_of_interest:
        candidate, iou, _inv_area = entry
        if candidate.descriptor in CONSTRUCTION_DESCRIPTORS:
            if iou > cfg.construction_chain_confidence and candidate.is_way:
                sites.link(way_id, candidate.osm_id, is_prev=prev)
                sites.update(way_id, geometry=candidate.geometry)
                return candidate.osm_id
            rejected.append(entry)
    for entry in rejected:
        locations_of_interest.remove(entry)

    # Pass 2: the best confident non-construction tag becomes the boundary tag.
    for candidate, iou, _inv_area in locations_of_interest:
        if NO_TAG not in candidate.descriptor and iou > cfg.tag_confidence:
            set_tag(candidate.descriptor)
            sites.update(way_id, geometry=candidate.geometry)
            return None

    set_tag(NO_TAG)
    return None


def generate_boundary_tags(
    ctx: _Context,
    current_date: date,
    transfer_ways: Sequence[Any],
    sites: SiteCollection,
    mode: int = PREV_FROM_FILL_START,
) -> tuple[list[Any], list[Any]]:
    """Resolve boundary tags for ``transfer_ways``, linking chains where needed.

    ``mode`` says which side of the construction period ``current_date`` is on:

    ``0``
        previous tag, reached from :func:`fill_start_helper` (the site is not
        under construction on ``current_date``);
    ``1``
        previous tag, reached from :func:`add_new_sites` (the site *is* under
        construction on ``current_date``);
    ``2``
        final tag (the site was under construction the day before).

    :return: ``(complete_ways, incomplete_ways)`` -- ways whose boundary is
        settled, and ways newly linked into a chain that still needs work.
    """
    if not transfer_ways:
        return [], []

    if mode == PREV_FROM_FILL_START:
        construction_date, boundary_date = current_date + DAY, current_date
    elif mode == PREV_FROM_NEW_SITE:
        construction_date, boundary_date = current_date, current_date - DAY
    else:
        construction_date, boundary_date = current_date - DAY, current_date

    complete_ways: list[Any] = []
    incomplete_ways: list[Any] = []
    for way_id in transfer_ways:
        site = sites.get(way_id)
        bbox = padded_bbox(site.geometry)
        if bbox is None:
            complete_ways.append(way_id)
            continue
        # The site's own footprint on the day it was under construction -- not
        # the chain's union, which may cover ground this way never occupied.
        construction_geometry = ctx.index.geometry_on(construction_date, way_id)
        if construction_geometry is None:
            construction_geometry = ctx.index.nearest_geometry(construction_date, way_id)
        candidates = _candidates(ctx.snapshot(boundary_date, bbox))
        new_way = update_prev_final_tag(
            sites,
            way_id,
            construction_geometry,
            candidates,
            ctx.cfg,
            prev=mode < FINAL_TAG,
        )
        if new_way is None:
            complete_ways.append(way_id)
        else:
            incomplete_ways.append(new_way)
    return complete_ways, incomplete_ways


# ---------------------------------------------------------------------------
# Backward fill: the true start date and the previous tag
# ---------------------------------------------------------------------------


def fill_start_helper(
    ctx: _Context,
    ids_need_start: list[Any],
    uc: SiteCollection,
    current_date: date,
) -> None:
    """Walk one day backwards: close out starts and collect previous tags.

    Every id still in ``ids_need_start`` was under construction on
    ``current_date + 1``, so its start moves back to that day. An id absent
    from ``current_date`` started then and is handed to
    :func:`generate_boundary_tags`, which may link an older construction way
    into the chain -- that id then takes its place in ``ids_need_start``.
    """
    today_construction = ctx.index.on_day(current_date)
    ways_start_found: list[Any] = []
    for way_id in ids_need_start:
        uc.update(way_id, start=current_date + DAY)
        span = today_construction.get(_norm(way_id))
        if span is None:
            ways_start_found.append(way_id)
        else:
            uc.update(way_id, geometry=span.geometry)
    for way_id in ways_start_found:
        ids_need_start.remove(way_id)

    _complete, incomplete = generate_boundary_tags(
        ctx, current_date, ways_start_found, uc, mode=PREV_FROM_FILL_START
    )
    ids_need_start.extend(incomplete)


def fill_start(ctx: _Context, uc: SiteCollection) -> SiteCollection:
    """Walk backwards from the window start until every chain has a true start.

    No new construction sites are added to ``uc``; only chain links are.
    """
    ctx.log("Finding all start dates")
    ids_need_start = list(uc.way_ids())
    current_date = ctx.cfg.start - DAY
    # v2's floor. The walk normally stops long before it: the first day on which
    # nothing is under construction resolves every remaining id at once.
    while ids_need_start and current_date > SEARCH_FLOOR:
        ctx.log(f"[Finding Start] {current_date}")
        fill_start_helper(ctx, ids_need_start, uc, current_date)
        current_date -= DAY
    return uc


# ---------------------------------------------------------------------------
# Forward fill: the true end date and the final tag
# ---------------------------------------------------------------------------


def _transfer(uc: SiteCollection, completed: SiteCollection, way_id: Any) -> None:
    """Move the chain holding ``way_id`` from in-progress to completed."""
    if not uc.has_way(way_id):
        return
    key = uc.find_key(way_id)
    completed.add(uc.pop(key), key=key)


def fill_end_helper(
    ctx: _Context,
    ids_need_end: list[Any],
    uc: SiteCollection,
    completed: SiteCollection,
    current_date: date,
) -> None:
    """Walk one day forwards: close out ends, collect final tags, transfer chains."""
    today_construction = ctx.index.on_day(current_date)
    ways_end_found: list[Any] = []
    for way_id in ids_need_end:
        uc.update(way_id, end=current_date - DAY)
        if _norm(way_id) not in today_construction:
            ways_end_found.append(way_id)
    for way_id in ways_end_found:
        ids_need_end.remove(way_id)

    complete, incomplete = generate_boundary_tags(
        ctx, current_date, ways_end_found, uc, mode=FINAL_TAG
    )
    ids_need_end.extend(incomplete)
    for way_id in complete:
        _transfer(uc, completed, way_id)


def forward_ceiling(ctx: _Context) -> date:
    """The last day the forward walk may examine.

    v2 stopped one day short of ``today - 7``. Beyond that a site can still be
    under construction without anyone having edited it yet, so an end date read
    there is not an end date. The walk additionally stops at the last day the
    backend has data for -- past it every snapshot is a repeat of the last real
    one, which would make every open site look finished on the same day.
    """
    ceiling = date.today() - timedelta(days=FORWARD_LAG_DAYS + 1)
    horizon = data_until(ctx.history)
    if horizon is not None:
        ceiling = min(ceiling, horizon + DAY)
    if ctx.index.max_day is not None:
        ceiling = min(ceiling, ctx.index.max_day + DAY)
    return ceiling


def fill_end(
    ctx: _Context, uc: SiteCollection, completed: SiteCollection
) -> tuple[SiteCollection, SiteCollection]:
    """Walk forwards from the window end until every chain has a true end."""
    ctx.log("Finding all end dates")
    ids_need_end = [site.ids[-1] for site in uc.values()]
    ceiling = forward_ceiling(ctx)
    current_date = ctx.cfg.end + DAY
    while ids_need_end and current_date <= ceiling:
        ctx.log(f"[Finding End] {current_date}")
        fill_end_helper(ctx, ids_need_end, uc, completed, current_date)
        current_date += DAY
    return uc, completed


# ---------------------------------------------------------------------------
# The in-window daily loop
# ---------------------------------------------------------------------------


def add_new_sites(ctx: _Context, uc: SiteCollection, current_date: date) -> None:
    """Add chains for sites that appear as construction on ``current_date``.

    A new site's previous tag is read from the day before; if that day holds an
    older construction way overlapping strongly enough, the chain is linked
    backwards and the backward walk continues until the whole chain has a start.
    """
    known = {_norm(item) for item in uc.way_ids()}
    new_sites: list[Any] = []
    for way_id, span in ctx.index.on_day(current_date).items():
        if way_id in known:
            continue
        uc.add(
            Site(
                way_id,
                start=current_date,
                end=current_date,
                constr_tag=span.constr_tag,
                prev_tag=UNKNOWN_TAG,
                final_tag=UNKNOWN_TAG,
                geometry=span.geometry,
            )
        )
        new_sites.append(way_id)

    _complete, incomplete = generate_boundary_tags(
        ctx, current_date, new_sites, uc, mode=PREV_FROM_NEW_SITE
    )
    prev_date = current_date - DAY
    while incomplete and prev_date > SEARCH_FLOOR:
        fill_start_helper(ctx, incomplete, uc, prev_date)
        prev_date -= DAY


def update_existing_sites(
    ctx: _Context, uc: SiteCollection, completed: SiteCollection, current_date: date
) -> None:
    """Extend every in-progress chain's end date, transferring finished chains."""
    today_construction = ctx.index.on_day(current_date)
    transfer_ways: list[Any] = []
    for site in list(uc.values()):
        site.end = current_date - DAY
        most_recent = site.ids[-1]
        if _norm(most_recent) not in today_construction:
            transfer_ways.append(most_recent)

    complete, _incomplete = generate_boundary_tags(
        ctx, current_date, transfer_ways, uc, mode=FINAL_TAG
    )
    for way_id in complete:
        _transfer(uc, completed, way_id)


def initialize_construction_map(ctx: _Context) -> SiteCollection:
    """Seed the in-progress collection with everything under construction at start."""
    uc = SiteCollection()
    for way_id, span in ctx.index.on_day(ctx.cfg.start).items():
        uc.add(
            Site(
                way_id,
                start=ctx.cfg.start,
                end=ctx.cfg.start,
                constr_tag=span.constr_tag,
                prev_tag=UNKNOWN_TAG,
                final_tag=UNKNOWN_TAG,
                geometry=span.geometry,
            )
        )
    if ctx.cfg.restrict_window:
        return uc
    return fill_start(ctx, uc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def search_window(cfg: ExtractConfig) -> tuple[date, date]:
    """The window of history to ask the backend for.

    With ``--restrict-window`` that is exactly the requested window; otherwise
    it widens to the whole searchable range, because the true start may predate
    the window and the true end may follow it.
    """
    if cfg.restrict_window:
        return cfg.window
    return (SEARCH_FLOOR, max(cfg.end, date.today() - timedelta(days=FORWARD_LAG_DAYS)))


def clip_to_region(intervals: Iterable[Interval], region: Any) -> list[Interval]:
    """Drop intervals whose footprint lies outside the ``.poly`` region.

    Backends are queried by bounding box, which for a non-rectangular region
    covers ground the user did not ask for. v2 clipped both ways -- ``osmium
    extract -p <poly>`` and ``shape(geom).intersects(polygon)`` in
    ``extract_ohsome`` -- so the region, not its bounding box, decides what is
    collected. An interval with no usable footprint is kept: there is nothing
    to test it against.
    """
    kept: list[Interval] = []
    for interval in intervals:
        geometry = prepare_geom(interval.geometry)
        if geometry is None or region.intersects(geometry):
            kept.append(interval)
    return kept


def build_chains(
    history: HistorySource,
    cfg: ExtractConfig,
    bbox: BBox | None = None,
    progress: Progress | None = None,
) -> tuple[SiteCollection, SiteCollection]:
    """Return ``(completed, wip)`` construction chains for ``cfg``'s window.

    ``completed`` chains have both a true start and a true end date inside the
    searched period; ``wip`` chains were still under construction at the end of
    the window. Only completed chains are eligible for imagery collection.

    ``bbox`` defaults to the bounding box of ``cfg.poly``; the polygon itself is
    always loaded, because the backend query is by bounding box but the
    collection is clipped to the region (:func:`clip_to_region`). ``progress``
    is called with one-line status messages when given (the CLI owns all output,
    so nothing is printed by default).
    """
    from cssic.poly import load_poly, polygon_bounds

    region = load_poly(cfg.poly)
    if bbox is None:
        bbox = polygon_bounds(region)

    intervals = clip_to_region(history.construction_intervals(bbox, search_window(cfg)), region)
    ctx = _Context(
        history=history,
        cfg=cfg,
        bbox=bbox,
        index=_ConstructionIndex(intervals),
        progress=progress,
    )

    ctx.log("Initializing map")
    uc = initialize_construction_map(ctx)
    completed = SiteCollection()

    ctx.log("Locating construction")
    current_date = cfg.start + DAY
    while current_date <= cfg.end:
        ctx.log(f"[Observing Construction Changes] {current_date}")
        update_existing_sites(ctx, uc, completed, current_date)
        add_new_sites(ctx, uc, current_date)
        current_date += DAY

    if not cfg.restrict_window:
        fill_end(ctx, uc, completed)
    return completed, uc
