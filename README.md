# Construction Site Satellite Imagery Collection

This repository extracts OpenStreetMap construction *ways* in a region of interest, then downloads multi-temporal RGB and/or NIR satellite chips that cover each site's construction window. It is the code for [A Framework for Semi-automatic Collection of Temporal Satellite Imagery for Analysis of Dynamic Regions](https://openaccess.thecvf.com/content/ICCV2021W/LUAI/papers/Motlagh_A_Framework_for_Semi-Automatic_Collection_of_Temporal_Satellite_Imagery_for_ICCVW_2021_paper.pdf) (ICCVW 2021 / LUAI).

## This fork (2026)

This is Nick Kashani Motlagh's **personal-GitHub modernization** of the original Ohio State CVL package, kept after graduating OSU. Upstream remains [`osu-cvl/Construction-Site-Satellite-Imagery-Collection`](https://github.com/osu-cvl/Construction-Site-Satellite-Imagery-Collection) and is not modified here.

v2 targets **Python 3.10+**. Default extract uses the [ohsome API](https://docs.ohsome.org/ohsome-api/stable/) (no Geofabrik history dump). Sentinel chips can come from **Planetary Computer STAC** (`-s stac`) or the **Copernicus Data Space Process API** (`-s s`). Planet uses **PSScene + Orders API v2**. The 2020 osmium daily-snapshot backend is still there as `--backend osmium`. The license is still **GPL-3.0**.

Original authors, still credited:

- Nicholas Kashani Motlagh, Aswathnarayan Radhakrishnan, and [Jim Davis](http://web.cse.ohio-state.edu/~davis.1719/) (Ohio State University)
- Roman Ilin (AFRL/RYAP, Wright-Patterson AFB)

**Point of contact for this fork:** Nick ([@nmotlagh](https://github.com/nmotlagh)). The original OSU contact `davis.1719@osu.edu` is retained as author credit, not as the support address for this repo.

There is **no GUI on purpose**. ohsome queries and osmium jobs can be long; Planet orders use quota. Use the CLI or `demo.ipynb`.

## Install

1. **Optional**, only if you want the paper-faithful osmium backend: install the **osmium-tool** system package (the `osmium` Python wheel is not enough):

   - Debian / Ubuntu: `sudo apt install -y osmium-tool`
   - macOS: `brew install osmium-tool`
   - Other OS: build from [osmcode/osmium-tool](https://github.com/osmcode/osmium-tool). On Windows, WSL + Ubuntu is the least painful path.

   Confirm with `osmium --version`.

2. Create a virtualenv with Python 3.10+, then install this package (editable, with tests and the notebook extra):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev,notebook]"
   ```

`pyproject.toml` is the source of truth. `requirements.txt` lists the same runtime pins for people who prefer a plain `pip install -r`. Descartes is gone; do not install it.

## Credentials

Copy the example env file and fill in **new** keys. Do not commit `.env` or API keys.

```bash
cp .env.example .env
```

| Provider | What to set | Notes |
| --- | --- | --- |
| **Planet** | `PLANET_API_KEY` **or** leave it empty and run `planet auth login` | Legacy Planet keys still work if you have them. Nick may need a new key. |
| **Sentinel-2 STAC** | none | `cssic gather -s stac` uses Planetary Computer. No CDSE account. |
| **Sentinel-2 Process API** | `SH_CLIENT_ID` and `SH_CLIENT_SECRET` from a CDSE OAuth client; `SENTINEL_PROVIDER=cdse` | Create the client at the [Copernicus Data Space Sentinel Hub dashboard](https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings). Copy the secret immediately; it is shown once. |

**Old Sentinel Hub instance IDs will not work.** The 2020-era `apps.sentinel-hub.com` configuration / instance-id flow is gone. CDSE OAuth clients are not interchangeable with commercial Sentinel Hub clients. Optional commercial Sentinel Hub is still wired (`SENTINEL_PROVIDER=sentinelhub`), but the default is free CDSE.

You can override credentials on the gather command (`--api`, `--sh-client-id`, `--sh-client-secret`, `--sentinel-provider`) instead of using `.env`.

## CLI

After install, the `cssic` entry point (`cssic = "cli:main"`) is the single argparse surface. `extract_sites` / `gather_images` take a params dict; they do not parse argv a second time.

```bash
cssic setup
cssic extract -s YYYY-MM-DD -e YYYY-MM-DD --poly poly/campus.poly
cssic extract -s YYYY-MM-DD -e YYYY-MM-DD --poly poly/campus.poly --backend osmium --region ohio-internal.osh.pbf
cssic gather -s stac --rgb --nir -n 3
cssic gather -s s --rgb --nir -n 3          # Sentinel Hub / CDSE Process API
cssic gather -s p --rgb --nir -n 3
cssic gather -s p --download
cssic reset-extract
cssic reset-images
```

The original scripts still work and take the same flags:

```bash
python workspace.py
python extract_sites.py -s YYYY-MM-DD -e YYYY-MM-DD --poly poly/campus.poly
python extract_sites.py -s YYYY-MM-DD -e YYYY-MM-DD --poly poly/campus.poly --backend osmium --region ohio-internal.osh.pbf
python gather_images.py --source stac --rgb --nir --num-images 3
python gather_images.py --source p --rgb --nir --num-images 3
python reset_extract.py
python reset_images.py
```

Walk through the same two-step story (extract sites, then gather images) in `demo.ipynb`:

```bash
jupyter lab
```

## History files (Geofabrik `.osh.pbf`)

Only needed for **`--backend osmium`**. Default ohsome extract does not download a history dump.

Extraction then needs a **full-history** extract that contains your polygon, not a current snapshot `.osm.pbf`.

1. Create a free [OpenStreetMap account](https://www.openstreetmap.org/user/new).
2. Open the regional page on [download.geofabrik.de](https://download.geofabrik.de) (Ohio demo: [north-america/us/ohio.html](https://download.geofabrik.de/north-america/us/ohio.html)).
3. Scroll to **Other Formats and Auxiliary Files**. The history file (`.osh.pbf`) is served from Geofabrik's **internal** server and requires an OSM login: [osm-internal.download.geofabrik.de](https://osm-internal.download.geofabrik.de/).
4. Click **internal server**, sign in with your OSM account, and download the previously crossed-out `*-internal.osh.pbf` file.

Keep `*.osh.pbf` out of git (already in `.gitignore`). These files are large and carry OSM contributor metadata.

## Demo polygon

`poly/campus.poly` is an **approximate bounding box** around the Ohio State University Columbus campus, in [Osmosis polygon](https://wiki.openstreetmap.org/wiki/Osmosis/Polygon_Filter_File_Format) format (`lon lat` rings). It is meant for the demo, not a cadastral campus boundary. The polygon must sit inside the history file you download (Ohio, in this case).

To search a different region, write another `.poly` or grab a regional poly from Geofabrik's "Other Formats" section.

## Step 1: extract construction sites

`cssic setup` (or `python workspace.py` / `from workspace import setup_directory`) creates:

```
temp/snapshots/
output/collection/
```

Then extract sites that were under construction between `--start` and `--end`. **Default `--backend ohsome`** asks HeiGIT's ohsome API for construction-tag lifetimes inside the poly. No `.osh.pbf` required. Completed sites are written as GeoPackage, GeoJSON, and Shapefile under `output/collection/collection.*`.

ohsome tracks each OSM id. It does not reconstruct the 2020 WayChain ID-rewiring / prev-final tag logic. For that paper-faithful path, use `--backend osmium --region history.osh.pbf`.

```bash
cssic extract -s 2018-04-15 -e 2020-07-01 --poly poly/campus.poly
cssic extract -s 2018-04-15 -e 2020-07-01 --poly poly/campus.poly \
  --backend osmium --region path/to/ohio-internal.osh.pbf
```

| Flag | Meaning |
| --- | --- |
| `-s` / `--start` | Search start (`YYYY-MM-DD`), on or after **2015-06-22** |
| `-e` / `--end` | Search end, at least **10 days before today** |
| `-p` / `--poly` | Osmosis `.poly` of the search region |
| `--backend` | `ohsome` (default) or `osmium` |
| `-r` / `--region` | Geofabrik history `.osh.pbf` (**osmium only**) |
| `--keep-temp` | Keep daily osmium snapshots (useful for QGIS) |
| `--restrict-window` | Do not walk before start / after end to recover true dates |
| `--save-wip` | Also save in-progress sites as `output/collection/in_progress.*` |

Each completed site also gets `output/{chain_id}/info.txt`:

`start_date,end_date,previous_tag,final_tag,minx,miny,maxx,maxy`

`cssic reset-extract` deletes extract outputs (including `output/collection/`). Copy anything you want to keep first.

Python equivalent:

```python
from workspace import setup_directory
import extract_sites

setup_directory()
extract_sites.set_params({
    "start": "2018-04-15",
    "end": "2020-07-01",
    "poly": "poly/campus.poly",
})
_, collection_gdf = extract_sites.locate_construction()
extract_sites.create_dataset(collection_gdf)
```

Osmium / paper-faithful path: add `"backend": "osmium"` and `"region": "ohio-internal.osh.pbf"` to the dict. You can also pass that dict straight to `locate_construction(params)` without `set_params`. `python extract_sites.py` still accepts the same flags as `cssic extract`.

## Step 2: gather imagery

Gather **at least one** of `--rgb` / `--nir`. Images are sampled evenly across each site's construction window (including start and end). `--num-images` must be `>= 3`, or `-1` for every available date.

```bash
# Sentinel-2 via Planetary Computer STAC (no CDSE key)
cssic gather -s stac --rgb --nir -n 3 --verbose

# Planet: place PSScene orders (visual + analytic)
cssic gather -s p --rgb --nir -n 3 --verbose

# Later, download completed Planet orders
cssic gather -s p --rgb --nir --download

# Sentinel-2 L2A via Copernicus Data Space Process API
cssic gather -s s --rgb --nir -n 3 --verbose
```

| Flag | Meaning |
| --- | --- |
| `-s` / `--source` | `stac`, `p` / `planet`, or `s` / `sentinel` |
| `-n` / `--num-images` | Samples per site (`>= 3` or `-1`); default `3` |
| `-p` / `--padding` | Area scale of the bounding box (center-invariant; `1` = exact box) |
| `-C` / `--rgb` | Visible chips |
| `-N` / `--nir` | Near-infrared chips |
| `-v` / `--verbose` | Progress logs |
| `-e` / `--email` | Planet email when an order is ready |
| `--api` | Planet API key (otherwise `PLANET_API_KEY` or `planet auth login`) |
| `--download` / `--download-planet` | Download previously placed Planet orders |
| `--sh-client-id` / `--sh-client-secret` | CDSE / Sentinel Hub OAuth (otherwise `.env`) |
| `--sentinel-provider` | `cdse` (default) or commercial `sentinelhub` |

Planet imagery is a two-step **order then download**. Sites that started before **2017-02-19** are skipped. Sentinel-2 STAC and Process API skip sites before **2015-06-23**.

If you already know the imagery source, set extract `--start` to the day before that cutoff (**2015-06-22** Sentinel, **2017-02-18** Planet) and pass `--restrict-window`.

`cssic reset-images` deletes downloaded chips but leaves the collection table.

Python equivalent (keys stay in the environment):

```python
import gather_images

gather_images.set_params({
    "source": "p",
    "rgb": True,
    "nir": True,
    "num-images": 3,
    "verbose": True,
})
geodf = gather_images.setup()
gather_images.gather_from_source(geodf)

# After Planet orders succeed:
gather_images.set_params({
    "source": "p",
    "rgb": True,
    "nir": True,
    "num-images": 3,
    "verbose": True,
    "download-planet": True,
})
gather_images.gather_from_source(geodf)
```

`python gather_images.py` still accepts the same flags as `cssic gather`.

## Output

RGB / NIR chips land at:

`output/{chain_id}/images/{planet|sentinel}/{rgb|nir}/{date_acquired}.png`

Planet RGB may include `{date}_mask.png` (alpha / unusable pixels). Planet NIR may include `{date}_udm.tif` (unusable-data mask). If `--num-images -1`, days with no scene get `{date}_empty.png`.

## How extraction works

**Default (`--backend ohsome`)** POSTs the poly bbox to HeiGIT's [ohsome](https://docs.ohsome.org/ohsome-api/stable/) `elementsFullHistory/geometry` with filter `(landuse=construction or building=construction) and geometry:polygon`. Consecutive versions of the same OSM id are merged into one interval. Rows still tagged construction at `--end` are treated as in-progress (`prev_tag` / `final_tag` are `N/A`). ohsome does not reconstruct construction *chains* across OSM id changes.

**`--backend osmium`** is the ICCVW 2021 algorithm. `osmium` first clips the history file to the poly, then keeps ways that were tagged construction. The extractor takes daily snapshots, walks backward from `--start` to recover true start dates, records changes inside the window, then walks forward after `--end` (up to 10 days ago) to see whether in-window sites later completed. New sites found after the window are not added.

Each GeoDataFrame row has `chain_id`, `start`, `end`, `constr_tag`, `prev_tag`, `final_tag`, and a bounding-box polygon. On the osmium path, previous / final tags are estimated from overlapping ways on the day before start and the day after end, ranked by intersection-over-union against a confidence threshold. Weak overlaps become `NO TAG FOUND`. A "construction chain" is the uncommon case where a finished site is immediately tagged construction again; those links are only kept if IOU exceeds `construction_chain_confidence`, so a previous/final tag is never literally `construction`.

Imagery dates are spaced as evenly as possible across `[start, end]`. Sentinel-2 revisits about every 5 days from 2015-06-23; Planet PSScene is daily after 2017-02-19, but coverage is thin in 2017. If a requested day has no scene, the next available image within a few days is used.

## License

[GPL-3.0](LICENSE).
