"""ohsome API history backend -- the 2026 default, no Geofabrik dump required.

``construction_intervals`` uses the ``elementsFullHistory/geometry`` endpoint to
recover every stretch during which an OSM way carried ``landuse=construction``
or ``building=construction``. ``snapshot`` uses ``elements/geometry`` with
``time=<day>`` to get all tagged polygons valid on one day, which is what
:mod:`cssic.chains` ranks boundary tags (previous tag / final tag) from by IOU
confidence.

Notes on the API (docs.ohsome.org):

* There is no published maximum area for the full-history endpoint. Instead the
  server answers ``413 Payload Too Large`` when a request "took too long to
  compute in respect of the given or default timeout". This backend therefore
  does not guess an area limit: it splits the bounding box into quadrants and
  retries when a 413 comes back (up to ``max_tile_depth`` levels).
* ``429``/``5xx`` and transport timeouts are retried with exponential backoff.
* Data-extraction requests can report an error inside an otherwise ``200``
  response (a broken GeoJSON with an error object at the end); that case is
  detected and raised as :class:`OhsomeError`.
* ``elements/geometry`` is not always reachable -- the public deployment
  currently answers ``403 Forbidden`` for that path at the reverse proxy while
  ``elementsFullHistory/geometry`` keeps working. ``snapshot`` therefore falls
  back to a one-day full-history query and keeps the version of each element
  that was valid at the start of the day.

The HTTP session is injected so tests drive the backend with recorded JSON and
never touch the network.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from cssic.geom import prepare_geom, safe_union_all
from cssic.history.base import (
    DAY,
    BBox,
    DateWindow,
    GeometryVersion,
    Interval,
    first_visible_day,
    last_visible_day,
)

#: Endpoint used for construction intervals.
FULL_HISTORY_PATH = "elementsFullHistory/geometry"
#: Endpoint used for daily snapshots.
ELEMENTS_PATH = "elements/geometry"
#: Endpoint used for daily snapshots when :data:`ELEMENTS_PATH` is unavailable.
SNAPSHOT_FALLBACK_PATH = "elementsFullHistory/geometry"
#: Endpoint describing the temporal extent of the data behind the API.
METADATA_PATH = "metadata"

#: Only *ways* that are polygons and tagged construction. The paper, v2
#: (``osmium`` ``ways_list``) and the osmium backend all seed construction
#: chains from ways alone, so a construction multipolygon relation must not
#: become a site here either.
CONSTRUCTION_FILTER = (
    "(landuse=construction or building=construction) and geometry:polygon and type:way"
)

#: Tag keys that can describe what a piece of land is, most useful first.
#: Ported verbatim from the v2 ``OSMHandler._update_descriptors``.
KEY_TAGS: tuple[str, ...] = (
    "landuse",
    "leisure",
    "amenity",
    "aeroway",
    "barrier",
    "boundary",
    "building",
    "craft",
    "emergency",
    "geological",
    "historic",
    "man_made",
    "military",
    "natural",
    "office",
    "power",
    "public_transport",
    "shop",
    "telecom",
    "tourism",
)
#: Tags that are technically present but say nothing useful about the land.
LESS_HELPFUL_TAGS = ("building=yes", "landuse=residential")

#: Everything a boundary tag may be read from: any polygon carrying a key tag.
#: The v2 pipeline read whole daily snapshots and let
#: :func:`descriptive_tag` decide, so restricting the query to
#: ``landuse``/``building`` would hide the ``leisure=park`` or ``amenity=school``
#: that a finished site usually becomes.
SNAPSHOT_FILTER = "(" + " or ".join(f"{key}=*" for key in KEY_TAGS) + ") and geometry:polygon"

#: HTTP statuses worth retrying.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
#: The status ohsome uses for "this request took too long"; answered by tiling.
TOO_LARGE_STATUS = 413

_SNAPSHOT_COLUMNS = ("osm_id", "element_type", "tag_key", "tag_value", "geometry")


class OhsomeError(RuntimeError):
    """The ohsome API refused or failed a request."""


class OhsomeTooLarge(OhsomeError):
    """The request exceeded ohsome's compute timeout; split the bounding box."""


def _network_errors() -> tuple[type[BaseException], ...]:
    """Transport-level exceptions worth retrying (``requests`` may be absent)."""
    base: tuple[type[BaseException], ...] = (TimeoutError, ConnectionError)
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency in practice
        return base
    return base + (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
    )


