# Architecture (v3)

This document is the contract for the v3 re-architecture. It is faithful to
*A Framework for Semi-automatic Collection of Temporal Satellite Imagery for
Analysis of Dynamic Regions* (Motlagh et al., ICCVW 2021). The vocabulary below
is the paper's vocabulary; keep it.

## The method, in one paragraph

Given a region polygon and a date window, find every OSM *way* tagged
`landuse=construction` or `building=construction` that was under construction
inside the window. Backends are queried by bounding box, but the result is
clipped to the polygon itself; only ways seed chains, while relations still
supply boundary tags. Each site is a **construction chain**: one physical site
that may be represented by several OSM way ids over time (a way is deleted and
re-drawn, split, or re-tagged). For each chain recover the **true start date**
(walk backward in time before the window), the **true end date** (walk forward
after the window), the **previous tag** (what the land was before construction,
taken from the best-overlapping tagged way on the day before start) and the
**final tag** (the best-overlapping tagged way on the day after end), where
"best" is intersection-over-union against a confidence threshold and weak
overlaps become `NO TAG FOUND`. A chain link (a finished site immediately
re-tagged construction) is kept only if IOU exceeds
`construction_chain_confidence`, so a boundary tag is never literally
`construction`. Then for each **completed** chain, sample `n` dates evenly
across `[start, end]` (both endpoints from `n >= 2` up; `n == 1` samples the
start; `-1` = every available acquisition), pad the bounding box by an **area
scale factor** (centre-invariant), and download RGB and/or NIR chips from
Sentinel-2 or Planet.

## Layout

