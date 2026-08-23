# Third-party API notes (Planet, Sentinel, STAC, pyosmium, GeoPandas)

Reference notes on the **external** APIs `cssic` calls, recorded because several
of them changed in ways that silently break 2020-era code. This is not project
documentation — for that see [ARCHITECTURE.md](ARCHITECTURE.md) and the
[README](../README.md). Each section ends with a pointer to the module that uses
it.

Verified against the versions pinned in `uv.lock`, on 2026-08-17:

| Package | Version |
| --- | --- |
| `planet` | **3.6.0** |
| `sentinelhub` | **3.11.5** |
| `osmium` (pyosmium) | **4.3.1** |
| `pystac-client` | **0.9.0** |
| `planetary-computer` | **1.0.0** |
| `rasterio` | **1.5.1** on Python 3.12+, **1.4.4** on 3.10 / 3.11 |
| `geopandas` | **1.1.4** |
| `pyogrio` | **0.13.0** |

The Planet Orders bundle spec below was fetched live from
`https://api.planet.com/compute/ops/bundles/spec` via the SDK’s lazy loader (no
API key required for that GET).

Do **not** put secrets in this file. Use env vars / `.env` (gitignored).

---

## 1. Planet Python SDK v3 (`planet` 3.6.0)

There are two client layers:

- **Sync (what `cssic` uses):** `planet.Planet` — methods return dicts / iterators. Internally they drive an `httpx.AsyncClient` on a background loop via `Session._call_sync`.
- **Async (do not mix into CLI code):** `async with planet.Session(...) as sess:` then `sess.client("data")` / `sess.client("orders")`, whose methods are `async` (`DataClient.search`, `OrdersClient.create_order`, …).

`Planet().data.search` and `Planet().orders.create_order` are **synchronous**. They are **not** coroutines.

### 1.1 Exact sync constructor (legacy API key)

This is the constructor `cssic.imagery.planet._planet_client` uses:

```python
from planet import Auth, Planet, Session

# Preferred (matches docs + type hints):
client = Planet(Session(auth=Auth.from_key(api_key)))

# Also valid: Session.__init__(self, auth=None, read_timeout_secs=None)
# so the Auth instance may be positional:
client = Planet(Session(Auth.from_key(api_key)))
```

`Planet.__init__(self, session: Optional[Session] = None, base_url: Optional[str] = None)` stores the session and attaches:

- `client.data` → `planet.sync.data.DataAPI`
- `client.orders` → `planet.sync.orders.OrdersAPI`

Empty keys fail fast:

```python
Auth.from_key("")  # raises planet.auth.APIKeyAuthException: API key cannot be empty.
```

**Do not** wrap the sync client in `async with Session(...)`. That path is for the async clients.

### 1.2 Env vars: `PL_API_KEY` vs `PL_AUTH_API_KEY`

| Variable | What 3.6.0 actually does |
| --- | --- |
| `PL_API_KEY` | **Real** env var. `Auth.from_env()` reads it. `Auth.from_user_default_session()` / `Planet()` also read it via `planet_auth_utils.EnvironmentVariables.AUTH_API_KEY`, which is namespaced to `"PL_API_KEY"`. |
| `PL_AUTH_API_KEY` | Appears **only** in the `Auth.from_user_default_session` docstring. It is **not** referenced in installed `planet` / `planet_auth` / `planet_auth_utils` code. Setting it does nothing unless our wrapper reads it. |
| `PL_AUTH_CLIENT_ID` + `PL_AUTH_CLIENT_SECRET` | OAuth2 M2M, considered by `from_user_default_session()` (higher priority than API key). |
| `PL_AUTH_PROFILE` | Named CLI profile under `~/.planet.json` / `~/.planet/`. |

Default client (no explicit key):

```python
client = Planet()  # Session() -> Auth.from_user_default_session()
```

`Auth.from_env()` still exists but is **pending deprecation** in favor of `Auth.from_user_default_session()`.

`cssic.config.Credentials.from_env` also accepts `PLANET_API_KEY` (project-specific, not an SDK var) and `PL_AUTH_API_KEY` (docstring-only). That is fine as a wrapper; the SDK itself only natively honors `PL_API_KEY` for legacy keys.

CLI equivalent:

```bash
planet auth login --auth-api-key "$PL_API_KEY"
```

### 1.3 Sync search and order methods (actual signatures)

