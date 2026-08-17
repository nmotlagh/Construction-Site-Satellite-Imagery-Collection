"""Download RGB/NIR imagery of extracted construction sites from Planet or Sentinel-2."""

from __future__ import annotations

import sys
from datetime import date

import geopandas as gpd
import numpy as np
from PIL import Image
from shapely import affinity

from credentials import planet_credentials, sentinel_credentials
from dates import padding_scale, sample_date_windows
from parsers import GATHER_FIELDS, build_gather_parser, namespace_to_dict
from planet_helper import PlanetHandlerV2, reflectance_to_uint8
from workspace import OUTPUT_DIR, collection_path, setup_directory

DEFAULT_PARAMETERS = {
    "rgb": False,
    "nir": False,
    "source": None,
    "num-images": 3,
    "padding": 1.0,
    "verbose": False,
    "email": False,
    "download-planet": False,
    "api": None,
    "sh-client-id": None,
    "sh-client-secret": None,
    "sentinel-provider": "cdse",
}

# Notebook / set_params store. CLI and typed callers pass a dict instead.
parameters = dict(DEFAULT_PARAMETERS)

output_dir = OUTPUT_DIR
SENTINEL_CUTOFF = date(2015, 6, 23)
CDSE_BASE = "https://sh.dataspace.copernicus.eu"
CDSE_TOKEN = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

TRUE_COLOR_EVALSCRIPT = """
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

NIR_EVALSCRIPT = """
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


def check_num_images(num_images):
    try:
        n = int(num_images)
        if n != -1 and n < 3:
            print(f"ERROR: num-images must be >=3 or -1. Got {num_images}")
            return None
        return n
    except (TypeError, ValueError):
        print(f"ERROR: num-images must be an int! Got {num_images}")
        return None


def check_source(s):
    aliases = {"s": "s", "sentinel": "s", "p": "p", "planet": "p", "stac": "stac"}
    if s not in aliases:
        print(f"ERROR: {s} is not a valid source. Choose 's'/'sentinel', 'stac', or 'p'/'planet'.")
        return None
    return aliases[s]


def check_padding(p):
    try:
        n = float(p)
        if n <= 0:
            print(f"ERROR: padding must be >0. Got {n}")
            return None
        return n
    except (TypeError, ValueError):
        print(f"ERROR: padding must be a number! Got {p}")
        return None


def _gather_cfg(p: dict | None) -> dict:
    return parameters if p is None else p


def normalize_gather_params(raw: dict) -> dict:
    """Validate a gather-params dict (hyphenated keys). Exits 2 on error."""
    out = dict(DEFAULT_PARAMETERS)
    mapping = {
        "num-images": check_num_images,
        "padding": check_padding,
        "source": check_source,
    }
    for key, checker in mapping.items():
        if key in raw:
            out[key] = checker(raw[key])
    for key in (
        "api",
        "verbose",
        "email",
        "download-planet",
        "nir",
        "rgb",
        "sh-client-id",
        "sh-client-secret",
        "sentinel-provider",
    ):
        if key in raw and raw[key] is not None:
            out[key] = raw[key]
    for key, value in out.items():
        if value is None and key in {"source", "num-images", "padding"}:
            print(f"ERROR: Provide a correct value for {key}!")
            sys.exit(2)
    if not out["rgb"] and not out["nir"]:
        print("ERROR: Select at least one of --rgb / --nir")
        sys.exit(2)
    return out


def set_params(params):
    global parameters
    parameters = normalize_gather_params(params)


def params_from_args(args) -> dict:
    """Validate an argparse namespace (cssic gather or gather_images.py) into a params dict."""
    return normalize_gather_params(namespace_to_dict(args, GATHER_FIELDS))


def get_input(argv=None):
    """Parse command-line arguments for imagery gather (python gather_images.py)."""
    return params_from_args(build_gather_parser().parse_args(argv))


def _sentinel_config(creds):
    from sentinelhub import SHConfig

    config = SHConfig()
    if not creds.client_id or not creds.client_secret:
        sys.exit(
            "Sentinel credentials missing. Create an OAuth client at "
            "https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings "
            "and set SH_CLIENT_ID / SH_CLIENT_SECRET (see .env.example)."
        )
    config.sh_client_id = creds.client_id
    config.sh_client_secret = creds.client_secret
    if creds.provider == "cdse":
        config.sh_base_url = CDSE_BASE
        config.sh_token_url = CDSE_TOKEN
    return config


def _sentinel_collection(config):
    from sentinelhub import DataCollection

    collection = DataCollection.SENTINEL2_L2A
    if config.sh_base_url == CDSE_BASE:
        collection = DataCollection.SENTINEL2_L2A.define_from("s2l2a_cdse", service_url=CDSE_BASE)
    return collection