def bbox_csv(bbox: BBox) -> str:
    """ohsome's ``bboxes`` parameter: ``minx,miny,maxx,maxy``."""
    minx, miny, maxx, maxy = bbox
    return f"{minx},{miny},{maxx},{maxy}"


def split_bbox(bbox: BBox) -> list[BBox]:
    """Split a bounding box into four quadrants (used when ohsome returns 413)."""
    minx, miny, maxx, maxy = bbox
    midx = (minx + maxx) / 2.0
    midy = (miny + maxy) / 2.0
    if not (minx < midx < maxx and miny < midy < maxy):
        return [bbox]
    return [
        (minx, miny, midx, midy),
        (midx, miny, maxx, midy),
        (minx, midy, midx, maxy),
        (midx, midy, maxx, maxy),
    ]


def parse_ohsome_datetime(value: str) -> datetime:
    """``2018-01-01T00:00:00Z`` -> an aware UTC :class:`~datetime.datetime`."""
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def parse_ohsome_date(value: str) -> date:
    """``2018-01-01T00:00:00Z`` -> ``date(2018, 1, 1)``."""
    return parse_ohsome_datetime(value).date()


def midnight(day: date) -> datetime:
    """The instant the daily snapshot for ``day`` is taken (``day T00:00:00Z``)."""
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


def descriptive_tag(tags: dict[str, Any]) -> tuple[str, str] | None:
    """Pick the most descriptive ``(key, value)`` pair from an element's tags.

    Mirrors the v2 handler: prefer any key tag that is not ``building=yes`` or
    ``landuse=residential``; fall back to any key tag; ``None`` when the element
    carries none of them.
    """
    fallback: tuple[str, str] | None = None
    for key in KEY_TAGS:
        value = tags.get(key)
        if value is None:
            continue
        value = str(value)
        if f"{key}={value}" in LESS_HELPFUL_TAGS:
            if fallback is None:
                fallback = (key, value)
            continue
        return (key, value)
    return fallback


def normalise_osm_id(raw: Any) -> str:
    """``way/123`` -> ``way-123`` so ids are safe as directory names."""
    return str(raw).replace("/", "-")


def element_type_of(raw: Any) -> str:
    """``way/123`` -> ``way``; ``""`` when the id carries no type prefix.

    :mod:`cssic.chains` never chain-links a relation, so the type has to travel
    with the snapshot row.
    """
    text = str(raw)
    prefix = text.split("/", 1)[0].lower() if "/" in text else ""
    return prefix if prefix in ("way", "relation", "node") else ""


def _feature_geometry(feature: dict[str, Any]) -> Any | None:
    from shapely.geometry import shape

    geom_json = feature.get("geometry")
    if not geom_json:
        return None
    try:
        geom = shape(geom_json)
        if geom.is_empty:
            return None
    except (ValueError, TypeError, AttributeError):
        return None
    # Repair when possible, but an irreparable footprint still carries its
    # row: every geometry consumer guards itself, and dropping the row would
    # lose the way's snapshot descriptor (its boundary tag).
    prepared = prepare_geom(geom)
    return prepared if prepared is not None else geom


def _append_version(
    versions: list[GeometryVersion],
    valid_from: date,
    valid_to: date,
    geometry: Any,
) -> None:
    """Add one footprint, trimming an earlier one that would overlap it."""
    while versions and versions[-1].valid_from >= valid_from:
        versions.pop()
    if versions and versions[-1].valid_to >= valid_from:
        previous = versions[-1]
        versions[-1] = GeometryVersion(previous.valid_from, valid_from - DAY, previous.geometry)
    versions.append(GeometryVersion(valid_from, valid_to, geometry))