```python
from datetime import datetime, timezone
from pathlib import Path

from planet import Auth, Planet, Session, data_filter, order_request
from planet.exceptions import APIError, ClientError

client = Planet(Session(auth=Auth.from_key(api_key)))

# DataAPI.search -> Iterator[dict]
# sort must be one of: 'published desc', 'published asc', 'acquired desc', 'acquired asc'
# limit=0 means no maximum (not "zero results")
for item in client.data.search(
    ["PSScene"],
    search_filter=and_filter,   # optional Dict
    name=None,
    sort="acquired asc",
    limit=250,
    geometry=None,              # GeoJSON / feature ref
):
    item_id = item["id"]
    acquired = item["properties"]["acquired"]  # RFC3339
    geom = item["geometry"]

# OrdersAPI.create_order(request: Dict) -> Dict  (includes ["id"], ["state"])
order = client.orders.create_order(request)

# OrdersAPI.get_order(order_id: str) -> Dict
order = client.orders.get_order(order["id"])

# OrdersAPI.wait(...) -> str (final or requested state)
# delay=5, max_attempts=200, max_attempts=0 means unlimited
state = client.orders.wait(order["id"], delay=5, max_attempts=200)

# OrdersAPI.download_order(...) -> List[Path]
# directory: pathlib.Path, MUST already exist
# overwrite: bool = False
downloaded = client.orders.download_order(
    order["id"],
    directory=Path("output/tmp"),
    overwrite=True,
    progress_bar=False,
)
```

`download_order` refuses non-final states (`queued` / `running`) with `ClientError`. Final states include `success`, `failed`, `partial`. Files are written under `directory / <per-asset-subdir>` using names from the order manifest — walk with `Path.rglob`.

### 1.4 Filters (`planet.data_filter`)

```python
from datetime import datetime, timezone
from planet import data_filter

geom = {
    "type": "Polygon",
    "coordinates": [[
        [-83.0, 40.0], [-83.0, 40.1], [-82.9, 40.1], [-82.9, 40.0], [-83.0, 40.0]
    ]],
}

# geometry_filter(geom: dict, relation: Optional[str] = None) -> dict
# relation in {"intersects", "contains", "disjoint", "within"}; default intersects
geo_f = data_filter.geometry_filter(geom)
# Feature / FeatureCollection also accepted; geometry is extracted.

# date_range_filter(field_name, gt=None, lt=None, gte=None, lte=None) -> dict
# MUST be datetime.datetime (naive is OK; SDK appends "Z").
# str ISO dates and datetime.date BOTH crash in 3.6.0 (_datetime_to_rfc3339).
start = datetime(2017, 2, 19, tzinfo=timezone.utc)
end = datetime(2017, 2, 25, 23, 59, 59, tzinfo=timezone.utc)
date_f = data_filter.date_range_filter("acquired", gte=start, lte=end)

# permission_filter() -> {'type': 'PermissionFilter', 'config': ['assets:download']}
perm_f = data_filter.permission_filter()

# and_filter(nested_filters: List[dict]) -> dict
and_filter = data_filter.and_filter([geo_f, date_f, perm_f])
```

### 1.5 Order request builders (`planet.order_request`)

**Item type must be `PSScene`.** Installed spec (2026-08-17) item types:

`PSScene`, `PelicanScene`, `REOrthoTile`, `REScene`, `SkySatCollect`, `SkySatScene`, `TanagerMethane`, `TanagerScene`

`PSOrthoTile` and `PSScene4Band` raise `SpecificationException`. RapidEye still has `REOrthoTile` / `REScene`; those are not PlanetScope.

```python
from planet import order_request

# product(item_ids, product_bundle, item_type, fallback_bundle=None) -> dict
# fallback_bundle: str (comma-separated) or list[str]
product = order_request.product(
    item_ids=["20200925_161029_69_2223"],
    product_bundle="visual",          # or "analytic_udm2"
    item_type="PSScene",              # case-insensitive; SDK normalizes
    fallback_bundle="analytic_udm2",  # optional
)

clip = order_request.clip_tool(aoi)  # Polygon or MultiPolygon GeoJSON
reproject = order_request.reproject_tool(
    projection="EPSG:4326",
    resolution=None,   # optional float
    kernel="near",     # default API kernel is "near"; also bilinear/cubic/...
)

notifications = order_request.notifications(email=True)  # or webhook_url=...

request = order_request.build_request(
    name="site-chain-id"[:100],
    products=[product],
    tools=[clip, reproject],          # order = toolchain order
    notifications=notifications,      # omit or None to skip
    order_type="partial",             # "full" | "partial"; None -> API default "full"
)
```

