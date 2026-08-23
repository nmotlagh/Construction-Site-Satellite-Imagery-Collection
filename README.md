# Construction Site Satellite Imagery Collection

Find every OpenStreetMap site that was under construction in a region and a date
window, recover when each one really started and finished, and download
multi-temporal RGB / NIR satellite chips across its construction period.

This is the code for [*A Framework for Semi-automatic Collection of Temporal
Satellite Imagery for Analysis of Dynamic
Regions*](https://openaccess.thecvf.com/content/ICCV2021W/LUAI/papers/Motlagh_A_Framework_for_Semi-Automatic_Collection_of_Temporal_Satellite_Imagery_for_ICCVW_2021_paper.pdf)
(Motlagh, Radhakrishnan, Davis and Ilin, ICCVW 2021 / LUAI).

## This fork (2026)

This is Nick Kashani Motlagh's **personal-GitHub modernization** of the original
Ohio State CVL package, kept after graduating OSU. Upstream remains
[`osu-cvl/Construction-Site-Satellite-Imagery-Collection`](https://github.com/osu-cvl/Construction-Site-Satellite-Imagery-Collection)
and is not modified here.

Original authors, still credited:

- Nicholas Kashani Motlagh, Aswathnarayan Radhakrishnan, and
  [Jim Davis](http://web.cse.ohio-state.edu/~davis.1719/) (Ohio State University)
- Roman Ilin (AFRL/RYAP, Wright-Patterson AFB)

**Point of contact for this fork:** Nick ([@nmotlagh](https://github.com/nmotlagh)).
The original OSU address `davis.1719@osu.edu` is author credit, not the support
address for this repo. The license is still [GPL-3.0](LICENSE).

What changed in v3:

- Everything lives in one package, `cssic`, installed as a single `cssic`
  command. The v2 top-level scripts (`extract_sites.py`, `gather_images.py`,
  `way_chain.py`, `osmhandler.py`, ...) are gone.
- The paper algorithm (`cssic/chains.py`) is written against backend protocols,
  so the two history backends (**ohsome**, **osmium**) and the three imagery
  backends (**STAC**, **Sentinel Hub / CDSE**, **Planet**) are interchangeable.
- Extraction defaults to the [ohsome API](https://docs.ohsome.org/ohsome-api/stable/),
  so no Geofabrik history dump is required.
- Sentinel-2 comes from Planetary Computer STAC (`-s stac`, no account) or the
  Copernicus Data Space Process API (`-s s`). The 2020-era Sentinel Hub
  instance-id / OGC path is gone.
- Planet uses PSScene + Orders API v2 through `planet` SDK v3.
- Python 3.10+, `uv` + a checked-in `uv.lock`, `ruff`, and a network-free test
  suite.

There is **no GUI on purpose**: ohsome and osmium jobs can run long, and Planet
orders consume quota. Use the CLI, the Python API, or `demo.ipynb`.

## Method

Given a region polygon and a date window, the pipeline finds every OSM *way*
tagged `landuse=construction` or `building=construction` that was under
construction inside the window. Each site is a **construction chain**: one
physical site that may be represented by several OSM way ids over time (a way is
deleted and re-drawn, split, or re-tagged). Backends are queried by bounding
box, but the collection is clipped to the polygon itself, so a site that only
falls in the box is dropped. Only ways seed chains; relations still supply
boundary tags.

For each chain the extractor recovers:

- the **true start date**, by walking backward in time before the window,
- the **true end date**, by walking forward after it,
- the **previous tag** — what the land was before construction, read from the
  best-overlapping tagged way on the day before the start,
- the **final tag** — the best-overlapping tagged way on the day after the end.

"Best" is intersection-over-union against a confidence threshold; weak overlaps
become `NO TAG FOUND`. Chain links are kept only when IOU clears
`construction_chain_confidence`, so a **boundary tag** (previous or final) is
never literally `construction`.

Then, for each **completed** chain, imagery collection samples `n` dates evenly
across `[start, end]` — both endpoints included when `n >= 2`, while `-n 1`
samples the start only and `-n -1` means every available acquisition — pads the
bounding box by an area scale factor (centre-invariant), and downloads RGB
and/or NIR chips.

Chains still under construction at the end of the search are reported as
in-progress and are not eligible for imagery. Their `end` is the last day they
were observed under construction, not the requested `--end`.

Module-by-module contract: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Third-party API notes (Planet, Sentinel Hub, pyosmium, GeoPandas):
[docs/PLANET_SENTINEL.md](docs/PLANET_SENTINEL.md).

## Install

Python 3.10+. Install with [uv](https://docs.astral.sh/uv/); the checked-in
`uv.lock` pins the whole tree.

```bash
uv sync --extra dev            # add --extra notebook for demo.ipynb
```

Or with plain pip, in a Python 3.10+ virtualenv:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,notebook]"
```

`pyproject.toml` is the source of truth. The heavy backends are extras —
`osmium`, `stac`, `sentinelhub`, `planet` (and `all`) — and `import cssic` works
with none of them installed; a missing extra is reported as an install hint, not
an `ImportError` traceback. Descartes is gone; do not install it.

**Only for `--backend osmium`:** install the **osmium-tool** system package as
well (the `osmium` Python wheel is not enough).

- Debian / Ubuntu: `sudo apt install -y osmium-tool`
- macOS: `brew install osmium-tool`
- Otherwise build from [osmcode/osmium-tool](https://github.com/osmcode/osmium-tool);
  on Windows, WSL + Ubuntu is the least painful path.

Confirm with `osmium --version`.

## Credentials

Copy the example env file and fill in the keys you need. Never commit `.env`.

```bash
cp .env.example .env
```

| Provider | What to set | Notes |
| --- | --- | --- |
| **Sentinel-2, Planetary Computer STAC** (`gather -s stac`) | nothing | Open API, asset hrefs are signed automatically. This is the no-credential path. |
| **Sentinel-2, CDSE Process API** (`gather -s s`) | `SH_CLIENT_ID`, `SH_CLIENT_SECRET`, `SENTINEL_PROVIDER=cdse` | Create an OAuth client at the [Copernicus Data Space dashboard](https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings). The secret is shown once — copy it immediately. |
| **Sentinel-2, commercial Sentinel Hub** (`gather -s s`) | `SH_CLIENT_ID`, `SH_CLIENT_SECRET`, `SENTINEL_PROVIDER=sentinelhub` | Optional. A commercial Sentinel Hub OAuth client is *not* interchangeable with a CDSE one. |
| **Planet** (`gather -s p`) | `PLANET_API_KEY`, or leave it empty and run `planet auth login` | Legacy Planet API keys still work. `PL_API_KEY` is also read. |

**Old Sentinel Hub instance ids will not work.** The 2020-era
`apps.sentinel-hub.com` configuration / instance-id flow has been removed; CDSE
free access is OAuth + Process API.

Credentials are read from `.env` and the environment by
`Credentials.from_env()`. Every one of them can also be passed on the gather
command (`--api`, `--sh-client-id`, `--sh-client-secret`, `--sentinel-provider`).

## Quick start

```bash
cssic setup
cssic extract -s 2019-01-01 -e 2020-01-01 --poly poly/campus.poly
cssic gather -s stac --rgb --nir -n 3 --verbose
```

Two demo regions ship in [Osmosis polygon
format](https://wiki.openstreetmap.org/wiki/Osmosis/Polygon_Filter_File_Format)
(`lon lat` rings):

- `poly/campus.poly` — an **approximate** box around the Ohio State University
  Columbus campus. One ring, no holes; fine for a first run.
- `poly/osu.poly` — the real campus boundary, OSM relation 306632. Several
  disjoint outer rings, so it is also the multi-section case.

Any `.poly` works. To make one for your own region, find its OSM relation id
(search on [nominatim.openstreetmap.org](https://nominatim.openstreetmap.org/))
and download the polygon:

```bash
curl -o poly/myregion.poly \
  "https://polygons.openstreetmap.fr/get_poly.py?id=306632&params=0"
```

Geofabrik also publishes `.poly` files for its regions under "Other Formats and
Auxiliary Files".

`demo.ipynb` walks the same two steps through the Python API on the
no-credential path (ohsome + STAC):

```bash
uv sync --extra notebook
uv run jupyter lab demo.ipynb
```

## CLI reference

`cssic` is the only entry point. The blocks below are real `cssic <command>
--help` output, with the top-level epilog re-wrapped: argparse prints it as a
single long line.

```
usage: cssic [-h] {setup,extract,gather,reset-extract,reset-images} ...

Extract OSM construction sites and download Sentinel-2 / Planet imagery.

positional arguments:
  {setup,extract,gather,reset-extract,reset-images}
    setup               Create temp/ and output/ directories
    extract             Extract construction sites (ohsome by default)
    gather              Download imagery for extracted sites
    reset-extract       Delete extract outputs
    reset-images        Delete downloaded imagery

options:
  -h, --help            show this help message and exit

No GUI: ohsome/osmium jobs can be long and Planet orders use quota.
Credentials come from the environment (see .env.example). Planet:
PLANET_API_KEY or `planet auth login`. Sentinel STAC (`gather -s stac`)
needs no key. Process API (`gather -s s`): SH_CLIENT_ID / SH_CLIENT_SECRET
from the CDSE dashboard.
```

### `cssic extract`

```
usage: cssic extract [-h] -s START -e END -p POLY [-r REGION]
                     [--backend {ohsome,osmium}] [--keep-temp]
                     [--restrict-window] [--save-wip] [-v | -q]

options:
  -h, --help            show this help message and exit
  -s START, --start START
                        Start date YYYY-MM-DD (on or after 2015-06-22)
  -e END, --end END     End date YYYY-MM-DD (more than 10 days before today)
  -p POLY, --poly POLY  Path to an Osmosis .poly file
  -r REGION, --region REGION
                        Path to an OSM history .osh.pbf file (required for
                        --backend osmium)
  --backend {ohsome,osmium}
                        ohsome API (default) or original osmium daily
                        snapshots
  --keep-temp           Keep the per-day osmium dumps in temp/snapshots (the
                        ohsome snapshot cache is always kept)
  --restrict-window     Do not search before start / after end for true
                        construction dates
  --save-wip            Also save in-progress sites
  -v, --verbose         Also echo the day-by-day walk (one line per observed
                        day)
  -q, --quiet           Print nothing but the final summary
```

Notes the help text has no room for:

- `--start` must be on or after **2015-06-22** (the day before Sentinel-2's
  first acquisition) and `--end` **more than 10 days** in the past, because OSM
  history and the ohsome API both trail reality. An `--end` exactly 10 days ago
  is rejected.
- By default the run prints a line per phase, `[snapshot N] YYYY-MM-DD` as each
  daily snapshot is read, and `Read N daily snapshot(s)` at the end. `-v` adds
  the day-by-day walk; `-q` prints nothing but the final summary.
- `--keep-temp` governs the per-day **osmium** dumps only. The ohsome snapshot
  cache under `temp/snapshots/` is always kept — it is keyed by day and bounding
  box, so a re-run replays from it instead of re-querying the API.
- `--restrict-window` skips the backward and forward walks entirely, so start
  and end dates are clipped to the requested window rather than being the true
  ones. On osmium that also skips the extra per-day dumps; on ohsome the time
  saved is small, since the history query is one call either way.
- Chains still under construction at `--end` are dropped with a `Skipped N
  chain(s) still in progress at <end>` line. `--save-wip` additionally writes
  them to `output/collection/in_progress.*`; they are never used for imagery,
  and their `end` is the last day they were seen under construction.
- Exit code `2` means invalid arguments or no completed chains found. Every
  user-facing failure — a malformed `.poly`, a bad date, a missing osmium
  stack — is one `ERROR: ...` line and exit `2`, never a traceback.

```bash
# default: ohsome, no history dump needed
cssic extract -s 2019-01-01 -e 2020-01-01 --poly poly/campus.poly

# the 2020 path: a local Geofabrik history extract
cssic extract -s 2019-01-01 -e 2020-01-01 --poly poly/campus.poly \
  --backend osmium --region path/to/ohio-internal.osh.pbf
```

### `cssic gather`

```
usage: cssic gather [-h] -s SOURCE [-n NUM_IMAGES] [-p PADDING]
                    [--day-padding DAY_PADDING] [-C] [-N]
                    [--max-cloud MAX_CLOUD] [-v] [-e] [--api API] [--download]
                    [--sh-client-id SH_CLIENT_ID]
                    [--sh-client-secret SH_CLIENT_SECRET]
                    [--sentinel-provider {cdse,sentinelhub}]

options:
  -h, --help            show this help message and exit
  -s SOURCE, --source SOURCE
                        'p'/'planet', 's'/'sentinel' (Process API), or 'stac'
  -n NUM_IMAGES, --num-images NUM_IMAGES
                        Number of samples across the construction period
                        (>=1), or -1 for every date
  -p PADDING, --padding PADDING
                        Area scale factor (1 = exact bbox)
  --day-padding DAY_PADDING
                        Days the search reaches past each sampled date
                        (default: 6, the v2 value)
  -C, --rgb             Download RGB (at least one of --rgb / --nir)
  -N, --nir             Download NIR
  --max-cloud MAX_CLOUD
                        Discard acquisitions cloudier than this percentage
                        (default: keep all)
  -v, --verbose
  -e, --email           Planet email notification when an order is ready
  --api API, --api-key API
                        Planet API key (or PLANET_API_KEY)
  --download, --download-planet
                        Download previously created Planet orders
  --sh-client-id SH_CLIENT_ID
                        Sentinel Hub / CDSE OAuth client id
  --sh-client-secret SH_CLIENT_SECRET
                        Sentinel Hub / CDSE OAuth client secret
  --sentinel-provider {cdse,sentinelhub}
                        cdse (default, Copernicus Data Space) or commercial
                        sentinelhub
```

Notes the help text has no room for:

- At least one of `--rgb` / `--nir` is required.
- `-n` accepts any `n >= 1`, or `-1` for every available acquisition. The v2
  "at least 3" rule is gone: `-n 1` and `-n 2` are valid samples. `-n 1` samples
  the start; both endpoints are sampled from `-n 2` up.
- `-p/--padding` is an **area** scale factor applied around the box's centre,
  so `-p 2` doubles the area, not the side length. `-p 1` is the exact box.
- `--day-padding` sets how far past a sampled date the search reaches: a window
  covers the sampled date through `--day-padding` days later, both ends
  included. `--day-padding 0` searches the sampled date alone, and no chip can
  post-date a chain's `end` by more than `--day-padding` days.
- `--max-cloud` is honoured by all three backends (an `eo:cloud_cover` search
  bound on `stac` / `s`, a `cloud_cover` range filter on Planet). It is
  scene-level everywhere — a 110 km tile can be 20% cloudy and still be solid
  cloud over one site — so treat it as a coarse pre-filter.
- On `stac`, the acquisition kept for a sampled window is the one least cloudy
  **over the site**, measured from the L2A SCL band (cloud, shadow, cirrus,
  snow) rather than the scene-wide `eo:cloud_cover`; unobserved (SCL 0)
  pixels count for neither side. Reprocessed duplicates of one overpass
  collapse to one candidate first, then at most the five least-cloudy
  acquisitions are ranked that way; a window with a single candidate is not
  checked at all. `-v` prints the per-candidate choice.
- Sentinel-2 (`stac` and `s`) clamps the search start to **2015-06-23**, its
  first acquisition; sites that started earlier are collected, they simply have
  no imagery before that date. Planet *skips* a chain that started before
  **2017-02-19**.
- Planet is a two-step **order, then download**: a first run places clipped
  PSScene orders and logs them in `temp/order_log.txt`; re-run with `--download`
  once Planet reports them ready. An order whose delivery yields no usable chip
  stays in `temp/order_log.txt` (its staging directory kept) instead of being
  retired to `temp/order_log_complete.txt`.
- A Planet account needs a plan (trial, paid, or the Education & Research
  program) before it may download anything; a bare account sees the archive
  but every search comes back empty under the permission filter. gather
  detects that case and prints a `WARNING: ... not permitted to download`
  line once, rather than silently ordering nothing.
- Per chain, gather prints `<chain>: no acquisition between <a> and <b>` for
  every window that came back empty, and closes with `<chain>: wrote K of N
  requested samples`.
- A per-site search or fetch failure is logged and skipped, not fatal — a gather
  run can take hours. Chips with no signal (all nodata, or saturated white by
  cloud) are reported as `SKIPPED blank` and not written.

```bash
# Sentinel-2 via Planetary Computer STAC (no key)
cssic gather -s stac --rgb --nir -n 3 --verbose

# Sentinel-2 L2A via the Copernicus Data Space Process API
cssic gather -s s --rgb --nir -n 3 --verbose

# Planet: place PSScene orders, then collect them later
cssic gather -s p --rgb --nir -n 3 --verbose
cssic gather -s p --rgb --nir --download
```

### `cssic setup` / `reset-extract` / `reset-images`

None of the three takes an option, and each says what it did.

```
$ cssic setup
Ready: temp/snapshots and output/collection
```

`setup` creates `temp/snapshots/` and `output/collection/`. `reset-extract`
empties both — the collection, every `output/{chain_id}/` folder and the cached
snapshots — so copy anything you want to keep first. `reset-images` deletes only
the downloaded chips, leaving the collection table and each site's `info.txt` in
place. Both resets print how many files they removed.

## Output

```
output/collection/collection.gpkg        # primary
output/collection/collection.geojson
output/collection/collection.shp         # + .dbf/.shx/.prj/.cpg
output/collection/in_progress.*          # only with --save-wip
output/{chain_id}/info.txt
output/{chain_id}/images/{planet|sentinel}/{rgb|nir}/{YYYY-MM-DD}.png
temp/snapshots/                          # cached daily snapshots
temp/order_log.txt                       # outstanding Planet orders
temp/order_log_complete.txt              # collected Planet orders
```

Each row of the collection is one construction chain, with columns `chain_id`,
`start`, `end`, `constr_tag`, `prev_tag`, `final_tag` and a bounding-box
geometry in EPSG:4326. A `chain_id` joins the chain's OSM ids with `_` and
appends a serial number, e.g. `way-1077541392_way-1235102886-1`. The serial is
handed out by the collection, not by a process-global counter, so the same
input produces the same ids.

`info.txt` is a single comma-separated line:

```
start,end,prev_tag,final_tag,minx,miny,maxx,maxy
```

OSM tag values are free text, so tag fields are whitespace-collapsed and any
comma inside them becomes a semicolon; the file stays eight fields wide.

Planet writes two extra kinds of file: `{date}_mask.png` beside an RGB chip
(the visual bundle's alpha / unusable-pixel band) and `{date}_udm.tif` in the
NIR directory (the delivered UDM2 mask). With `-n -1`, days inside the
construction period that produced no acquisition get a blank `{date}_empty.png`.

The layout is unchanged from v2, so existing datasets stay valid.

## Backends

### History (`extract --backend`)

Both backends implement the same `HistorySource` protocol and feed the same
algorithm (`cssic.chains.build_chains`), so **both** produce the paper's
construction chains, true start/end dates and IOU-ranked boundary tags. The
backend only decides where the history comes from.

**`ohsome`** (default) queries HeiGIT's [ohsome
API](https://docs.ohsome.org/ohsome-api/stable/). `elementsFullHistory/geometry`
with the filter `(landuse=construction or building=construction) and
geometry:polygon and type:way` gives every stretch during which a way carried a
construction tag; daily neighbourhood snapshots — every polygon carrying a tag
that says what the land *is* (`landuse`, `leisure`, `amenity`, `building`,
`natural`, ...), relations included — are what the boundary tags are ranked
from. Snapshots normally use
`elements/geometry?time=`; when that endpoint is unavailable (the public
deployment currently answers `403` for it) the backend falls back to a one-day
full-history query. A `413 Payload Too Large` splits the bounding box into
quadrants and retries; `429`/`5xx` back off exponentially. Queries are clamped
to the API's temporal extent, which trails today by days to weeks, and the
forward walk stops there rather than marching over days nobody can answer.
Snapshots are cached under `temp/snapshots/`.

**`osmium`** is the 2020 path, kept so the paper's results can be reproduced
from a dump: `osmium extract` clips a Geofabrik `.osh.pbf` history file to the
poly, construction ways are pulled out of it, and per-day snapshots are read
with `osmium time-filter` + pyosmium. It needs the `osmium-tool` binary and a
history file, and it is much slower — but it needs no network and it is
reproducible against a fixed dump.

### Imagery (`gather -s`)

| `-s` | Backend | Credentials | Notes |
| --- | --- | --- | --- |
| `stac` | Planetary Computer Sentinel-2 L2A | none | Prefers a scene that covers the whole AOI, keeps the acquisition least cloudy *over the site* (SCL band) per sampled window, and applies the baseline-04.00 `BOA_ADD_OFFSET` so post-2022 chips are not ~10% too bright. |
| `s` / `sentinel` | Sentinel Hub Process API (CDSE by default) | `SH_CLIENT_ID` / `SH_CLIENT_SECRET` | Catalog for dates, Process API for pixels, 10 m. |
| `p` / `planet` | Planet PSScene + Orders API v2 | `PLANET_API_KEY` or `planet auth login` | Asynchronous: order, then download. Orders are clipped and reprojected to EPSG:4326. |

All three reproject the WGS84 AOI into the raster's CRS before windowing, write
8-bit PNGs, and refuse to write a chip with no signal — one whose pixels all
sit within 8 levels of each other, whether nodata (the scene only caught a
corner of the site) or flat cloud/snow (which pins Sentinel-2's `visual` asset
at 255 and leaves NIR a near-constant grey).

## History files (Geofabrik `.osh.pbf`)

Only needed for `--backend osmium`. Extraction needs a **full-history** extract
covering your polygon, not a current-snapshot `.osm.pbf`.

1. Create a free [OpenStreetMap account](https://www.openstreetmap.org/user/new).
2. Open the regional page on [download.geofabrik.de](https://download.geofabrik.de)
   (Ohio demo: [north-america/us/ohio.html](https://download.geofabrik.de/north-america/us/ohio.html)).
3. Scroll to **Other Formats and Auxiliary Files**. The history file (`.osh.pbf`)
   is served from Geofabrik's **internal** server and requires an OSM login:
   [osm-internal.download.geofabrik.de](https://osm-internal.download.geofabrik.de/).
4. Click **internal server**, sign in, and download the `*-internal.osh.pbf` file.

The polygon must sit inside the region you download. Keep `*.osh.pbf` out of git
(already in `.gitignore`): the files are large and carry OSM contributor
metadata.

## Python API

Extraction:

```python
from cssic import ExtractConfig, Workspace, load_poly, polygon_bounds
from cssic.chains import build_chains
from cssic.history import get_history_source

ws = Workspace()          # rooted at the current directory; Workspace(path) elsewhere
ws.setup()

cfg = ExtractConfig(start="2019-01-01", end="2020-01-01", poly="poly/campus.poly")
bbox = polygon_bounds(load_poly(cfg.poly))
history = get_history_source("ohsome", cache_dir=ws)

completed, wip = build_chains(history, cfg, bbox=bbox, progress=print)
collection_gdf = completed.to_gdf()      # None when nothing completed
ws.save_collection(collection_gdf)       # collection.gpkg / .geojson / .shp
ws.create_dataset(collection_gdf)        # output/{chain_id}/info.txt
```

For the osmium backend: `ExtractConfig(..., backend="osmium", region="ohio-internal.osh.pbf")`
and `get_history_source("osmium", region=cfg.region, poly=cfg.poly, workspace=ws)`.

Imagery:

```python
from cssic import Credentials, GatherConfig, Workspace
from cssic.cli import run_gather

ws = Workspace()
run_gather(
    GatherConfig(source="stac", num_images=3, padding=2, rgb=True, nir=True, verbose=True),
    Credentials.from_env(),
    ws,
)
```

The pieces are usable on their own:
`cssic.imagery.get_image_source("stac").find_scenes(aoi, start, end)` and
`.fetch(scene, aoi, "rgb")`, `cssic.dates.sample_date_windows`,
`cssic.imagery.chips.padded_box` / `save_png`, and
`cssic.sites.intersection_over_union`.

Nothing in the package mutates global state — chain serial numbers are handed
out by the `SiteCollection` that owns them, not by a module- or class-level
counter — and every network client is a constructor argument, so an alternative
history or imagery source is a class that satisfies the protocol in
`cssic/history/base.py` or `cssic/imagery/base.py`.

## Development

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

The test suite never touches the network: HTTP sessions, STAC clients, Sentinel
Hub requests, Planet clients and the osmium runner are injected at the
constructor boundary and faked. Tests needing the `osmium-tool` binary skip when
it is absent. GitHub Actions runs the same three commands on Python 3.10 and
3.12, plus a second job that installs the core dependencies only and checks that
`import cssic` works with no extra present (`.github/workflows/ci.yml`).

## License

[GPL-3.0](LICENSE).
