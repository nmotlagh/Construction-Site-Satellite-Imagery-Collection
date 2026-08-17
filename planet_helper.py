"""Planet Data API search + Orders API v2 client (PSScene)."""

from __future__ import annotations

import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely import affinity, geometry
from skimage import io

from dates import padding_scale, sample_date_windows
from workspace import OUTPUT_DIR

output_dir = OUTPUT_DIR

PLANET_CUTOFF = date(2017, 2, 19)
ITEM_TYPE = "PSScene"
DATE_IN_NAME = re.compile(r"(?:19|20)\d{6}")
FINAL_ORDER_STATES = frozenset({"success", "failed", "partial", "cancelled"})
RGB_FALLBACK = "analytic_udm2"
NIR_FALLBACK = "analytic_sr_udm2"


def _as_acquired_datetime(value: date | datetime | str) -> datetime:
    """Planet ``date_range_filter`` requires ``datetime`` (not ISO strings)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    parsed = date.fromisoformat(str(value).strip()[:10])
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def acquired_date_from_filename(path: Path) -> str | None:
    """Return ``YYYY-MM-DD`` from the PSScene ``YYYYMMDD_...`` filename prefix."""
    match = DATE_IN_NAME.search(path.name)
    if not match:
        return None
    raw = match.group(0)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def classify_planet_file(path: Path) -> str | None:
    name = path.name.lower()
    if path.suffix.lower() not in {".tif", ".tiff"}:
        return None
    if "udm" in name:
        return "udm"
    if "visual" in name:
        return "rgb"
    if "analytic" in name:
        return "nir"
    return None


def reflectance_to_uint8(arr: np.ndarray) -> np.ndarray:
    """Scale Planet/Sentinel reflectance to displayable uint8.

    Handles uint16 (0-10000) analytic, float 0-1 reflectance, and already-uint8
    visual chips. The ICCVW code cast float 0-1 arrays with ``astype(np.uint8)``,
    which produced black PNGs.
    """
    pixels = np.asarray(arr)
    if pixels.size == 0:
        return pixels.astype(np.uint8, copy=False)
    if pixels.dtype == np.uint8:
        return pixels
    values = pixels.astype(np.float64, copy=False)
    peak = np.nanmax(values)
    if np.issubdtype(pixels.dtype, np.floating) or peak <= 1.5:
        values = values * 255.0
    elif peak > 255:
        values = values / 10000.0 * 255.0
    return np.clip(values, 0, 255).astype(np.uint8)


def _bgr_to_rgb(img: np.ndarray) -> np.ndarray:
    """Planet analytic 4-band order is B, G, R, NIR."""
    return np.stack([img[:, :, 2], img[:, :, 1], img[:, :, 0]], axis=-1)


def _planet_client(api_key: str | None):
    """Build a sync Planet SDK v3 client.

    ``Planet(session=Session(auth=Auth.from_key(key)))`` when a key is present;
    otherwise ``Planet()`` so ``planet auth login`` / ``PL_AUTH_*`` sessions work.
    Verified against planet 3.6.0: ``Planet(session=None, base_url=None)``.
    """
    from planet import Auth, Planet, Session

    if api_key:
        session = Session(auth=Auth.from_key(api_key))
        return Planet(session=session)
    return Planet()


class PlanetHandlerV2:
    """Search PSScene items and order clipped RGB/NIR bundles for each site."""

    def __init__(self, api_key, image_parameters, gdf):
        self.gdf = gdf
        self.params = image_parameters
        self.item_types = [ITEM_TYPE]
        self.day_padding = 5
        self.client = _planet_client(api_key)

        self.bundle = []
        if self.params["rgb"]:
            self.bundle.append("visual")
        if self.params["nir"]:
            self.bundle.append("analytic_udm2")
        if not self.bundle:
            raise ValueError("Select --rgb and/or --nir before ordering Planet imagery")

    def get_dates(self, row):
        start_date = date.fromisoformat(row["start"])
        end_date = date.fromisoformat(row["end"])
        return sample_date_windows(
            start_date, end_date, self.params["num-images"], self.day_padding
        )

    def generate_geoson_geometry(self, row):
        scale = padding_scale(self.params["padding"])
        boxes = affinity.scale(row["geometry"], xfact=scale, yfact=scale)
        return gpd.GeoSeries([boxes], crs="EPSG:4326").__geo_interface__["features"][0]["geometry"]

    def get_filter(self, row, left_date, right_date):
        from planet import data_filter

        geo_json_geometry = self.generate_geoson_geometry(row)
        return data_filter.and_filter(
            [
                data_filter.geometry_filter(geo_json_geometry),
                data_filter.date_range_filter(
                    "acquired",
                    gte=_as_acquired_datetime(left_date),
                    lte=_as_acquired_datetime(right_date),
                ),
                data_filter.permission_filter(),
            ]
        )

    def update_item_ids(self, item_ids, and_filter, row, full_window=False):
        from planet.exceptions import APIError, ClientError

        site_geo = row["geometry"]
        covering = {}
        intersecting = []
        try:
            results = self.client.data.search(
                self.item_types,
                search_filter=and_filter,
                sort="acquired asc",
                limit=0 if full_window else 250,
            )
        except (APIError, ClientError) as exc:
            print(f"\tPlanet search failed: {exc}")
            return

        for item in results:
            acquired_date = item["properties"]["acquired"].split("T")[0]
            item_geo = geometry.shape(item["geometry"])
            if item_geo.contains(site_geo) or item_geo.covers(site_geo):
                covering.setdefault(acquired_date, item["id"])
                if not full_window:
                    item_ids.update(covering)
                    return
            elif item_geo.intersects(site_geo):
                overlap = item_geo.intersection(site_geo).area
                intersecting.append((overlap, acquired_date, item["id"]))

        if covering:
            item_ids.update(covering)
            return
        if intersecting:
            intersecting.sort(reverse=True)
            _, acquired_date, item_id = intersecting[0]
            item_ids.setdefault(acquired_date, item_id)

    def create_order(self, row):
        from planet import order_request
        from planet.exceptions import APIError, ClientError

        if date.fromisoformat(row["start"]) < PLANET_CUTOFF:
            if self.params["verbose"]:
                print("\tStart date before 2017-02-19! Skipped!")
            return None

        date_list = self.get_dates(row)
        item_ids = {}
        geo_json_geometry = self.generate_geoson_geometry(row)
        for left, right in date_list:
            and_filter = self.get_filter(row, left, right)
            self.update_item_ids(item_ids, and_filter, row, full_window=(len(date_list) == 1))

        available_items = list(item_ids.values())
        if not available_items:
            if self.params["verbose"]:
                print("\tCould not submit order. No available items!")
            return None

        sent_orders = []
        n = max(1, 500 // len(self.bundle))
        chunked_items = [available_items[i * n : (i + 1) * n] for i in range((len(available_items) + n - 1) // n)]
        # Clip in the item CRS using a WGS84 AOI, then reproject the chip to EPSG:4326.
        tools = [
            order_request.clip_tool(geo_json_geometry),
            order_request.reproject_tool(projection="EPSG:4326", kernel="near"),
        ]
        notifications = order_request.notifications(email=True) if self.params["email"] else None

        for chunk in chunked_items:
            products = []
            for bundle in self.bundle:
                fallback = NIR_FALLBACK if bundle == "analytic_udm2" else RGB_FALLBACK
                products.append(
                    order_request.product(
                        item_ids=chunk,
                        product_bundle=bundle,
                        item_type=ITEM_TYPE,
                        fallback_bundle=fallback,
                    )
                )
            request = order_request.build_request(
                name=str(row["chain_id"])[:100],
                products=products,
                tools=tools,
                notifications=notifications,
                order_type="partial",
            )
            try:
                sent_order = self.client.orders.create_order(request)
                sent_orders.append([sent_order, row["chain_id"], item_ids])
            except (APIError, ClientError) as exc:
                print(f"\tCould not get order {row['chain_id']}. Exception: {exc}")
        return sent_orders or None

    def construct_order_list(self):
        from planet.exceptions import APIError, ClientError

        order_list = []
        with open("order_log.txt", encoding="utf-8") as handle:
            for line in handle:
                order_id, chain_id, *_ = line.strip().split(",")
                try:
                    order = self.client.orders.get_order(order_id)
                except (APIError, ClientError) as exc:
                    print(f"\tCould not load order {order_id}: {exc}")
                    continue
                order_list.append([order, chain_id])
        return order_list

    def download_orders(self):
        from planet.exceptions import APIError, ClientError

        if not Path("order_log.txt").is_file():
            print("No order_log.txt found. Place orders first without --download.")
            return

        order_list = self.construct_order_list()
        completed_order_indices = []
        try:
            for order_info_index, order_info in enumerate(order_list):
                planet_path = output_dir / f"{order_info[1]}/images/planet"
                planet_path.mkdir(parents=True, exist_ok=True)
                order_id = order_info[0]["id"]
                state = order_info[0].get("state")
                if state not in FINAL_ORDER_STATES:
                    if self.params["verbose"] and state != "running":
                        print(f"\tOrder {order_id} is {state}")
                    try:
                        order_info[0] = self.client.orders.get_order(order_id)
                    except (APIError, ClientError) as exc:
                        print(f"\tError getting order. Order will remain in order_log. Exception: {exc}")
                    continue

                if self.params["verbose"] and state in {"failed", "partial", "cancelled"}:
                    print(f"\tOrder {order_id} {state} for {order_info[1]}.")
                accept_partial = self.params.get("accept-partial", True)
                can_download = state == "success" or (state == "partial" and accept_partial)
                if can_download:
                    write_path = planet_path / "temp"
                    write_path.mkdir(parents=True, exist_ok=True)
                    try:
                        # directory= must already exist; SDK raises ClientError if state is not final.
                        self.client.orders.download_order(order_id, directory=write_path, overwrite=True)
                    except (APIError, ClientError) as exc:
                        print(f"\tDownload failed for {order_id}: {exc}")
                        continue
                    for file in write_path.rglob("*"):
                        if file.is_file():
                            self.rewrite_tif(file, planet_path)
                    shutil.rmtree(write_path, ignore_errors=True)
                    if self.params["verbose"]:
                        print(f"\tBundle {order_id} downloaded for {order_info[1]}!")

                if self.params["num-images"] == -1 and not any(
                    order_info[1] == chain_id and o_index > order_info_index
                    for o_index, (_, chain_id) in enumerate(order_list)
                ):
                    self._write_empty_placeholders(order_info[1], planet_path)
                completed_order_indices.append(order_info_index)
        except BaseException as exc:
            print(f"Something went wrong... {exc}. Please retry.")
        finally:
            new_order_list, completed_orders = [], []
            for index, order in enumerate(order_list):
                if index in completed_order_indices:
                    completed_orders.append(order)
                else:
                    new_order_list.append(order)
            if new_order_list:
                self.save_order_list(new_order_list)
                print(f"There are still {len(new_order_list)} orders!")
            else:
                print("No remaining orders!")
                if Path("order_log.txt").is_file():
                    os.remove("order_log.txt")
            if completed_orders:
                self.update_complete_log(completed_orders)

    def _write_empty_placeholders(self, chain_id, planet_path):
        bands = [name for name, flag in (("rgb", self.params["rgb"]), ("nir", self.params["nir"])) if flag]
        row = self.gdf.loc[self.gdf["chain_id"] == chain_id].iloc[0]
        start_date = date.fromisoformat(row["start"])
        end_date = date.fromisoformat(row["end"])
        empty_dates = [
            str(start_date + timedelta(days=offset))
            for offset in range((end_date - start_date).days + 1)
        ]
        for band in bands:
            img_dir = planet_path / band
            try:
                any_img_path = next(img_dir.glob("*.png"))
            except StopIteration:
                continue
            blank_img = np.zeros_like(io.imread(str(any_img_path)))
            for empty_date in empty_dates:
                if not (img_dir / f"{empty_date}.png").is_file():
                    io.imsave(str(img_dir / f"{empty_date}_empty.png"), blank_img, check_contrast=False)

    @staticmethod
    def rewrite_tif(current_file, new_dir):
        kind = classify_planet_file(current_file)
        if kind is None:
            return
        date_name = acquired_date_from_filename(current_file)
        if date_name is None:
            print(f"{current_file} has no YYYYMMDD in the filename; skipped")
            return
        if kind == "udm":
            dest = new_dir / "nir"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy(str(current_file), str(dest / f"{date_name}_udm.tif"))
            return
        try:
            img = io.imread(str(current_file))
        except Exception:
            print(f"{current_file} failed to read as TIFF")
            return

        if kind == "rgb":
            dest = new_dir / "rgb"
            dest.mkdir(parents=True, exist_ok=True)
            if img.ndim == 3 and img.shape[-1] >= 3:
                rgb = reflectance_to_uint8(img[:, :, :3])
                io.imsave(str(dest / f"{date_name}.png"), rgb, check_contrast=False)
                if img.shape[-1] >= 4:
                    io.imsave(
                        str(dest / f"{date_name}_mask.png"),
                        np.array(img[:, :, 3], dtype=np.uint8),
                        check_contrast=False,
                    )
            return

        dest = new_dir / "nir"
        dest.mkdir(parents=True, exist_ok=True)
        if img.ndim == 2:
            nir = np.array(img)
        elif img.shape[-1] >= 8:
            nir = np.array(img[:, :, 7])
        else:
            nir = np.array(img[:, :, min(3, img.shape[-1] - 1)])
        io.imsave(str(dest / f"{date_name}.png"), reflectance_to_uint8(nir), check_contrast=False)

        # visual fallback is analytic_udm2: emit RGB from B,G,R when --rgb created rgb/.
        rgb_dir = new_dir / "rgb"
        rgb_path = rgb_dir / f"{date_name}.png"
        if img.ndim == 3 and img.shape[-1] >= 3 and rgb_dir.is_dir() and not rgb_path.is_file():
            io.imsave(str(rgb_path), reflectance_to_uint8(_bgr_to_rgb(img)), check_contrast=False)

    @staticmethod
    def write_to_log(message):
        log = Path("order_log.txt")
        if not log.is_file():
            log.touch()
        with log.open("a", encoding="utf-8") as handle:
            handle.write(message)

    def bulk_order(self):
        orders_list = []
        for _, row in self.gdf.iterrows():
            print(f"Orders processing for {row['chain_id']}")
            created_orders = self.create_order(row)
            if created_orders:
                orders_list += created_orders
                for order in created_orders:
                    self.write_to_log(f"{order[0]['id']},{order[1]}\n")
                if self.params["verbose"]:
                    print(f"\tOrders created for {row['chain_id']}")
        if orders_list:
            print(f"Last order in list will be {orders_list[-1][0]['id']}")
            print("Re-run with --download once Planet emails you or after a few minutes.")

    @staticmethod
    def save_order_list(order_list):
        with Path("order_log.txt").open("w", encoding="utf-8") as handle:
            for order_info in order_list:
                handle.write(f"{order_info[0]['id']},{order_info[1]}\n")

    @staticmethod
    def update_complete_log(order_list):
        with Path("order_log_complete.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(timezone.utc).isoformat()}\n")
            for order_info in order_list:
                handle.write(f"{order_info[0]['id']},{order_info[0].get('state')},{order_info[1]}\n")
            handle.write(f"{'*' * 15}\n")