`order_type="partial"` delivers every item whose **complete** bundle is available and drops the rest, instead of failing the whole order.

Clip AOI must be Polygon/MultiPolygon (or a Features API `ref`). Invalid types raise `ClientError`.

### 1.6 Valid PSScene bundles for RGB and NIR (live spec)

PSScene bundles currently accepted by `order_request.product(..., "PSScene")`:

| Bundle | Assets | Use |
| --- | --- | --- |
| **`visual`** | `ortho_visual` | RGB, 8-bit, color-balanced. **Use for `--rgb`.** |
| **`analytic_udm2`** | `ortho_analytic_4b`, `ortho_analytic_4b_xml`, `ortho_udm2` | 4-band B,G,R,NIR TOAR 16-bit + UDM2. **Use for `--nir` (band 4 / index 3).** |
| `analytic_sr_udm2` | `ortho_analytic_4b_sr`, xml, `ortho_udm2` | Same 4-band but surface reflectance. Better analytic fallback / alternative for NIR. |
| `analytic_8b_udm2` | `ortho_analytic_8b`, xml, `ortho_udm2` | 8-band TOAR; NIR is last band (index 7). |
| `analytic_8b_sr_udm2` | `ortho_analytic_8b_sr`, xml, `ortho_udm2` | 8-band SR. |
| `analytic_3b_udm2` | `ortho_analytic_3b`, xml, `ortho_udm2` | 3-band analytic (no NIR). Do not use for `--nir`. |
| `basic_analytic_udm2` / `basic_analytic_8b_udm2` | non-ortho | **Incompatible with clip/reproject.** Do not order. |

Dead / rejected:

- `analytic` (no `_udm2`) → `SpecificationException`
- `PSOrthoTile`, `PSScene4Band` as `item_type`

Recommended for this project:

```python
# RGB
order_request.product(ids, "visual", "PSScene")

# NIR (4-band analytic + UDM2), with SR as the reflectance fallback
order_request.product(
    ids, "analytic_udm2", "PSScene",
    fallback_bundle="analytic_sr_udm2",  # or reverse: SR first, TOAR fallback
)
```

`fallback_bundle` is concatenated into `product_bundle` as `"analytic_udm2,analytic_sr_udm2"`.

`visual` with fallback `analytic_udm2` is only safe if the download step can turn `AnalyticMS` files into RGB — they are 4-band analytic, not visual RGB. `cssic.imagery.planet` uses that fallback and handles the case: `rewrite_tif` synthesises true colour from the analytic bands (`analytic_rgb`) when an RGB chip was requested and no visual file arrived for that date.

### 1.7 Typical downloaded filenames (PSScene, clip + reproject)

Base names (June 2026 PSScene product spec):

```
{YYYYMMDD}_{HHMMSS}_{sat}_3B_Visual.tif
{YYYYMMDD}_{HHMMSS}_{sat}_3B_AnalyticMS.tif          # analytic_udm2 (TOAR 4b)
{YYYYMMDD}_{HHMMSS}_{sat}_3B_AnalyticMS_SR.tif       # analytic_sr_udm2
{YYYYMMDD}_{HHMMSS}_{sat}_3B_AnalyticMS_8b.tif       # analytic_8b_udm2
{YYYYMMDD}_{HHMMSS}_{sat}_3B_udm2.tif                # ortho_udm2
```

Orders tools append suffixes **in toolchain order**:

- clip → `_clip` (skipped if the AOI does not actually clip the scene)
- reproject → `_reproject`

`cssic` orders `tools=[clip_tool(...), reproject_tool(...)]` — clip first, then reproject, which is the usual toolchain (fewer pixels resampled):

```
20230207_143613_03_241c_3B_Visual_clip_reproject.tif
20230207_143613_03_241c_3B_AnalyticMS_clip_reproject.tif
20230207_143613_03_241c_3B_udm2_clip_reproject.tif
```