class SentinelHandler:
    """Process API client for Sentinel-2 L2A RGB and NIR chips."""

    def __init__(self, creds, params, gdf):
        self.config = _sentinel_config(creds)
        self.collection = _sentinel_collection(self.config)
        self.params = params
        self.gdf = gdf

    def get_dates_sentinel(self, start, end):
        return sample_date_windows(
            date.fromisoformat(start),
            date.fromisoformat(end),
            self.params["num-images"],
            day_padding=6,
        )

    def _bbox_for_row(self, row):
        from sentinelhub import BBox, CRS

        scale = padding_scale(self.params["padding"])
        bounds = affinity.scale(row["geometry"], xfact=scale, yfact=scale).bounds
        return BBox(bbox=bounds, crs=CRS.WGS84)

    def _request_image(self, bbox, time_interval, evalscript, mime, mosaicking="leastCC"):
        from sentinelhub import MimeType, SentinelHubRequest, bbox_to_dimensions

        size = bbox_to_dimensions(bbox, resolution=10)
        size = (max(size[0], 1), max(size[1], 1))
        other_args = {"dataFilter": {"maxCloudCoverage": 100, "mosaickingOrder": mosaicking}}
        request = SentinelHubRequest(
            evalscript=evalscript,
            input_data=[
                SentinelHubRequest.input_data(
                    data_collection=self.collection,
                    time_interval=time_interval,
                    other_args=other_args,
                )
            ],
            responses=[SentinelHubRequest.output_response("default", mime)],
            bbox=bbox,
            size=size,
            config=self.config,
        )
        data = request.get_data()
        return data[0] if data else None

    def _catalog_dates(self, bbox, start, end):
        from sentinelhub import SentinelHubCatalog

        catalog = SentinelHubCatalog(config=self.config)
        timestamps = []
        for item in catalog.search(self.collection, bbox=bbox, time=(start, end)):
            acquired = item["properties"].get("datetime") or item["properties"].get("start_datetime")
            if acquired:
                timestamps.append(acquired[:10])
        return sorted(set(timestamps))

    def get_bands_sentinel(self, row):
        from sentinelhub import MimeType

        bbox = self._bbox_for_row(row)
        chain_id = row["chain_id"]
        if self.params["num-images"] == -1:
            windows = [(d, d) for d in self._catalog_dates(bbox, row["start"], row["end"])]
            if not windows:
                if self.params["verbose"]:
                    print(f"\tNo Sentinel-2 scenes for {chain_id}")
                return
        else:
            windows = self.get_dates_sentinel(row["start"], row["end"])

        for window in windows:
            time_interval = (str(window[0]), str(window[1]))
            label = str(window[0])[:10]
            if self.params["nir"]:
                nir = self._request_image(bbox, time_interval, NIR_EVALSCRIPT, MimeType.TIFF)
                if nir is None:
                    print(f"FAILED TO GET NIR IMAGE {time_interval[0]}-{time_interval[1]}")
                else:
                    img_path = output_dir / f"{chain_id}/images/sentinel/nir/{label}.png"
                    Image.fromarray(reflectance_to_uint8(np.squeeze(np.array(nir)))).save(str(img_path))
            if self.params["rgb"]:
                rgb = self._request_image(bbox, time_interval, TRUE_COLOR_EVALSCRIPT, MimeType.PNG)
                if rgb is None:
                    print(f"FAILED TO GET RGB IMAGE {time_interval[0]}-{time_interval[1]}")
                else:
                    img_path = output_dir / f"{chain_id}/images/sentinel/rgb/{label}.png"
                    Image.fromarray(reflectance_to_uint8(np.array(rgb))).save(str(img_path))

    def get_all_imagery(self):
        for _, row in self.gdf.iterrows():
            if date.fromisoformat(row["start"]) < SENTINEL_CUTOFF:
                if self.params["verbose"]:
                    print(f"Start date for {row['chain_id']} is before 2015-06-23. Skipped!")
                continue
            if self.params["verbose"]:
                print(f"Getting imagery for {row['chain_id']}")
            self.get_bands_sentinel(row)


def setup(other_gdf=None, params=None):
    cfg = _gather_cfg(params)
    setup_directory()
    gdf = other_gdf
    if gdf is None:
        path = collection_path()
        if path is None:
            print("No construction sites found!")
            return None
        gdf = gpd.read_file(path)
    source_name = "planet" if cfg["source"] == "p" else "sentinel"
    for _, row in gdf.iterrows():
        source_path = output_dir / f"{row['chain_id']}/images/{source_name}"
        if cfg["rgb"]:
            (source_path / "rgb").mkdir(parents=True, exist_ok=True)
        if cfg["nir"]:
            (source_path / "nir").mkdir(parents=True, exist_ok=True)
    return gdf


def gather_from_source(gdf, params=None):
    cfg = _gather_cfg(params)
    if cfg["source"] == "p":
        creds = planet_credentials(cfg.get("api"))
        if not creds.api_key and not cfg["download-planet"]:
            print(
                "No Planet API key found. Set PLANET_API_KEY, pass --api, "
                "or run `planet auth login`."
            )
        handler = PlanetHandlerV2(creds.api_key, cfg, gdf)
        if cfg["download-planet"]:
            handler.download_orders()
        else:
            handler.bulk_order()
        return
    if cfg["source"] == "stac":
        from sentinel_stac import SentinelSTACHandler

        SentinelSTACHandler(cfg, gdf).get_all_imagery()
        return
    creds = sentinel_credentials(
        cfg.get("sh-client-id"),
        cfg.get("sh-client-secret"),
        cfg.get("sentinel-provider"),
    )
    SentinelHandler(creds, cfg, gdf).get_all_imagery()


if __name__ == "__main__":
    cfg = get_input()
    geodf = setup(params=cfg)
    if geodf is None:
        sys.exit(2)
    gather_from_source(geodf, cfg)