def intervals_from_features(
    features: Iterable[dict[str, Any]],
    window_end: date | None = None,
) -> list[Interval]:
    """Collapse ohsome full-history versions into one :class:`Interval` per stretch.

    Only ways become intervals -- a construction *relation* is not a site (see
    :data:`CONSTRUCTION_FILTER`).

    Timestamps become days the way the v2 pipeline saw them: a version is
    visible in the ``{day}T00:00:00Z`` snapshot only once its edit timestamp has
    passed, so an edit at 15:00 first shows up the *next* day (see
    :func:`~cssic.history.base.first_visible_day`). A version created and
    replaced between two midnights was never in any snapshot and is dropped.

    Consecutive versions of the same OSM id are merged while they are
    day-adjacent; a real gap -- the way stopped being construction for at least
    one whole day and later became construction again -- starts a new interval.
    The interval geometry is the union of the footprints the way had while under
    construction (the chain footprint), and ``versions`` keeps them apart so
    boundary tags can be ranked against the shape of the right day.
    """
    by_id: dict[str, list[tuple[date, date, Any, str, bool]]] = {}
    for feature in features:
        props = feature.get("properties") or {}
        raw_id = props.get("@osmId")
        if not raw_id:
            continue
        # Only ways seed construction chains (see :data:`CONSTRUCTION_FILTER`);
        # an untyped id is trusted, a relation or node is not.
        element_type = str(props.get("@osmType") or "").lower() or element_type_of(raw_id)
        if element_type not in ("", "way"):
            continue
        geom = _feature_geometry(feature)
        if geom is None:
            continue
        raw_to = parse_ohsome_datetime(props["@validTo"])
        # ohsome clamps @validTo to the end of the query window for a version
        # that is still current, so that version is in the window_end snapshot.
        still_open = window_end is not None and raw_to.date() >= window_end
        start = first_visible_day(parse_ohsome_datetime(props["@validFrom"]))
        end = window_end if still_open and window_end is not None else last_visible_day(raw_to)
        if end < start:
            continue
        constr_tag = "building" if props.get("building") == "construction" else "landuse"
        by_id.setdefault(str(raw_id), []).append((start, end, geom, constr_tag, still_open))

    intervals: list[Interval] = []
    for raw_id, spans in sorted(by_id.items()):
        spans.sort(key=lambda item: item[0])
        merged: list[list[Any]] = []
        for start, end, geom, constr_tag, still_open in spans:
            if merged and start <= merged[-1][1] + DAY:
                current = merged[-1]
                current[1] = max(current[1], end)
                current[2] = constr_tag
                current[3] += 1
                current[4] = still_open
                _append_version(current[5], start, end, geom)
                continue
            merged.append(
                [start, end, constr_tag, 1, still_open, [GeometryVersion(start, end, geom)]]
            )
        for index, span in enumerate(merged):
            start, end, constr_tag, versions, still_open, footprints = span
            metadata = {
                "ohsome_id": raw_id,
                "interval_index": index,
                "versions": versions,
                "open": bool(still_open),
            }
            footprints[-1] = GeometryVersion(
                footprints[-1].valid_from, end, footprints[-1].geometry
            )
            geometry = safe_union_all(footprint.geometry for footprint in footprints)
            if geometry is None:
                continue
            intervals.append(
                Interval(
                    osm_id=normalise_osm_id(raw_id),
                    valid_from=start,
                    valid_to=end,
                    geometry=geometry,
                    constr_tag=constr_tag,
                    metadata=metadata,
                    versions=tuple(footprints),
                )
            )
    return intervals


def _extend_open(interval: Interval, query_end: date, window_end: date) -> Interval:
    """Carry an interval that is still open at ``query_end`` to ``window_end``.

    The query is clamped to the API's temporal extent, which trails today by
    days to weeks. A way still tagged construction on the last available day is
    still under construction *now*, so :mod:`cssic.chains` must keep seeing it
    until the end of the requested window -- otherwise it looks like the site
    finished on the day the data runs out.
    """
    if not interval.metadata.get("open") or window_end <= query_end:
        return interval
    metadata = {**interval.metadata, "data_until": query_end}
    versions = interval.versions
    if versions:
        last = versions[-1]
        versions = versions[:-1] + (GeometryVersion(last.valid_from, window_end, last.geometry),)
    return Interval(
        osm_id=interval.osm_id,
        valid_from=interval.valid_from,
        valid_to=window_end,
        geometry=interval.geometry,
        constr_tag=interval.constr_tag,
        metadata=metadata,
        versions=versions,
    )