The reverse toolchain is equally valid API and just swaps the suffixes
(`..._reproject_clip.tif`), so nothing downstream may depend on suffix order. A
`YYYYMMDD` regex on the filename matches either way, and classification uses the
`visual` / `analytic` / `udm` substrings (`classify_planet_file`). Downloads also
include XML/JSON sidecar files — ignore non-TIFF.

---

## 2. Sentinel Hub / Copernicus Data Space (CDSE)

### 2.1 Free path: CDSE OAuth client

Create an OAuth client (not a configuration `instance_id`) at:

https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings

Copy the client id + secret once (secret is shown only at creation). Set `SH_CLIENT_ID` / `SH_CLIENT_SECRET`. Commercial Sentinel Hub (`https://apps.sentinel-hub.com`) is a different dashboard and a different token URL.

### 2.2 Exact `SHConfig` fields for CDSE

Defaults in `sentinelhub` 3.11.5 point at **commercial** Sentinel Hub (`https://services.sentinel-hub.com`). CDSE must override base + token URLs:

```python
from sentinelhub import SHConfig

config = SHConfig()
config.sh_client_id = client_id          # or env SH_CLIENT_ID
config.sh_client_secret = client_secret  # or env SH_CLIENT_SECRET
config.sh_base_url = "https://sh.dataspace.copernicus.eu"
config.sh_token_url = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
# Do not set instance_id for Process/Catalog.
# Do not set sh_auth_base_url (deprecated; it overwrites sh_token_url).
```

Field meanings:

| Field | CDSE value |
| --- | --- |
| `sh_client_id` | OAuth client id |
| `sh_client_secret` | OAuth client secret |
| `sh_base_url` | `https://sh.dataspace.copernicus.eu` |
| `sh_token_url` | CDSE identity token URL above |
| `instance_id` | **Unused** for Process API / Catalog. Required only by OGC `WcsRequest` / `WmsRequest`. |

Env vars auto-loaded by `SHConfig()`: `SH_CLIENT_ID`, `SH_CLIENT_SECRET`, `SH_PROFILE`.

### 2.3 `DataCollection.SENTINEL2_L2A.define_from(...)`

Stock `SENTINEL2_L2A` has `service_url=https://services.sentinel-hub.com`. For CDSE, clone it:

```python
from sentinelhub import DataCollection

collection = DataCollection.SENTINEL2_L2A.define_from(
    "s2l2a_cdse",
    service_url="https://sh.dataspace.copernicus.eu",
)
# api_id / catalog_id stay "sentinel-2-l2a"
```

`define_from(self, name: str, **params)` accepts any `DataCollectionDefinition` field (`service_url`, `api_id`, …).

### 2.4 Process API: true-color RGB + B08 NIR, `bbox_to_dimensions`, leastCC

```python
from sentinelhub import (
    BBox, CRS, DataCollection, MimeType, MosaickingOrder,
    SentinelHubRequest, bbox_to_dimensions,
)

TRUE_COLOR = """
//VERSION=3
function setup() {
  return {
    input: ["B02", "B03", "B04", "dataMask"],
    output: { bands: 3, sampleType: "AUTO" }
  };
}
function evaluatePixel(sample) {
  return [2.5 * sample.B04, 2.5 * sample.B03, 2.5 * sample.B02];
}
"""

NIR_B08 = """
//VERSION=3
function setup() {
  return {
    input: ["B08", "dataMask"],
    output: { bands: 1, sampleType: "FLOAT32" }
  };
}
function evaluatePixel(sample) {
  return [sample.B08];
}
"""

bbox = BBox(bbox=(minx, miny, maxx, maxy), crs=CRS.WGS84)
size = bbox_to_dimensions(bbox, resolution=10)  # (width, height) int px
size = (max(size[0], 1), max(size[1], 1))

request = SentinelHubRequest(
    evalscript=TRUE_COLOR,  # or NIR_B08
    input_data=[
        SentinelHubRequest.input_data(
            data_collection=collection,
            time_interval=("2020-06-01", "2020-06-10"),
            mosaicking_order=MosaickingOrder.LEAST_CC,  # first-class; value "leastCC"
            maxcc=1.0,  # Python helper: float in [0, 1] -> JSON maxCloudCoverage 0–100
        )
    ],
    responses=[SentinelHubRequest.output_response("default", MimeType.PNG)],  # TIFF for NIR
    bbox=bbox,
    size=size,
    config=config,
)
data = request.get_data()  # list; data[0] is the ndarray
```

