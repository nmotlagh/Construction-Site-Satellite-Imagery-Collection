"""Parse Osmosis .poly files into Shapely polygons."""

from __future__ import annotations

from pathlib import Path

from shapely.geometry import Polygon


def load_poly(path: str | Path) -> Polygon:
    """Load an Osmosis polygon file (lon lat rings) as a Shapely polygon.

    See https://wiki.openstreetmap.org/wiki/Osmosis/Polygon_Filter_File_Format
    """
    text = Path(path).read_text(encoding="utf-8")
    rings: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper() == "END":
            if current:
                rings.append(current)
                current = []
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        current.append((lon, lat))
    if current:
        rings.append(current)
    if not rings:
        raise ValueError(f"No coordinate rings found in {path}")
    exterior = rings[0]
    holes = rings[1:] if len(rings) > 1 else None
    polygon = Polygon(exterior, holes)
    if not polygon.is_valid or polygon.is_empty:
        polygon = polygon.buffer(0)
    if polygon.is_empty:
        raise ValueError(f"Polygon in {path} is empty")
    return polygon


def bbox_csv(polygon: Polygon) -> str:
    minx, miny, maxx, maxy = polygon.bounds
    return f"{minx},{miny},{maxx},{maxy}"
