"""Extract OSM construction intervals via the HeiGIT ohsome API.

This is the 2026 default: no Geofabrik history dump and no daily osmium snapshots.
It tracks each OSM id's construction tag lifetime. It does **not** reconstruct
the 2020 WayChain ID-rewiring logic; use ``--backend osmium`` for that.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

import geopandas as gpd
import requests
from shapely.geometry import box, shape
from shapely.ops import unary_union

from polyfile import bbox_csv, load_poly
from workspace import COLLECTION_DIR, setup_directory

OHSOME_FULL_HISTORY = "https://api.ohsome.org/v1/elementsFullHistory/geometry"
CONSTRUCTION_FILTER = (
    "(landuse=construction or building=construction) and geometry:polygon"
)


def _as_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def merge_construction_spans(
    features: list[dict[str, Any]],
    query_end: date,
) -> tuple[gpd.GeoDataFrame | None, gpd.GeoDataFrame | None]:
    """Collapse ohsome full-history features into one row per OSM id interval."""
    by_id: dict[str, list[tuple[date, date, Any, str]]] = defaultdict(list)
    for feature in features:
        props = feature.get("properties") or {}
        geom_json = feature.get("geometry")
        if not geom_json or not props.get("@osmId"):
            continue
        geom = shape(geom_json)
        if geom.is_empty:
            continue
        constr = "building" if props.get("building") == "construction" else "landuse"
        by_id[props["@osmId"]].append(
            (_as_date(props["@validFrom"]), _as_date(props["@validTo"]), geom, constr)
        )

    completed_rows: list[list[str]] = []
    completed_geoms = []
    wip_rows: list[list[str]] = []
    wip_geoms = []
    columns = ["chain_id", "start", "end", "constr_tag", "prev_tag", "final_tag"]

    for osm_id, spans in by_id.items():
        spans.sort(key=lambda item: item[0])
        merged: list[list[Any]] = []
        for start, end, geom, constr in spans:
            if not merged or start > merged[-1][1]:
                merged.append([start, end, geom, constr])
                continue
            merged[-1][1] = max(merged[-1][1], end)
            merged[-1][2] = unary_union([merged[-1][2], geom])
            merged[-1][3] = constr
        for index, (start, end, geom, constr) in enumerate(merged):
            minx, miny, maxx, maxy = geom.bounds
            row = [
                f"{osm_id.replace('/', '-')}-{index}",
                start.isoformat(),
                end.isoformat(),
                constr,
                "N/A",
                "N/A",
            ]
            bbox_geom = box(minx, miny, maxx, maxy)
            if end >= query_end:
                wip_rows.append(row)
                wip_geoms.append(bbox_geom)
            else:
                completed_rows.append(row)
                completed_geoms.append(bbox_geom)

    def _frame(rows, geoms):
        if not rows:
            return None
        return gpd.GeoDataFrame(rows, columns=columns, geometry=geoms, crs="EPSG:4326")

    return _frame(wip_rows, wip_geoms), _frame(completed_rows, completed_geoms)


def fetch_construction_history(
    polygon,
    start: date,
    end: date,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    client = session or requests.Session()
    payload = {
        "bboxes": bbox_csv(polygon),
        "time": f"{start.isoformat()},{end.isoformat()}",
        "filter": CONSTRUCTION_FILTER,
        "properties": "tags,metadata",
        "clipGeometry": "false",
    }
    response = client.post(OHSOME_FULL_HISTORY, data=payload, timeout=120)
    response.raise_for_status()
    body = response.json()
    return list(body.get("features") or [])


def save_gdf(gdf, stem: str) -> None:
    COLLECTION_DIR.mkdir(parents=True, exist_ok=True)
    gdf.to_file(COLLECTION_DIR / f"{stem}.gpkg", driver="GPKG")
    gdf.to_file(COLLECTION_DIR / f"{stem}.geojson", driver="GeoJSON")
    gdf.to_file(COLLECTION_DIR / f"{stem}.shp")


def locate_construction_ohsome(params: dict) -> tuple[gpd.GeoDataFrame | None, gpd.GeoDataFrame | None]:
    setup_directory()
    polygon = load_poly(params["poly"])
    print(f"Querying ohsome for construction polygons in {polygon.bounds}")
    features = fetch_construction_history(polygon, params["start"], params["end"])
    print(f"ohsome returned {len(features)} history versions")
    inside = []
    for feature in features:
        geom = feature.get("geometry")
        if geom and shape(geom).intersects(polygon):
            inside.append(feature)
    wip_gdf, completed_gdf = merge_construction_spans(inside, params["end"])
    if wip_gdf is not None and params.get("save-wip"):
        save_gdf(wip_gdf, "in_progress")
        print(f"Saved {len(wip_gdf)} in-progress sites")
    if completed_gdf is not None:
        save_gdf(completed_gdf, "collection")
        print(f"Saved {len(completed_gdf)} completed sites")
    elif wip_gdf is not None:
        # If everything is still tagged construction at query end, still write a collection.
        save_gdf(wip_gdf, "collection")
        print(f"Saved {len(wip_gdf)} sites still tagged construction at {params['end']}")
        completed_gdf = wip_gdf
    else:
        print("No construction sites found in this window.")
    return wip_gdf, completed_gdf