Equivalent raw `dataFilter` (what `other_args` merges into):

```python
other_args = {"dataFilter": {"maxCloudCoverage": 100, "mosaickingOrder": "leastCC"}}
```

`MosaickingOrder`: `MOST_RECENT` (`mostRecent`), `LEAST_RECENT` (`leastRecent`), `LEAST_CC` (`leastCC`).

L2A B08 is surface reflectance (~0–1). Scale to 8-bit with `np.clip(array * 255.0, 0, 255)`.

### 2.5 Catalog: list dates in a window

```python
from sentinelhub import SentinelHubCatalog

catalog = SentinelHubCatalog(config=config)
dates = []
for item in catalog.search(
    collection,                 # DataCollection or catalog id str
    bbox=bbox,
    time=(start_iso, end_iso),  # keyword-only; str or datetime
    limit=100,
):
    acquired = item["properties"].get("datetime") or item["properties"].get("start_datetime")
    if acquired:
        dates.append(acquired[:10])
dates = sorted(set(dates))
```

Signature (keyword-only after `collection`):

`search(self, collection, *, time=None, bbox=None, geometry=None, ids=None, filter=None, filter_lang="cql2-text", filter_crs=None, fields=None, distinct=None, limit=100, **kwargs) -> CatalogSearchIterator`

The old Catalog `query=` parameter is removed (raises `ValueError`).

Use the **same** `define_from(..., service_url=CDSE)` collection so Catalog hits CDSE (`{sh_base_url}/api/v1/catalog/1.0.0`).

### 2.6 `instance_id` + `WcsRequest` OGC — do not use

`WcsRequest` / `WmsRequest` still exist in 3.11.5 and **require** `config.instance_id` (OGC configuration from the commercial dashboard). Official `sentinelhub-py` OGC notebook: *“Sentinel Hub service has a dedicated Process API that extends the limiting capabilities of the standard OGC endpoints. We advise you to move to Process API.”*

CDSE free access is OAuth Process API + Catalog. **Do not** revive `SENTINEL_INSTANCE_ID` / `WcsRequest` / preconfigured `TRUE-COLOR` / `NIR` layers.

---

## 3. Planetary Computer STAC (`pystac-client` 0.9, `planetary-computer` 1.0)

The default imagery path (`cssic gather -s stac`). No account, no key: the
Planetary Computer STAC API is open and asset hrefs are signed on the way out.

### 3.1 Signing: use the client modifier, not a per-href call

```python
import planetary_computer
from pystac_client import Client

client = Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)
```

`modifier=` signs every item the client hands back, so `item.assets[key].href`
is already readable. Signatures expire (hours), so sign at read time — do not
cache signed hrefs across a long run.

### 3.2 Search

```python
search = client.search(
    collections=["sentinel-2-l2a"],
    intersects=mapping(aoi),                 # GeoJSON, EPSG:4326
    datetime=f"{start.isoformat()}/{end.isoformat()}",
    query={"eo:cloud_cover": {"lt": 40}},    # omit entirely to keep everything
    sortby=[{"field": "properties.datetime", "direction": "asc"}],
)
for item in search.items():
    ...
```

`intersects` also returns scenes that only clip a corner of a small AOI; the
missing part reads back as nodata fill. Prefer an item whose footprint
`covers()` the AOI and fall back to a partial one only when nothing covers it.

### 3.3 Baseline 04.00 `BOA_ADD_OFFSET` — the silent one

From ESA processing baseline 04.00 (25 January 2022), L2A digital numbers carry
an offset: reflectance is `(DN + BOA_ADD_OFFSET) / 10000` with
`BOA_ADD_OFFSET = -1000`. Code written before 2022 renders every later scene
about 10% too bright.

```python
baseline = float(item.properties.get("s2:processing_baseline", 0))
offset = -1000.0 if baseline >= 4.0 else 0.0
```

The offset applies to the **raw** bands (`B02`…`B08`). The pre-rendered
`visual` asset is already 8-bit and already corrected — do not offset it again.

### 3.4 Reading a window out of a remote COG

Sentinel-2 COGs are in UTM; an AOI in EPSG:4326 must be reprojected into
`dataset.crs` *before* windowing, or rasterio reports `Input shapes do not
overlap raster`.

```python
import rasterio

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",   # one range request, not a listing
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSOLIDATED_RANGES": "YES",
}
with rasterio.Env(**GDAL_ENV), rasterio.open(signed_href) as src:
    ...
```