def snapshot_frame(features: Iterable[dict[str, Any]]) -> Any:
    """Build the snapshot GeoDataFrame the ``HistorySource`` protocol promises."""
    import geopandas as gpd

    rows: list[tuple[str, str, str, str]] = []
    geoms: list[Any] = []
    for feature in features:
        props = feature.get("properties") or {}
        raw_id = props.get("@osmId")
        if not raw_id:
            continue
        geom = _feature_geometry(feature)
        if geom is None:
            continue
        tag = descriptive_tag(props)
        if tag is None:
            continue
        element_type = str(props.get("@osmType") or "").lower() or element_type_of(raw_id)
        rows.append((normalise_osm_id(raw_id), element_type, tag[0], tag[1]))
        geoms.append(geom)
    return gpd.GeoDataFrame(
        rows or [],
        columns=list(_SNAPSHOT_COLUMNS[:-1]),
        geometry=geoms,
        crs="EPSG:4326",
    )


def features_valid_on(features: Iterable[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    """Reduce full-history versions to the one version of each element on ``day``.

    A one-day ``elementsFullHistory`` query answers with every version that was
    valid at any point during the day. The snapshot wants the version the v2
    pipeline would have seen, i.e. the one current at ``day T00:00:00Z``: the
    latest whose ``@validFrom`` instant is not after that midnight. A version
    edited later the same day belongs to the next day's snapshot.
    """
    cutoff = midnight(day)
    best: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for feature in features:
        props = feature.get("properties") or {}
        raw_id = props.get("@osmId")
        if not raw_id:
            continue
        raw_from = props.get("@validFrom")
        valid_from = parse_ohsome_datetime(raw_from) if raw_from else cutoff
        if valid_from > cutoff:
            continue
        raw_to = props.get("@validTo")
        if raw_to and parse_ohsome_datetime(raw_to) < cutoff:
            # Superseded or deleted before the day began: a version that ended
            # at 2019-05-03T12:00 is not the element on 2019-05-04. Strictly
            # before, because the one-day query clamps the live version's
            # ``@validTo`` to the query end, which is this very midnight.
            continue
        current = best.get(str(raw_id))
        if current is None or valid_from >= current[0]:
            best[str(raw_id)] = (valid_from, feature)
    return [feature for _valid_from, feature in best.values()]


def _dedupe(features: Sequence[dict[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    """Drop features repeated because a tiled request covered them twice."""
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for feature in features:
        props = feature.get("properties") or {}
        identity = tuple(props.get(key) for key in keys)
        if identity[0] is not None:
            if identity in seen:
                continue
            seen.add(identity)
        unique.append(feature)
    return unique


def _round_bbox(bbox: BBox, digits: int = 6) -> BBox:
    return tuple(round(float(value), digits) for value in bbox)  # type: ignore[return-value]


class OhsomeHistory:
    """A :class:`~cssic.history.base.HistorySource` backed by the ohsome API.

    ``client`` is injected so tests can mock at the HTTP boundary: anything with
    a ``post(url, data=..., timeout=...)`` method returning an object with
    ``status_code`` and ``json()`` will do. ``cache_dir`` may be a path or a
    :class:`cssic.store.Workspace` (its ``temp/snapshots`` directory is used);
    snapshots are always memoised in process, and additionally written there as
    GeoJSON when a cache directory is given.
    """

    def __init__(
        self,
        client: Any | None = None,
        base_url: str = "https://api.ohsome.org/v1",
        cache_dir: Any | None = None,
        *,
        timeout: float = 120.0,
        max_retries: int = 3,
        backoff: float = 1.0,
        max_tile_depth: int = 3,
        sleep: Any | None = None,
    ) -> None:
        self.base_url = str(base_url).rstrip("/")
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff = float(backoff)
        self.max_tile_depth = int(max_tile_depth)
        self.cache_dir = self._resolve_cache_dir(cache_dir)
        self._client = client
        self._sleep = sleep
        self._snapshots: dict[tuple[str, BBox], Any] = {}
        #: Set to False once ``elements/geometry`` is known to be unavailable.
        self._elements_endpoint = True
        self._extent: tuple[date, date] | None = None
        self._extent_read = False

    # -- plumbing ---------------------------------------------------------

    @staticmethod
    def _resolve_cache_dir(cache_dir: Any | None) -> Path | None:
        if cache_dir is None:
            return None
        snapshot_dir = getattr(cache_dir, "snapshot_dir", None)
        if snapshot_dir is not None:
            return Path(snapshot_dir)
        return Path(cache_dir)

    @property
    def client(self) -> Any:
        """The injected session, or a lazily created :class:`requests.Session`."""
        if self._client is None:
            import requests

            self._client = requests.Session()
        return self._client

    def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._sleep is not None:
            self._sleep(seconds)
            return
        import time

        time.sleep(seconds)

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path}"

    def _post(self, path: str, payload: dict[str, str]) -> dict[str, Any]:
        """POST with retry; raise :class:`OhsomeTooLarge` on 413."""
        url = self._url(path)
        retryable = _network_errors()
        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.post(url, data=payload, timeout=self.timeout)
            except retryable as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.max_retries:
                    raise OhsomeError(f"ohsome request to {url} failed: {last_error}") from exc
                self._pause(self.backoff * (2**attempt))
                continue

            status = int(getattr(response, "status_code", 200))
            if status == TOO_LARGE_STATUS:
                raise OhsomeTooLarge(f"ohsome timed out on bbox {payload.get('bboxes')!r}")
            if status in RETRY_STATUSES:
                last_error = f"HTTP {status}"
                if attempt >= self.max_retries:
                    raise OhsomeError(f"ohsome request to {url} failed: {last_error}")
                self._pause(self._retry_delay(response, attempt))
                continue
            if status >= 400:
                raise OhsomeError(f"ohsome request to {url} failed: HTTP {status}")

            try:
                body = response.json()
            except ValueError as exc:
                raise OhsomeError(f"ohsome returned unparseable JSON from {url}") from exc
            self._raise_for_body_error(body, url)
            return body if isinstance(body, dict) else {"features": body}
        raise OhsomeError(f"ohsome request to {url} failed: {last_error}")

    def _retry_delay(self, response: Any, attempt: int) -> float:
        headers = getattr(response, "headers", None) or {}
        try:
            retry_after = headers.get("Retry-After")
        except AttributeError:
            retry_after = None
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass
        return self.backoff * (2**attempt)

    @staticmethod
    def _raise_for_body_error(body: Any, url: str) -> None:
        """ohsome can report a failure inside a 200 GeoJSON body."""
        if not isinstance(body, dict):
            return
        error = body.get("error")
        if not error:
            return
        status = body.get("status")
        message = error if isinstance(error, str) else json.dumps(error)
        if status == TOO_LARGE_STATUS or "timeout" in str(message).lower():
            raise OhsomeTooLarge(f"ohsome timed out: {message}")
        raise OhsomeError(f"ohsome request to {url} failed: {message}")

    def _fetch_features(
        self,
        path: str,
        payload: dict[str, str],
        bbox: BBox,
        dedupe_keys: Sequence[str],
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        """POST for one bbox, splitting into quadrants if ohsome says 413."""
        try:
            body = self._post(path, {**payload, "bboxes": bbox_csv(bbox)})
        except OhsomeTooLarge:
            tiles = split_bbox(bbox) if depth < self.max_tile_depth else [bbox]
            if len(tiles) == 1:
                raise
            collected: list[dict[str, Any]] = []
            for tile in tiles:
                collected.extend(self._fetch_features(path, payload, tile, dedupe_keys, depth + 1))
            return _dedupe(collected, dedupe_keys)
        return list(body.get("features") or [])

    # -- temporal extent --------------------------------------------------

    def temporal_extent(self) -> tuple[date, date] | None:
        """``(from, to)`` dates the API has data for, or ``None`` if unknown.

        ohsome answers 404 for any ``time`` outside the extent of the OSH data
        behind it, and that extent trails "today" by days to weeks. Queries are
        clamped to it rather than failing.
        """
        if self._extent_read:
            return self._extent
        self._extent_read = True
        get = getattr(self.client, "get", None)
        if not callable(get):
            return None
        try:
            response = get(self._url(METADATA_PATH), timeout=self.timeout)
            if int(getattr(response, "status_code", 200)) >= 400:
                return None
            body = response.json()
            extent = body["extractRegion"]["temporalExtent"]
            self._extent = (
                parse_ohsome_date(extent["fromTimestamp"]),
                parse_ohsome_date(extent["toTimestamp"]),
            )
        except Exception:  # noqa: BLE001 - metadata is a best-effort optimisation
            self._extent = None
        return self._extent

    def data_until(self) -> date | None:
        """The last day the API has data for (see :func:`cssic.history.base.data_until`)."""
        extent = self.temporal_extent()
        return extent[1] if extent is not None else None

    def _clamp_day(self, day: date) -> date:
        extent = self.temporal_extent()
        if extent is None:
            return day
        first, last = extent
        return min(max(day, first), last)

    def _clamp_window(self, window: DateWindow) -> DateWindow:
        start, end = window
        return (self._clamp_day(start), self._clamp_day(end))

    # -- HistorySource ----------------------------------------------------

    def construction_intervals(self, bbox: BBox, window: DateWindow) -> list[Interval]:
        """Construction stretches of every way in ``bbox`` overlapping ``window``."""
        start, end = self._clamp_window(window)
        payload = {
            "time": f"{start.isoformat()},{end.isoformat()}",
            "filter": CONSTRUCTION_FILTER,
            "properties": "tags,metadata",
            "clipGeometry": "false",
        }
        features = self._fetch_features(
            FULL_HISTORY_PATH,
            payload,
            tuple(float(v) for v in bbox),  # type: ignore[arg-type]
            ("@osmId", "@validFrom", "@validTo"),
        )
        intervals = intervals_from_features(features, window_end=end)
        return [_extend_open(interval, end, window[1]) for interval in intervals]

    def snapshot(self, day: date, bbox: BBox) -> Any:
        """All :data:`KEY_TAGS`-tagged polygons valid on ``day``, cached."""
        bbox = _round_bbox(tuple(float(v) for v in bbox))  # type: ignore[arg-type]
        day = self._clamp_day(day)
        key = (day.isoformat(), bbox)
        cached = self._snapshots.get(key)
        if cached is not None:
            return cached

        features = self._read_disk_cache(key)
        if features is None:
            features = self._snapshot_features(day, bbox)
            self._write_disk_cache(key, features)

        frame = snapshot_frame(features)
        self._snapshots[key] = frame
        return frame

    def _snapshot_features(self, day: date, bbox: BBox) -> list[dict[str, Any]]:
        """Snapshot features for one day, falling back to the full-history path."""
        if self._elements_endpoint:
            payload = {
                "time": day.isoformat(),
                "filter": SNAPSHOT_FILTER,
                "properties": "tags",
                "clipGeometry": "false",
            }
            try:
                return self._fetch_features(ELEMENTS_PATH, payload, bbox, ("@osmId",))
            except OhsomeTooLarge:
                raise
            except OhsomeError:
                self._elements_endpoint = False
        return self._snapshot_features_from_history(day, bbox)

    def _snapshot_features_from_history(self, day: date, bbox: BBox) -> list[dict[str, Any]]:
        """One-day ``elementsFullHistory`` query reduced to a single snapshot."""
        window_start, window_end = self._one_day_window(day)
        payload = {
            "time": f"{window_start.isoformat()},{window_end.isoformat()}",
            "filter": SNAPSHOT_FILTER,
            "properties": "tags,metadata",
            "clipGeometry": "false",
        }
        features = self._fetch_features(
            SNAPSHOT_FALLBACK_PATH,
            payload,
            bbox,
            ("@osmId", "@validFrom"),
        )
        return features_valid_on(features, day)

    def _one_day_window(self, day: date) -> DateWindow:
        """A ``[day, day+1]`` window that stays inside the API's temporal extent.

        ohsome answers 404 for a ``time`` outside the extent of the data behind
        it, so on the very last available day the window is shifted back to
        ``[day-1, day]`` -- :func:`features_valid_on` still picks the version
        that was current at ``day``.
        """
        window_end = self._clamp_day(day + timedelta(days=1))
        window_start = self._clamp_day(day)
        if window_end <= window_start:
            window_start = window_end - timedelta(days=1)
        return (window_start, window_end)

    # -- disk cache -------------------------------------------------------

    def _cache_path(self, key: tuple[str, BBox]) -> Path | None:
        if self.cache_dir is None:
            return None
        day, bbox = key
        tag = "_".join(f"{value:.6f}".replace("-", "m").replace(".", "p") for value in bbox)
        return self.cache_dir / f"snapshot_{day}_{tag}.geojson"

    def _read_disk_cache(self, key: tuple[str, BBox]) -> list[dict[str, Any]] | None:
        path = self._cache_path(key)
        if path is None or not path.exists():
            return None
        try:
            body = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        features = body.get("features") if isinstance(body, dict) else None
        return list(features) if features is not None else None

    def _write_disk_cache(self, key: tuple[str, BBox], features: list[dict[str, Any]]) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        except OSError:
            return
