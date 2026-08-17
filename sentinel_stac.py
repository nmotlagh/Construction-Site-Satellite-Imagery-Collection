"""Sentinel-2 chips via STAC (Planetary Computer), no Sentinel Hub instance id."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
from PIL import Image
from shapely import affinity

from dates import padding_scale, sample_date_windows
from workspace import OUTPUT_DIR

SENTINEL_CUTOFF = date(2015, 6, 23)
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"


class SentinelSTACHandler:
    """Search Sentinel-2 L2A on Planetary Computer and clip RGB/NIR chips."""

    def __init__(self, params, gdf):
        self.params = params
        self.gdf = gdf
        self.output_dir = OUTPUT_DIR

    def _aoi(self, row):
        scale = padding_scale(self.params["padding"])
        return affinity.scale(row["geometry"], xfact=scale, yfact=scale)

    def _search_items(self, aoi, start: str, end: str, limit: int = 12):
        import planetary_computer
        from pystac_client import Client

        catalog = Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)
        search = catalog.search(
            collections=[COLLECTION],
            intersects=aoi,
            datetime=f"{start}/{end}",
            query={"eo:cloud_cover": {"lt": 80}},
            sortby=[{"field": "properties.datetime", "direction": "asc"}],
            max_items=limit,
        )
        return list(search.items())

    def _pick_items(self, aoi, start: str, end: str):
        if self.params["num-images"] == -1:
            return self._search_items(aoi, start, end, limit=200)
        windows = sample_date_windows(
            date.fromisoformat(start),
            date.fromisoformat(end),
            self.params["num-images"],
            day_padding=6,
        )
        picked = []
        seen = set()
        for left, right in windows:
            items = self._search_items(aoi, left.isoformat(), right.isoformat(), limit=5)
            for item in items:
                if item.id not in seen:
                    picked.append(item)
                    seen.add(item.id)
                    break
        return picked

    def _read_window(self, href: str, aoi, band_indexes=None):
        import rasterio
        from rasterio.mask import mask

        with rasterio.open(href) as src:
            data, _ = mask(src, [aoi], crop=True, filled=True)
        if band_indexes is None:
            return data
        return data[list(band_indexes), ...]

    def _save_rgb(self, item, aoi, dest: Path):
        visual = item.assets.get("visual")
        if visual is not None:
            array = self._read_window(visual.href, aoi)
            rgb = np.moveaxis(array[:3], 0, -1)
        else:
            bands = []
            for name in ("B04", "B03", "B02"):
                asset = item.assets.get(name)
                if asset is None:
                    return
                bands.append(self._read_window(asset.href, aoi)[0])
            rgb = np.stack(bands, axis=-1)
            if rgb.max() > 255:
                rgb = np.clip(rgb / 20.0, 0, 255)
        Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save(dest)

    def _save_nir(self, item, aoi, dest: Path):
        asset = item.assets.get("B08") or item.assets.get("nir")
        if asset is None:
            return
        array = np.squeeze(self._read_window(asset.href, aoi))
        if array.max() <= 1.5:
            scaled = array * 255.0
        elif array.max() > 255:
            scaled = array / 20.0
        else:
            scaled = array
        Image.fromarray(np.clip(scaled, 0, 255).astype(np.uint8)).save(dest)

    def get_all_imagery(self):
        from shapely.geometry import mapping

        for _, row in self.gdf.iterrows():
            if date.fromisoformat(row["start"]) < SENTINEL_CUTOFF:
                if self.params["verbose"]:
                    print(f"Start date for {row['chain_id']} is before 2015-06-23. Skipped!")
                continue
            aoi = self._aoi(row)
            if self.params["verbose"]:
                print(f"STAC search for {row['chain_id']}")
            try:
                items = self._pick_items(aoi, row["start"], row["end"])
            except Exception as exc:
                print(f"STAC search failed for {row['chain_id']}: {exc}")
                continue
            aoi_geojson = mapping(aoi)
            chain_id = row["chain_id"]
            for item in items:
                label = (item.properties.get("datetime") or item.id)[:10]
                if self.params["rgb"]:
                    path = self.output_dir / f"{chain_id}/images/sentinel/rgb/{label}.png"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        self._save_rgb(item, aoi_geojson, path)
                    except Exception as exc:
                        print(f"FAILED TO GET RGB IMAGE {label}: {exc}")
                if self.params["nir"]:
                    path = self.output_dir / f"{chain_id}/images/sentinel/nir/{label}.png"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        self._save_nir(item, aoi_geojson, path)
                    except Exception as exc:
                        print(f"FAILED TO GET NIR IMAGE {label}: {exc}")