Read a plain pixel **window**, not `rasterio.mask.mask(crop=True)`: masking
rasterizes with centre-in-polygon semantics and fills the outer ring of the
crop with nodata, which on chips this small is most of the image.

### 3.5 `SCL`: the only cloud number that is about your site

Every `sentinel-2-l2a` item publishes an `SCL` asset — the L2A Scene
Classification Layer, 20 m, one band. Windowing it costs one extra small range
request and answers what `eo:cloud_cover` cannot: how much of *this* AOI is
obscured, rather than how much of a 110 km tile is. Unusable classes:

| Value | Class |
| --- | --- |
| 3 | cloud shadow |
| 8 | cloud, medium probability |
| 9 | cloud, high probability |
| 10 | thin cirrus |
| 11 | snow / ice |

`cssic.imagery.stac` picks a window's acquisition on that fraction (class 0,
no data, is excluded from the denominator), after collapsing reprocessed
duplicates of one overpass and ranking at most the five acquisitions that
`eo:cloud_cover` already likes best.

Used by `cssic.imagery.stac` and `cssic.imagery.chips`.

---

## 4. pyosmium 4 (`osmium` 4.3.1)

### 4.1 `InvalidLocationError` import path

Public export (this is the one to use):

```python
from osmium import InvalidLocationError
# class osmium._osmium.InvalidLocationError  — re-exported in osmium/__init__.py
```

Still importable as `from osmium._osmium import InvalidLocationError`. The pyosmium 3 path `osmium._osmium` remains, but the top-level import is stable in 4.3.1.

### 4.2 `Area.from_way()`, `orig_id()`, `WKBFactory.create_multipolygon`

All still valid on `osmium.osm.Area` / `osmium.geom.WKBFactory`:

```python
import osmium
import shapely.wkb as wkblib
from osmium import InvalidLocationError

class Handler(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.wkbfab = osmium.geom.WKBFactory()

    def area(self, a):
        if a.from_way():          # bool: True if built from a closed way
            osm_id = a.orig_id()  # original way id (int)
        else:
            osm_id = int(a.id)    # area id space; orig_id() still returns the relation id
        try:
            wkb = self.wkbfab.create_multipolygon(a)  # -> hex WKB str
            geom = wkblib.loads(wkb, hex=True)
        except InvalidLocationError:
            return
        except RuntimeError:
            return
```

`SimpleHandler.apply_file`: if an `area` callback exists, pyosmium scans the file twice and installs a location handler + area assembler. That is unchanged.

Used by `cssic.history.osmium`, which also shells out to the separate
**osmium-tool** binary (`osmium extract` / `osmium time-filter`); the Python
wheel does not provide those commands.

---

## 5. GeoPandas 1.x (`geopandas` 1.1.4)

### 5.1 Recommended drivers: GPKG + GeoJSON vs Shapefile

With `pyogrio` installed, the default engine is **pyogrio**.

| Driver | Recommendation |
| --- | --- |
| **GPKG** (`driver="GPKG"`) | Primary. Single file, no 10-char field-name cap, mixed geometry, metadata dict supported. |
| **GeoJSON** (`driver="GeoJSON"`) | Interchange / git-friendly. |
| **ESRI Shapefile** | Compatibility only. Truncates column names to 10 chars (`chain_id`, `constr_tag`, `final_tag` are OK; longer names are not), weak datetime, sidecar files (`.shx/.dbf/.prj`). |

Do not rely on Shapefile as the only collection format. `cssic.store.Workspace.collection_path()` prefers `collection.gpkg`, then `.geojson`, then `.shp`, and `save_collection()` writes all three.

### 5.2 `to_file` API

```python
gdf.to_file(
    filename,                 # path-like; first positional. Keyword filename= still works.
    driver=None,              # inferred from suffix if omitted
    schema=None,              # Fiona only; ignored by pyogrio
    index=None,
    engine="pyogrio",         # or "fiona"; default pyogrio if installed
    mode="w",                 # 'w' overwrite, 'a' append (driver-dependent)
    metadata={"foo": "bar"},  # GPKG only
)
```

Copy-paste:

```python
gdf.to_file(collection_dir / f"{stem}.gpkg", driver="GPKG")
gdf.to_file(collection_dir / f"{stem}.geojson", driver="GeoJSON")
# optional compatibility copy:
gdf.to_file(collection_dir / f"{stem}.shp")  # infers ESRI Shapefile
```

