"""The 2020 osmium path: a local OSM history dump plus ``osmium-tool``.

Port of v2 ``extract_sites.locate_construction_osmium`` (the file plumbing) and
``osmhandler.OSMHandler`` (the geometry/descriptor extraction), reshaped to the
:class:`~cssic.history.base.HistorySource` protocol so
:func:`cssic.chains.build_chains` can drive it exactly like the ohsome backend.

The pipeline is unchanged:

1. ``osmium extract -p <poly> <region>.osh.pbf`` -> the region's full history;
2. ``osmium tags-filter -R`` + ``osmium getid --add-referenced`` -> the history
   of every way that was *ever* ``landuse=construction`` / ``building=construction``;
3. ``osmium time-filter`` -> a snapshot of a given day, read with ``pyosmium``.

Construction *intervals* are derived from way versions in (2) -- one pass over
the dump instead of a ``time-filter`` per day. Their geometry is then read back
from (3) on the days the footprint could actually have changed (a new version of
the way, or of one of its nodes), so a site re-drawn mid-construction keeps both
shapes: the union is the chain footprint and the per-day versions are what
boundary tags are ranked against. Boundary *snapshots* are cut from (1) with
``osmium extract -b`` over the site's padded bounding box, as in v2.

Both ``osmium-tool`` and ``pyosmium`` are optional: :func:`available` reports
whether this backend can run, and the imports happen inside the methods so
``import cssic`` never needs them.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
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
)

#: Tag keys that can describe what a polygon is, most useful first. Port of
#: ``OSMHandler._update_descriptors``.
KEY_TAGS = (
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
#: Tags too generic to describe a site when something better is present.
LESS_HELPFUL_TAGS = ("building=yes", "landuse=residential")
NO_DESCRIPTOR = "NO TAG FOUND"


def available() -> bool:
    """True when ``osmium-tool`` is on PATH and ``pyosmium`` is importable."""
    if shutil.which("osmium") is None:
        return False
    try:
        import osmium  # noqa: F401
    except ImportError:
        return False
    return True


def require_osmium() -> None:
    """Raise a clear error when the optional osmium stack is missing."""
    if shutil.which("osmium") is None:
        raise RuntimeError(
            "osmium-tool is not on PATH. Install it first "
            "(Debian/Ubuntu: sudo apt install osmium-tool; macOS: brew install osmium-tool)."
        )
    try:
        import osmium  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("pyosmium is not installed (pip install osmium)") from exc


def run_osmium(*args: str) -> None:
    """Run ``osmium <args>``, adding ``--overwrite`` when writing an output file."""
    cmd = ["osmium", *args]
    if "-o" in cmd and "--overwrite" not in cmd:
        cmd.append("--overwrite")
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{' '.join(cmd)} failed ({result.returncode}):\n{detail}")


def public_id(element_type: str, raw_id: Any) -> str:
    """``("way", 123)`` -> ``way-123``: the id form chains and the ohsome backend use.

    Inside this module ways are keyed by their bare numeric id (that is what
    pyosmium hands out); everything that leaves it -- intervals, snapshot
    frames, hence chain ids and output directories -- carries the type prefix
    so the two backends produce identical collections for the same history.
    """
    return f"{element_type}-{raw_id}"


def descriptor_for(tags: dict[str, str]) -> str:
    """The most descriptive ``key=value`` for a polygon, or ``NO TAG FOUND``.

    Port of ``OSMHandler._update_descriptors``: a specific tag beats
    ``building=yes`` / ``landuse=residential``, which in turn beat nothing.
    """
    fallback: str | None = None
    for key in KEY_TAGS:
        value = tags.get(key)
        if value is None:
            continue
        candidate = f"{key}={value}"
        if candidate not in LESS_HELPFUL_TAGS:
            return candidate
        if fallback is None:
            fallback = candidate
    return fallback if fallback is not None else NO_DESCRIPTOR


def construction_key(tags: dict[str, str]) -> str | None:
    """``"building"`` / ``"landuse"`` when the tags mark construction, else None."""
    for key in ("building", "landuse"):
        if tags.get(key) == "construction":
            return key
    return None


def _tag_dict(tags: Any) -> dict[str, str]:
    return {tag.k: tag.v for tag in tags}


def _area_rows(path: Path) -> list[dict[str, Any]]:
    """Read every polygon in an ``.osm`` snapshot as a row dict.

    Ports the ``area`` callback of ``OSMHandler``: ways and relations both
    become (multi)polygons, keyed by their original way id / relation id.
    """
    import osmium
    import shapely.wkb as wkblib

    try:
        from osmium import InvalidLocationError
    except ImportError:  # pragma: no cover - pyosmium < 4
        from osmium._osmium import InvalidLocationError

    rows: list[dict[str, Any]] = []

    class _AreaHandler(osmium.SimpleHandler):
        def __init__(self) -> None:
            osmium.SimpleHandler.__init__(self)
            self.factory = osmium.geom.WKBFactory()

        def area(self, a: Any) -> None:
            try:
                geometry = wkblib.loads(self.factory.create_multipolygon(a), hex=True)
            except (InvalidLocationError, RuntimeError):
                return
            geometry = prepare_geom(geometry)
            if geometry is None:
                return
            from_way = a.from_way()
            tags = _tag_dict(a.tags)
            descriptor = descriptor_for(tags)
            tag_key, _, tag_value = descriptor.partition("=")
            rows.append(
                {
                    "osm_id": str(a.orig_id() if from_way else a.id),
                    "element_type": "way" if from_way else "relation",
                    "tag_key": tag_key if tag_value else None,
                    "tag_value": tag_value or None,
                    "descriptor": descriptor,
                    "tags": tags,
                    "geometry": geometry,
                }
            )

    handler = _AreaHandler()
    try:
        handler.apply_file(str(path), locations=True)
    except TypeError:  # pragma: no cover - very old pyosmium
        handler.apply_file(str(path))
    return rows


@dataclass
class WayHistory:
    """One way's edit history, as days the daily snapshots would show.

    ``versions`` is ``(day, construction key or None)`` per *effective* day --
    the day the edit first appears in a ``{day}T00:00:00Z`` snapshot -- with at
    most one entry per day (a way edited twice before the same midnight is only
    ever seen in its final state). ``change_days`` additionally holds the days
    on which the way's *geometry* could have moved, including edits to its
    nodes that leave the way itself untouched.
    """

    versions: list[tuple[date, str | None]] = field(default_factory=list)
    change_days: set[date] = field(default_factory=set)


def _effective_day(stamp: Any) -> date:
    """The first snapshot day that shows an edit made at ``stamp``."""
    if isinstance(stamp, datetime):
        return first_visible_day(stamp)
    return first_visible_day(stamp) if isinstance(stamp, date) else stamp


def _collapse_same_day(versions: list[tuple[date, str | None]]) -> list[tuple[date, str | None]]:
    """Keep the last version of each day: a snapshot only shows the final state."""
    collapsed: list[tuple[date, str | None]] = []
    for day, key in sorted(versions, key=lambda item: item[0]):
        if collapsed and collapsed[-1][0] == day:
            collapsed[-1] = (day, key)
            continue
        collapsed.append((day, key))
    return collapsed


def _way_versions(path: Path) -> dict[str, WayHistory]:
    """``{way_id: WayHistory}`` from an OSM history file."""
    import osmium

    versions: dict[str, list[tuple[date, str | None]]] = {}
    way_nodes: dict[str, set[int]] = {}
    node_days: dict[int, set[date]] = {}

    class _WayHandler(osmium.SimpleHandler):
        def way(self, w: Any) -> None:
            tags = _tag_dict(w.tags) if w.visible else {}
            key = construction_key(tags) if w.visible else None
            versions.setdefault(str(w.id), []).append((_effective_day(w.timestamp), key))
            refs = way_nodes.setdefault(str(w.id), set())
            if w.visible:
                refs.update(int(node.ref) for node in w.nodes)

        def node(self, n: Any) -> None:
            node_days.setdefault(int(n.id), set()).add(_effective_day(n.timestamp))

    handler = _WayHandler()
    handler.apply_file(str(path))

    histories: dict[str, WayHistory] = {}
    for way_id, raw in versions.items():
        collapsed = _collapse_same_day(raw)
        change_days = {day for day, _key in collapsed}
        for ref in way_nodes.get(way_id, ()):  # a node moved: the footprint moved
            change_days |= node_days.get(ref, set())
        histories[way_id] = WayHistory(versions=collapsed, change_days=change_days)
    return histories


def spans_from_versions(
    versions: list[tuple[date, str | None]], open_end: date
) -> list[tuple[date, date, str]]:
    """Collapse dated way versions into ``(valid_from, valid_to, constr_tag)``.

    ``versions`` are effective days (see :class:`WayHistory`). A version that
    first carries a construction tag opens an interval; the next version without
    one closes it on the previous day. An interval still open at the end of
    history runs to ``open_end``.
    """
    spans: list[tuple[date, date, str]] = []
    start: date | None = None
    constr_tag: str | None = None
    for stamp, key in versions:
        day = _effective_day(stamp)
        if key is not None:
            if start is None:
                start, constr_tag = day, key
            else:
                constr_tag = key
            continue
        if start is not None:
            valid_to = max(start, day - DAY)
            spans.append((start, valid_to, constr_tag or "landuse"))
            start, constr_tag = None, None
    if start is not None:
        spans.append((start, max(start, open_end), constr_tag or "landuse"))
    return spans


class OsmiumHistory:
    """A :class:`~cssic.history.base.HistorySource` backed by a local history dump.

    :param region: an ``.osh.pbf`` OSM history file covering ``poly``.
    :param poly: an Osmosis ``.poly`` file bounding the area of interest.
    :param workspace: a :class:`~cssic.store.Workspace`; its ``temp/snapshots``
        directory holds the intermediate extracts.
    :param keep_temp: keep the per-day snapshots instead of deleting them.
    :param runner: the ``osmium`` invoker, injected for tests.
    """

    def __init__(
        self,
        region: str | Path,
        poly: str | Path,
        workspace: Any | None = None,
        keep_temp: bool = False,
        runner: Callable[..., None] | None = None,
    ) -> None:
        from cssic.store import Workspace

        self.region = Path(region)
        self.poly = Path(poly)
        self.workspace = workspace if workspace is not None else Workspace()
        self.keep_temp = bool(keep_temp)
        self.run = runner if runner is not None else run_osmium
        self._prepared = False
        self._snapshots: dict[tuple[date, tuple[float, ...]], Any] = {}
        self._construction_days: dict[date, dict[str, dict[str, Any]]] = {}

    # -- file plumbing ----------------------------------------------------
    @property
    def snapshot_dir(self) -> Path:
        return self.workspace.snapshot_dir

    @property
    def poly_history(self) -> Path:
        """The region's full history, clipped to the ``.poly``."""
        return self.snapshot_dir / "outputpoly.osh.pbf"

    @property
    def construction_history(self) -> Path:
        """History of every way that was ever tagged construction."""
        return self.snapshot_dir / "filtered.osh.pbf"

    def prepare(self) -> None:
        """Clip the dump to the polygon and isolate construction ways (once)."""
        if self._prepared:
            return
        require_osmium()
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        ids_only = self.snapshot_dir / "out.osh.pbf"
        self.run(
            "extract",
            "-p",
            str(self.poly),
            str(self.region),
            "-o",
            str(self.poly_history),
            "--with-history",
            "--overwrite",
        )
        self.run(
            "tags-filter",
            "-R",
            str(self.poly_history),
            "w/building=construction",
            "w/landuse=construction",
            "-o",
            str(ids_only),
        )
        self.run(
            "getid",
            "--id-osm-file",
            str(ids_only),
            "--with-history",
            str(self.poly_history),
            "-o",
            str(self.construction_history),
            "--add-referenced",
        )
        self._prepared = True

    def _time_filter(self, source: Path, day: date, out: Path) -> Path:
        self.run("time-filter", str(source), f"{day}T00:00:00Z", "-o", str(out), "--overwrite")
        return out

    def _discard(self, *paths: Path) -> None:
        if self.keep_temp:
            return
        for path in paths:
            path.unlink(missing_ok=True)

    # -- HistorySource ----------------------------------------------------
    def construction_intervals(self, bbox: BBox, window: DateWindow) -> list[Interval]:
        """Ways tagged construction inside ``bbox`` overlapping ``window``."""
        from shapely.geometry import box

        self.prepare()
        window_start, window_end = window
        histories = _way_versions(self.construction_history)

        # One record per construction stretch, plus the days its footprint could
        # have changed. Days are pooled so each one costs a single time-filter.
        records: list[dict[str, Any]] = []
        pending: dict[date, list[tuple[str, int]]] = {}
        for way_id, history in histories.items():
            for valid_from, valid_to, constr_tag in spans_from_versions(
                history.versions, window_end
            ):
                if valid_to < window_start or valid_from > window_end:
                    continue
                days = sorted(
                    {valid_from} | {d for d in history.change_days if valid_from < d <= valid_to}
                )
                index = len(records)
                records.append(
                    {
                        "way_id": way_id,
                        "valid_from": valid_from,
                        "valid_to": valid_to,
                        "constr_tag": constr_tag,
                        "rows": {},
                    }
                )
                for day in days:
                    pending.setdefault(day, []).append((way_id, index))

        for day in sorted(pending):
            geometries = self._construction_geometries(day)
            for way_id, index in pending[day]:
                row = geometries.get(way_id)
                if row is not None:
                    records[index]["rows"][day] = row

        clip = box(*bbox)
        intervals: list[Interval] = []
        for record in records:
            rows: dict[date, dict[str, Any]] = record["rows"]
            if not rows:
                continue
            versions = self._geometry_versions(rows, record["valid_from"], record["valid_to"])
            geometry = safe_union_all(version.geometry for version in versions)
            if geometry is None or not geometry.intersects(clip):
                continue
            first_row = rows[min(rows)]
            intervals.append(
                Interval(
                    osm_id=public_id("way", record["way_id"]),
                    valid_from=record["valid_from"],
                    valid_to=record["valid_to"],
                    geometry=geometry,
                    constr_tag=record["constr_tag"],
                    metadata={
                        "descriptor": first_row["descriptor"],
                        "source": "osmium",
                        "versions": len(versions),
                    },
                    versions=versions,
                )
            )
        return intervals

    @staticmethod
    def _geometry_versions(
        rows: dict[date, dict[str, Any]], valid_from: date, valid_to: date
    ) -> tuple[GeometryVersion, ...]:
        """Turn ``{day: row}`` into contiguous footprints, dropping repeats."""
        days = sorted(rows)
        versions: list[GeometryVersion] = []
        for position, day in enumerate(days):
            geometry = rows[day]["geometry"]
            start = min(day, valid_from) if position == 0 else day
            end = days[position + 1] - DAY if position + 1 < len(days) else valid_to
            if end < start:
                continue
            if versions and versions[-1].geometry.equals(geometry):
                previous = versions[-1]
                versions[-1] = GeometryVersion(previous.valid_from, end, geometry)
                continue
            versions.append(GeometryVersion(start, end, geometry))
        return tuple(versions)

    def _construction_geometries(self, day: date) -> dict[str, dict[str, Any]]:
        """Construction polygons on ``day``, keyed by way id (cached)."""
        cached = self._construction_days.get(day)
        if cached is not None:
            return cached
        self.prepare()
        snapshot = self._time_filter(
            self.construction_history, day, self.snapshot_dir / f"{day}-construction.osm"
        )
        rows = {
            row["osm_id"]: row
            for row in _area_rows(snapshot)
            if row["element_type"] == "way" and construction_key(row["tags"]) is not None
        }
        self._discard(snapshot)
        self._construction_days[day] = rows
        return rows

    def snapshot(self, day: date, bbox: BBox) -> Any:
        """All tagged polygons in ``bbox`` valid on ``day``, as a GeoDataFrame."""
        import geopandas as gpd

        self.prepare()
        key = (day, tuple(round(value, 7) for value in bbox))
        cached = self._snapshots.get(key)
        if cached is not None:
            return cached

        stem = f"{day}-{'_'.join(f'{value:.5f}' for value in key[1])}"
        extract = self.snapshot_dir / f"{stem}-extract.osh.pbf"
        self.run(
            "extract",
            "-b",
            ",".join(str(value) for value in bbox),
            str(self.poly_history),
            "-o",
            str(extract),
            "--with-history",
            "--overwrite",
        )
        boundary = self._time_filter(extract, day, self.snapshot_dir / f"{stem}-boundary.osm")
        rows = _area_rows(boundary)
        self._discard(extract, boundary)

        frame = gpd.GeoDataFrame(
            [
                {
                    "osm_id": public_id(row["element_type"], row["osm_id"]),
                    "element_type": row["element_type"],
                    "tag_key": row["tag_key"],
                    "tag_value": row["tag_value"],
                    "descriptor": row["descriptor"],
                }
                for row in rows
            ],
            columns=["osm_id", "element_type", "tag_key", "tag_value", "descriptor"],
            geometry=[row["geometry"] for row in rows],
            crs="EPSG:4326",
        )
        self._snapshots[key] = frame
        return frame
