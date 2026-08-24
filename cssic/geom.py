"""Geometry helpers shared by the chain algorithm and the site collection.

Wrappers around shapely that never raise: OSM footprints can be empty,
self-intersecting, or otherwise degenerate, and one bad polygon must not
abort an extraction that took hours. Unusable input degrades to ``None``
(``0.0`` for :func:`intersection_over_union`, an empty polygon for
:func:`bounds_box`) instead. shapely stays a call-time import so that
``import cssic`` works without it.
"""

from __future__ import annotations

from typing import Any

_GEOM_ERRORS: tuple[type[BaseException], ...] = (ValueError, TypeError, AttributeError)


def _geom_errors() -> tuple[type[BaseException], ...]:
    """The failures a degenerate geometry can raise, including shapely's own."""
    from shapely.errors import GEOSException

    return _GEOM_ERRORS + (GEOSException,)


def prepare_geom(geom: Any) -> Any | None:
    """Return a valid, non-empty geometry, or None if it cannot be used."""
    if geom is None:
        return None
    from shapely import make_valid

    try:
        if geom.is_empty:
            return None
        if not geom.is_valid:
            geom = make_valid(geom)
        if geom is None or geom.is_empty:
            return None
        return geom
    except _geom_errors():
        return None


def safe_union(left: Any, right: Any) -> Any | None:
    """Union two geometries, skipping empty/invalid inputs."""
    left = prepare_geom(left)
    right = prepare_geom(right)
    if left is None:
        return right
    if right is None:
        return left
    from shapely import union_all

    try:
        merged = union_all([left, right])
        prepared = prepare_geom(merged)
        return prepared if prepared is not None else left
    except _geom_errors():
        return left


def intersection_over_union(left: Any, right: Any) -> float:
    """IOU confidence of two geometries; 0.0 when union area is 0 or GEOS fails."""
    left = prepare_geom(left)
    right = prepare_geom(right)
    if left is None or right is None:
        return 0.0
    from shapely import union_all

    try:
        if not left.intersects(right):
            return 0.0
        inter_area = left.intersection(right).area
        union_geom = union_all([left, right])
        union_area = 0.0 if union_geom is None or union_geom.is_empty else union_geom.area
        if union_area <= 0.0:
            return 0.0
        return inter_area / union_area
    except _geom_errors() + (ZeroDivisionError,):
        return 0.0


def bounds_box(geom: Any) -> Any:
    """Return the geometry's bounding box as a polygon (empty polygon if unusable)."""
    from shapely.geometry import Polygon, box

    prepared = prepare_geom(geom)
    if prepared is None:
        return Polygon()
    minx, miny, maxx, maxy = prepared.bounds
    if any(v != v for v in (minx, miny, maxx, maxy)):  # NaN check
        return Polygon()
    return box(minx, miny, maxx, maxy)
