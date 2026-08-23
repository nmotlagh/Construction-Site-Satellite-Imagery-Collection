"""Parse Osmosis ``.poly`` files into Shapely polygons, plus bbox helpers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shapely.geometry import MultiPolygon, Polygon

BBox = tuple[float, float, float, float]


def load_poly(path: str | Path) -> Polygon | MultiPolygon:
    """Load an Osmosis polygon file (lon lat rings) as a Shapely polygon.

    A ``.poly`` file is a name line followed by one or more *sections*, each a
    section-name line, ``lon lat`` rows and an ``END``; a final ``END`` closes
    the file. Every section is an outer ring and the region is their union;
    a section whose name starts with ``!`` is subtracted (a hole). Files from
    polygons.openstreetmap.fr routinely have several outer sections, so
    treating the second ring onward as holes (what v2 did) collapses a campus
    to a sliver. Coordinates are unprojected lon/lat.

    See https://wiki.openstreetmap.org/wiki/Osmosis/Polygon_Filter_File_Format
    """
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    lines = [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines:
        raise ValueError(f"No coordinate rings found in {path}")

    outers: list[list[tuple[float, float]]] = []
    holes: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] | None = None
    subtract = False
    for line in lines[1:]:  # the first line names the file
        if line.upper() == "END":
            if current:
                (holes if subtract else outers).append(current)
            current = None
            continue
        parts = line.split()
        if current is None:
            # A section header. Anything that does not parse as "lon lat"
            # starts a section; a bare number (the common "1", "2", ...) too.
            try:
                lon, lat = float(parts[0]), float(parts[1])
            except (IndexError, ValueError):
                subtract = line.startswith("!")
                current = []
                continue
            subtract = False
            current = [(lon, lat)]
            continue
        if len(parts) < 2:
            continue
        try:
            current.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    if current:
        (holes if subtract else outers).append(current)
    if not outers:
        raise ValueError(f"No coordinate rings found in {path}")

    def ring(points: list[tuple[float, float]]) -> Polygon:
        shape = Polygon(points)
        return shape if shape.is_valid else shape.buffer(0)

    polygon = unary_union([ring(r) for r in outers if len(r) >= 3])
    if holes:
        polygon = polygon.difference(unary_union([ring(r) for r in holes if len(r) >= 3]))
    if polygon.is_empty:
        raise ValueError(f"Polygon in {path} is empty")
    return polygon


def polygon_bounds(polygon: Any) -> BBox:
    """Return ``(minx, miny, maxx, maxy)`` for a geometry."""
    minx, miny, maxx, maxy = polygon.bounds
    return (float(minx), float(miny), float(maxx), float(maxy))


def bbox_csv(polygon: Any) -> str:
    """Return the geometry's bounding box as ``minx,miny,maxx,maxy``.

    This is the form the ohsome API expects for its ``bboxes`` parameter.
    """
    minx, miny, maxx, maxy = polygon_bounds(polygon)
    return f"{minx},{miny},{maxx},{maxy}"


def scale_bbox(bbox: BBox, linear_scale: float) -> BBox:
    """Scale a bounding box about its centre by ``linear_scale``.

    Use :func:`cssic.dates.padding_scale` to convert an *area* scale factor into
    the linear scale this function expects.
    """
    if linear_scale <= 0:
        raise ValueError(f"linear_scale must be > 0, got {linear_scale}")
    minx, miny, maxx, maxy = bbox
    cx = (minx + maxx) / 2.0
    cy = (miny + maxy) / 2.0
    half_w = (maxx - minx) / 2.0 * linear_scale
    half_h = (maxy - miny) / 2.0 * linear_scale
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