```
cssic/
  __init__.py
  config.py          ExtractConfig, GatherConfig, Credentials — frozen dataclasses.
                     All validation lives here. No module-level mutable state.
  poly.py            load_poly(path) -> Polygon | MultiPolygon (.poly format);
                     every section is an outer ring and the region is their
                     union, `!`-prefixed sections are holes; bbox helpers.
  dates.py           sample_date_windows(), padding_scale()  (ported from
                     dates.py; `-n` now accepts 1 and 2, and a window reaches
                     day_padding days past its sampled date, both ends included)
  geom.py            Geometry helpers that never raise (prepare_geom, safe_union,
                     intersection_over_union, bounds_box): degenerate OSM
                     footprints degrade to None/0.0/empty instead of aborting an
                     extraction. shapely is a call-time import.
  sites.py           Site (the paper's construction chain: ids, start, end,
                     constr_tag, prev_tag, final_tag, geometry) and
                     SiteCollection (keyed by chain_id; to_gdf(); merge/link ops;
                     it also hands out the chain serial numbers, so ids do not
                     depend on process-global construction order).
                     Port of way_chain.py with the same semantics and a cleaner API.
  chains.py          THE PAPER ALGORITHM, backend-agnostic:
                       build_chains(history: HistorySource, cfg, bbox=None,
                                    progress=None) -> (completed, wip)
                     backward fill, forward fill, boundary-tag IOU ranking,
                     chain linking, and clip_to_region() so the .poly polygon --
                     not its bounding box -- decides what is collected. Pure
                     functions over a HistorySource; fully unit-testable with
                     synthetic snapshots.
  history/
    base.py          HistorySource protocol:
                       construction_intervals(bbox, window) -> list[Interval]
                           (osm_id, valid_from, valid_to, geometry, constr_tag,
                            metadata, versions); Interval.geometry is the union
                            of every footprint the way had while under
                            construction, Interval.versions keeps them apart and
                            .geometry_on(day) answers "what did it look like
                            then" (what IOU ranking is measured against)
                       snapshot(day, bbox) -> GeoDataFrame of ALL tagged
                           polygons valid on `day` -- any tag that says what a
                           piece of land is (the backends' KEY_TAGS: landuse,
                           building, leisure, amenity, natural, ...), not only
                           landuse/building, since the previous/final tag may be
                           any of them
                     Optional extension: data_until() -> date | None, the last
                     day the backend has data for; the forward walk stops there
                     instead of marching to today over days nobody can answer.
                     Days, not instants: v2 read one snapshot per day at
                     {day}T00:00:00Z, so first_visible_day()/last_visible_day()
                     convert edit timestamps into that daily view. Both backends
                     implement the protocol; chains.py uses only the protocol.
    ohsome.py        elementsFullHistory for intervals (filter ends in
                     `type:way`, so only ways seed chains); elements/geometry?time=
                     for snapshots (cached per day; the public deployment
                     answers 403, so full-history is the fallback). No Geofabrik
                     dump needed.
    osmium.py        The 2020 path: clip .osh.pbf with osmium-tool, daily
                     snapshots via pyosmium. Optional dependency; skip cleanly
                     if osmium-tool is absent.
  imagery/
    base.py          ImageSource protocol:
                       find_scenes(aoi, start, end) -> list[Scene]
                       fetch(scene, aoi, band: "rgb"|"nir") -> np.ndarray uint8 HxWx{3,1}
                     Optional: sample_scenes(aoi, windows, all_dates=False),
                     one scene per sampled window (all_dates is `-n -1`).
    chips.py         Shared: padded bbox from area scale, reproject AOI to the
                     raster CRS before windowing (this is the bug in v2),
                     reflectance->uint8 (with the provider's DN offset),
                     save_png(array, path), and is_blank(array) so a chip with
                     no signal -- every pixel within BLANK_TOLERANCE (8)
                     levels: nodata, or flat cloud/snow -- is never written.
                     All three imagery backends apply is_blank.
    stac.py          Planetary Computer Sentinel-2 L2A (sign hrefs); prefers a
                     scene that covers the whole AOI, ranks a window's
                     candidates by cloud over the *site* from the L2A SCL band
                     (not scene-wide eo:cloud_cover), and applies the baseline
                     04.00 BOA_ADD_OFFSET to raw bands.
    sentinelhub.py   CDSE / commercial Sentinel Hub Process API.
    planet.py        PSScene + Orders API v2 via planet SDK v3 (order, poll,
                     download, clip, write chips). An order that delivers no
                     usable chip stays in temp/order_log.txt rather than being
                     retired as collected.
  store.py           Output layout (unchanged from v2 so existing data stays valid):
                       output/collection/collection.{gpkg,geojson,shp...}
                       output/{chain_id}/info.txt
                         start,end,prev_tag,final_tag,minx,miny,maxx,maxy
                         (tag fields are whitespace-collapsed and commas become
                          semicolons: OSM tag values are free text and the file
                          is a single comma-separated line)
                       output/{chain_id}/images/{planet|sentinel}/{rgb|nir}/{YYYY-MM-DD}.png
                     temp/ for snapshots and order logs. reset_extract() /
                     reset_images() return how many files they removed, so the
                     CLI can report it.
  deps.py            Optional-dependency preflight: require_backend() /
                     require_history_backend() check a backend's lazy imports up
                     front, so a missing extra is an install hint, not an
                     ImportError traceback from inside a fetch.
  extract.py         run_extract(): the `cssic extract` command. Picks the
                     history backend, runs build_chains, owns the run's progress
                     output (`-v` / `-q`), writes results through Workspace.
  gather.py          run_gather(): the `cssic gather` command. Loads the saved
                     collection, samples one scene per date window (with a
                     fallback sampler for backends without sample_scenes),
                     fetches bands, drops blank chips; per-site failures never
                     lose the sites after them. Planet's order/download flow and
                     the up-front Sentinel Hub credential check live here too.
  cli.py             argparse only: cssic setup|extract|gather|reset-extract|
                     reset-images, config building, dispatch to extract.py /
                     gather.py. Flags stay compatible with the v2 README. `-n`
                     accepts any n >= 1 or -1 (the >=3 rule is gone; 1 and 2 are
                     valid samples); `--day-padding` exposes the v2 six-day
                     search widening. Every user-facing failure is one
                     `ERROR: ...` line and exit 2.
tests/               pytest, no network. Synthetic HistorySource fixtures for
                     chains.py; a real in-memory rasterio GeoTIFF in UTM for
                     chips.py so the CRS path is exercised.
poly/                Demo regions: campus.poly (an approximate single-ring box)
                     and osu.poly (OSM relation 306632 from
                     polygons.openstreetmap.fr, several outer rings).
```

Top-level `extract_sites.py`, `gather_images.py`, `way_chain.py`, etc. are
removed; `cssic` is the single entry point. `demo.ipynb` is rewritten against
the package API (`from cssic import ...`), small, and runnable with the ohsome +
STAC path (no credentials).

## Rules for implementers

- Python 3.10+, type hints, `from __future__ import annotations`, dataclasses.
- No globals mutated at import or by `set_params`-style functions.
- Network clients are injected (constructor arg) so tests mock at the client
  boundary, never with `unittest.mock.patch` on module internals.
- Optional heavy deps (`osmium`, `sentinelhub`, `planet`, `rasterio`) are
  imported lazily inside the backend that needs them; `import cssic` must
  work with none of them installed.
- Tooling: `uv` with checked-in `uv.lock`; `ruff` (lint + format) configured in
  `pyproject.toml`; GitHub Actions runs `ruff check`, `ruff format --check`,
  and `pytest` on 3.10 and 3.12.
- Keep the GPL-3.0 license and original author credits.
- Do not commit. Do not add AI attribution anywhere.