Avoid the old-only style `to_file(driver="ESRI Shapefile", filename=...)` for new code; positional path + `driver=` is the 1.x form.

---

## 6. Where `cssic` uses each of these

| Note | Module |
| --- | --- |
| §1 Planet SDK v3 — sync client, filters, bundles, order tools, filenames | `cssic/imagery/planet.py` |
| §2 Sentinel Hub / CDSE Process API + Catalog | `cssic/imagery/sentinelhub.py` |
| §3 Planetary Computer STAC, `BOA_ADD_OFFSET`, COG windowing | `cssic/imagery/stac.py`, `cssic/imagery/chips.py` |
| §4 pyosmium 4 areas and geometry | `cssic/history/osmium.py` |
| §5 GeoPandas 1.x `to_file` drivers | `cssic/store.py` |
| Env vars / `.env` | `cssic/config.py` (`Credentials.from_env`) |

Choices worth remembering, all of them still valid against the versions above:

- `ITEM_TYPE = "PSScene"`. `PSOrthoTile` and `PSScene4Band` raise
  `SpecificationException`; OrthoTile / 4Band are gone.
- `data.search(..., sort="acquired asc", limit=0)` — `limit=0` means unbounded,
  not "no results". `cssic` passes `0` only for `-n -1`.
- Planet date filters are built from `datetime` objects, never ISO strings or
  `datetime.date`: both crash inside `_datetime_to_rfc3339` in 3.6.0.
- `order_type="partial"` delivers whatever items have a complete bundle instead
  of failing the whole order.
- `orders.wait()` is not used: `cssic gather -s p` places orders, logs them to
  `temp/order_log.txt`, and a later `--download` run collects whatever has
  reached `success` / `partial`. `download_order` refuses non-final states.
  "Delivered" is not "collected": an order whose delivery yields no usable chip
  (unreadable GeoTIFF, nothing but UDM masks, pure cloud) stays in
  `temp/order_log.txt` with its staging directory, instead of being retired to
  `temp/order_log_complete.txt`.
- The `Session` is never explicitly closed; acceptable for a short CLI process
  (the background loop thread is a daemon).
- Sentinel Hub OGC (`WcsRequest` / `WmsRequest` / `instance_id`) is deliberately
  unused — see §2.6.
- The collection is written as GPKG **and** GeoJSON **and** Shapefile, with GPKG
  as the source of truth; Shapefile is a compatibility copy only (§5.1).

---

## 7. Copy-paste: minimal Planet sync order

A standalone sketch, kept because it is the shortest correct end-to-end use of
the SDK. `cssic` splits the same steps across `create_order` / `bulk_order` and
a later `download_orders`, and so does not block on `orders.wait`.

```python
from datetime import datetime, timezone
from pathlib import Path

from planet import Auth, Planet, Session, data_filter, order_request

def planet_client(api_key: str | None) -> Planet:
    if api_key:
        return Planet(Session(auth=Auth.from_key(api_key)))
    return Planet()  # PL_API_KEY / planet auth login / ~/.planet.json

def search_and_order(client: Planet, aoi: dict, start: datetime, end: datetime, item_ids_out: list[str]):
    filt = data_filter.and_filter([
        data_filter.geometry_filter(aoi),
        data_filter.date_range_filter("acquired", gte=start, lte=end),
        data_filter.permission_filter(),
    ])
    for item in client.data.search(["PSScene"], search_filter=filt, sort="acquired asc", limit=250):
        item_ids_out.append(item["id"])

    products = [
        order_request.product(item_ids_out, "visual", "PSScene"),
        order_request.product(
            item_ids_out, "analytic_udm2", "PSScene",
            fallback_bundle="analytic_sr_udm2",
        ),
    ]
    request = order_request.build_request(
        name="construction-site",
        products=products,
        tools=[
            order_request.clip_tool(aoi),
            order_request.reproject_tool(projection="EPSG:4326", kernel="near"),
        ],
        order_type="partial",
    )
    order = client.orders.create_order(request)
    client.orders.wait(order["id"])
    out = Path("output/planet_tmp")
    out.mkdir(parents=True, exist_ok=True)
    return client.orders.download_order(order["id"], directory=out, overwrite=True)
```
