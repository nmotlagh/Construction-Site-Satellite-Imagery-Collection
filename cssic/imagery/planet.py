"""Planet PSScene imagery through the Data API and Orders API v2.

Planet is not a fetch-a-pixel-window API: chips are *ordered*, clipped
server-side, and collected once the order reaches a final state. The flow the
CLI exposes is therefore two steps::

    cssic gather -s p -C -N        # search + create clipped orders
    cssic gather -s p -C -N --download   # collect the finished orders

:meth:`PlanetSource.find_scenes` implements the search half of the
:class:`~cssic.imagery.base.ImageSource` protocol; :meth:`PlanetSource.fetch`
cannot be honoured for Planet (there is no synchronous pixel endpoint) and says
so. :meth:`PlanetSource.bulk_order` and :meth:`PlanetSource.download_orders`
are the two steps above.

Ported from v2 ``planet_helper.PlanetHandlerV2``. The ``planet`` SDK (v3, sync
``Planet`` facade) is imported lazily inside the methods that need it, as are
``rasterio``/``PIL`` and :mod:`cssic.imagery.chips`, so ``import cssic`` works
without any of them.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cssic.config import Credentials, GatherConfig
from cssic.imagery.base import Band, Scene

#: PlanetScope imagery does not exist before this date.
PLANET_CUTOFF = date(2017, 2, 19)
#: The only item type this package orders (v2 also touched the retired
#: ``PSOrthoTile``/``PSScene4Band`` types).
ITEM_TYPE = "PSScene"
SOURCE_NAME = "planet"

#: PSScene asset filenames start ``YYYYMMDD_HHMMSS_...``; the second underscore
#: field is a satellite fragment, not the date, so match the date by shape.
DATE_IN_NAME = re.compile(r"(?:19|20)\d{6}")

#: Order states after which nothing more will change.
FINAL_ORDER_STATES = frozenset({"success", "failed", "partial", "cancelled"})
#: States whose assets are worth downloading.
DOWNLOADABLE_STATES = frozenset({"success", "partial"})

#: Product bundle per requested band, and the fallback bundle Planet uses when
#: an item does not publish the preferred one.
BUNDLES: dict[str, str] = {"rgb": "visual", "nir": "analytic_udm2"}
FALLBACK_BUNDLES: dict[str, str] = {"visual": "analytic_udm2", "analytic_udm2": "analytic_sr_udm2"}

#: Planet caps an order at 500 items; each bundle counts as one product.
MAX_ITEMS_PER_ORDER = 500
#: Data API search page size for a single sampled date window.
SEARCH_LIMIT = 250

ORDER_LOG_NAME = "order_log.txt"
COMPLETE_LOG_NAME = "order_log_complete.txt"


# --------------------------------------------------------------------------
# filename / value helpers (pure, unit-testable)
# --------------------------------------------------------------------------
def as_acquired_datetime(value: date | datetime | str) -> datetime:
    """Coerce a date to the tz-aware ``datetime`` ``data_filter`` requires."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    parsed = date.fromisoformat(str(value).strip()[:10])
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def acquired_end_of_day(value: date | datetime | str) -> datetime:
    """The upper ``acquired`` bound that keeps the whole last day of a window.

    ``date_range_filter`` compares instants, so ``lte=<day>T00:00:00Z`` drops
    every acquisition of that day -- and the last day of a sampled window is
    often the only one with an acquisition.
    """
    moment = as_acquired_datetime(value)
    if (moment.hour, moment.minute, moment.second, moment.microsecond) != (0, 0, 0, 0):
        return moment
    return moment + timedelta(days=1) - timedelta(microseconds=1)


def acquired_date_from_filename(path: str | Path) -> str | None:
    """Return ``YYYY-MM-DD`` from a PSScene ``YYYYMMDD_...`` filename, or None."""
    match = DATE_IN_NAME.search(Path(path).name)
    if not match:
        return None
    raw = match.group(0)
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def classify_planet_file(path: str | Path) -> str | None:
    """Classify a delivered asset as ``"rgb"``, ``"nir"``, ``"udm"`` or None."""
    path = Path(path)
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


def analytic_rgb(image: Any) -> Any:
    """Extract true-colour RGB from a Planet analytic chip (HxWxC).

    4-band PSScene is B, G, R, NIR; 8-band SuperDove is coastal, blue, green i,
    green, yellow, red, red-edge, NIR.
    """
    import numpy as np

    array = np.asarray(image)
    bands = array.shape[-1]
    if bands >= 8:
        red, green, blue = 5, 3, 1
    elif bands >= 3:
        red, green, blue = 2, 1, 0
    else:
        raise ValueError(f"cannot build rgb from a {bands}-band analytic chip")
    return np.stack([array[:, :, red], array[:, :, green], array[:, :, blue]], axis=-1)


def analytic_nir(image: Any) -> Any:
    """Extract the NIR plane from a Planet analytic chip (HxWxC or HxW)."""
    import numpy as np

    array = np.asarray(image)
    if array.ndim == 2:
        return array
    bands = array.shape[-1]
    if bands >= 8:
        return array[:, :, 7]
    return array[:, :, min(3, bands - 1)]


# --------------------------------------------------------------------------
# order log
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class OrderRecord:
    """One created order: the Planet order id and the chain it belongs to."""

    order_id: str
    chain_id: str
    state: str | None = None

    @property
    def log_line(self) -> str:
        return f"{self.order_id},{self.chain_id}\n"

    @classmethod
    def parse(cls, line: str) -> OrderRecord | None:
        parts = [part.strip() for part in line.strip().split(",")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        return cls(order_id=parts[0], chain_id=parts[1])


def order_log_path(workspace: Any | None = None) -> Path:
    """``temp/order_log.txt`` inside the workspace (v2 wrote it to the cwd)."""
    return _temp_dir(workspace) / ORDER_LOG_NAME


def complete_log_path(workspace: Any | None = None) -> Path:
    """``temp/order_log_complete.txt``: an audit trail of collected orders."""
    return _temp_dir(workspace) / COMPLETE_LOG_NAME


def _temp_dir(workspace: Any | None) -> Path:
    if workspace is None:
        return Path("temp")
    return Path(workspace.temp_dir)


def read_order_log(workspace: Any | None = None) -> list[OrderRecord]:
    """Parse ``temp/order_log.txt``; an absent log is an empty list."""
    path = order_log_path(workspace)
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = OrderRecord.parse(line)
        if record is not None:
            records.append(record)
    return records


def write_order_log(records: list[OrderRecord], workspace: Any | None = None) -> Path | None:
    """Rewrite ``temp/order_log.txt``; deletes it when nothing is outstanding."""
    path = order_log_path(workspace)
    if not records:
        if path.is_file():
            path.unlink()
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(record.log_line for record in records), encoding="utf-8")
    return path


def append_order_log(record: OrderRecord, workspace: Any | None = None) -> Path:
    path = order_log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.log_line)
    return path


def append_complete_log(records: list[OrderRecord], workspace: Any | None = None) -> Path | None:
    if not records:
        return None
    path = complete_log_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now(timezone.utc).isoformat()}\n")
        for record in records:
            handle.write(f"{record.order_id},{record.state},{record.chain_id}\n")
        handle.write(f"{'*' * 15}\n")
    return path


def _planet_client(credentials: Credentials | None) -> Any:
    """Build the sync SDK client (planet 3.6: ``Planet(session=None, base_url=None)``).

    With an API key we build an authenticated session; without one we fall back
    to ``Planet()`` so ``planet auth login`` / ``PL_API_KEY`` sessions work.
    """
    from planet import Auth, Planet, Session

    api_key = credentials.planet_api_key if credentials is not None else None
    if api_key:
        return Planet(session=Session(auth=Auth.from_key(api_key)))
    return Planet()


# --------------------------------------------------------------------------
# the backend
# --------------------------------------------------------------------------
class PlanetSource:
    """Planet PSScene imagery collected through the Orders API.

    :meth:`bulk_order` places orders; :meth:`download_orders` collects chips.
    :meth:`fetch` is unsupported by this backend; chips come from completed orders.

    ``client`` (anything with ``.data.search`` / ``.orders.*``, normally a
    ``planet.Planet``) is injected so tests never place real orders.
    """

    def __init__(
        self,
        credentials: Credentials | None = None,
        cfg: GatherConfig | None = None,
        client: Any | None = None,
        item_type: str = ITEM_TYPE,
    ) -> None:
        self.credentials = credentials
        self.cfg = cfg if cfg is not None else GatherConfig(source="planet", rgb=True)
        self.item_type = item_type
        self._client = client
        self._entitlement_checked = False

    # -- plumbing ---------------------------------------------------------
    @property
    def client(self) -> Any:
        """The injected client, or a lazily built SDK client."""
        if self._client is None:
            self._client = _planet_client(self.credentials)
        return self._client

    @property
    def bundles(self) -> list[str]:
        """The product bundles for the requested bands, ``rgb`` first."""
        bundles = [BUNDLES[band] for band in self.cfg.bands if band in BUNDLES]
        if not bundles:
            raise ValueError("select --rgb and/or --nir before ordering Planet imagery")
        return bundles

    def _say(self, message: str) -> None:
        if self.cfg.verbose:
            print(message)

    # -- ImageSource ------------------------------------------------------
    def find_scenes(
        self,
        aoi: Any,
        start: date,
        end: date,
        all_dates: bool = False,
    ) -> list[Scene]:
        """PSScene acquisitions usable for ``aoi`` between ``start`` and ``end``.

        Scenes that *cover* the whole AOI win: at most one per acquisition date,
        earliest first. ``all_dates=False`` (the default) stops at the first
        such scene, which is what one sampled date window needs;
        ``all_dates=True`` (``-n -1``) keeps one scene for every date in the
        window. When nothing covers the AOI, the single largest-overlap
        intersecting scene is returned so the site is not lost entirely.
        """
        from planet.exceptions import APIError, ClientError
        from shapely.geometry import shape

        search_filter = self.search_filter(aoi, start, end)
        try:
            results = self.client.data.search(
                [self.item_type],
                search_filter=search_filter,
                sort="acquired asc",
                limit=0 if all_dates else SEARCH_LIMIT,
            )
        except (APIError, ClientError) as exc:
            print(f"\tPlanet search failed: {exc}")
            return []

        covering: dict[str, Scene] = {}
        intersecting: list[tuple[float, str, Scene]] = []
        for item in results:
            scene = _scene_from_item(item)
            if scene is None:
                continue
            item_geom = shape(item["geometry"])
            if item_geom.covers(aoi):
                covering.setdefault(scene.acquired.isoformat(), scene)
                if not all_dates:
                    break
            elif item_geom.intersects(aoi):
                overlap = item_geom.intersection(aoi).area
                intersecting.append((overlap, scene.id, scene))

        if covering:
            return [covering[key] for key in sorted(covering)]
        if intersecting:
            intersecting.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
            return [intersecting[0][2]]
        return []

    def fetch(self, scene: Scene, aoi: Any, band: Band) -> Any:
        """Not available: Planet delivers pixels through asynchronous orders."""
        raise NotImplementedError(
            "Planet has no synchronous pixel endpoint; use bulk_order() to place "
            "clipped orders and download_orders() (cssic gather -s p --download) "
            "to collect the chips."
        )

    # -- search -----------------------------------------------------------
    def _explain_empty_search(self, aoi: Any, start: date, end: date) -> None:
        """Once per run, tell an unentitled account why every search is empty.

        A Planet account without a plan sees the archive but may download none
        of it: the same search without ``permission_filter`` returns items
        whose ``_permissions`` list is empty. From the outside that looks
        exactly like "no imagery here", so the first empty site re-runs the
        search unfiltered and names the real cause. Always printed, because a
        run that orders nothing for that reason is not a quiet success.
        """
        if self._entitlement_checked:
            return
        self._entitlement_checked = True
        from planet import data_filter
        from planet.exceptions import APIError, ClientError
        from shapely.geometry import mapping

        unfiltered = data_filter.and_filter(
            [
                data_filter.geometry_filter(dict(mapping(aoi))),
                data_filter.date_range_filter(
                    "acquired",
                    gte=as_acquired_datetime(start),
                    lte=acquired_end_of_day(end),
                ),
            ]
        )
        try:
            items = list(
                self.client.data.search([self.item_type], search_filter=unfiltered, limit=1)
            )
        except (APIError, ClientError):
            return
        if items and not items[0].get("_permissions"):
            print(
                f"WARNING: Planet has {self.item_type} imagery over this site but your "
                "account is not permitted to download any of it (no plan or quota on the "
                "archive). Every site will order nothing until the account has "
                "PlanetScope access: https://www.planet.com/account/"
            )

    def search_filter(self, aoi: Any, start: date, end: date) -> dict[str, Any]:
        """The Data API ``and`` filter for one AOI and date window."""
        from planet import data_filter
        from shapely.geometry import mapping

        filters = [
            data_filter.geometry_filter(dict(mapping(aoi))),
            data_filter.date_range_filter(
                "acquired",
                gte=as_acquired_datetime(start),
                lte=acquired_end_of_day(end),
            ),
            data_filter.permission_filter(),
        ]
        if self.cfg.max_cloud_cover is not None:
            # ``--max-cloud`` is a percentage; PSScene ``cloud_cover`` is a 0-1
            # fraction. Without this the flag was silently stac/sentinel-only.
            filters.append(
                data_filter.range_filter("cloud_cover", lte=self.cfg.max_cloud_cover / 100.0)
            )
        return data_filter.and_filter(filters)

    def order_aoi(self, geometry: Any) -> Any:
        """The padded bounding box ordered and clipped for a site."""
        from cssic.imagery.chips import padded_box

        return padded_box(geometry, self.cfg.padding)

    # -- ordering ---------------------------------------------------------
    def create_order(self, row: Any, workspace: Any | None = None) -> list[OrderRecord]:
        """Create the clipped order(s) for one construction chain."""
        from planet import order_request
        from planet.exceptions import APIError, ClientError
        from shapely.geometry import mapping

        from cssic.dates import sample_date_windows

        chain_id = str(row["chain_id"])
        start = date.fromisoformat(str(row["start"]))
        end = date.fromisoformat(str(row["end"]))
        if start < PLANET_CUTOFF:
            self._say(f"\t{chain_id} starts before {PLANET_CUTOFF} (no PlanetScope); skipped")
            return []

        aoi = self.order_aoi(row["geometry"])
        all_dates = self.cfg.num_images == -1
        item_ids: dict[str, str] = {}
        for window_start, window_end in sample_date_windows(
            start, end, self.cfg.num_images, self.cfg.day_padding
        ):
            for scene in self.find_scenes(aoi, window_start, window_end, all_dates=all_dates):
                item_ids.setdefault(scene.acquired.isoformat(), scene.id)

        if not item_ids:
            self._say(f"\tNo available PSScene items for {chain_id}; nothing ordered")
            self._explain_empty_search(aoi, start, end)
            return []

        bundles = self.bundles
        tools = [
            # Clip in the item CRS against a WGS84 AOI, then reproject the chip.
            order_request.clip_tool(dict(mapping(aoi))),
            order_request.reproject_tool(projection="EPSG:4326", kernel="near"),
        ]
        notifications = order_request.notifications(email=True) if self.cfg.email else None

        records: list[OrderRecord] = []
        for chunk in _chunk(sorted(item_ids.values()), max(1, MAX_ITEMS_PER_ORDER // len(bundles))):
            products = [
                order_request.product(
                    item_ids=chunk,
                    product_bundle=bundle,
                    item_type=self.item_type,
                    fallback_bundle=FALLBACK_BUNDLES.get(bundle),
                )
                for bundle in bundles
            ]
            request = order_request.build_request(
                name=chain_id[:100],
                products=products,
                tools=tools,
                notifications=notifications,
                order_type="partial",
            )
            try:
                order = self.client.orders.create_order(request)
            except (APIError, ClientError) as exc:
                print(f"\tCould not create an order for {chain_id}: {exc}")
                continue
            record = OrderRecord(order_id=str(order["id"]), chain_id=chain_id)
            append_order_log(record, workspace)
            records.append(record)
        return records

    def bulk_order(self, gdf: Any, workspace: Any | None = None) -> list[OrderRecord]:
        """Create clipped orders for every construction chain in ``gdf``."""
        records: list[OrderRecord] = []
        for _, row in gdf.iterrows():
            self._say(f"Ordering imagery for {row['chain_id']}")
            created = self.create_order(row, workspace)
            if created:
                records.extend(created)
                self._say(f"\t{len(created)} order(s) created for {row['chain_id']}")
        if records:
            print(f"Created {len(records)} Planet order(s); last is {records[-1].order_id}.")
            print("Re-run with --download once Planet reports the orders are ready.")
        else:
            print("No Planet orders were created.")
        return records

    # -- downloading ------------------------------------------------------
    def download_orders(self, workspace: Any | None = None) -> list[OrderRecord]:
        """Collect finished orders from the log and write their chips.

        Orders that are still running stay in ``temp/order_log.txt`` so the
        command can be re-run; collected ones move to
        ``temp/order_log_complete.txt``.
        """
        from planet.exceptions import APIError, ClientError

        records = read_order_log(workspace)
        if not records:
            print(f"No {ORDER_LOG_NAME} found. Place orders first (without --download).")
            return []

        pending: list[OrderRecord] = []
        completed: list[OrderRecord] = []
        gdf: Any = None
        for index, record in enumerate(records):
            try:
                order = self.client.orders.get_order(record.order_id)
            except (APIError, ClientError) as exc:
                print(f"\tCould not load order {record.order_id}: {exc}; kept in the log")
                pending.append(record)
                continue

            state = order.get("state")
            if state not in FINAL_ORDER_STATES:
                self._say(f"\tOrder {record.order_id} is {state}; kept in the log")
                pending.append(record)
                continue
            if state != "success":
                print(f"\tOrder {record.order_id} for {record.chain_id} is {state}.")

            planet_dir = _images_dir(workspace, record.chain_id)
            if state in DOWNLOADABLE_STATES and not self._collect_order(record, planet_dir):
                pending.append(record)
                continue

            if self.cfg.num_images == -1 and not any(
                other.chain_id == record.chain_id for other in records[index + 1 :]
            ):
                if gdf is None:
                    gdf = _load_collection(workspace)
                self.write_empty_placeholders(record.chain_id, planet_dir, gdf)
            completed.append(OrderRecord(record.order_id, record.chain_id, state))

        write_order_log(pending, workspace)
        append_complete_log(completed, workspace)
        if pending:
            print(f"There are still {len(pending)} order(s) outstanding.")
        else:
            print("No remaining orders!")
        return completed

    def _collect_order(self, record: OrderRecord, planet_dir: Path) -> bool:
        """Download one order's assets into ``planet_dir``; False if it failed.

        "Delivered" is not "collected": a download can succeed and still yield
        no imagery (an unreadable GeoTIFF, a delivery of nothing but UDM masks,
        chips that are pure cloud). Reporting that as a success would retire the
        order from the log and leave the chain silently without images, so the
        order stays pending -- with its staging directory, for inspection --
        until a re-run writes at least one chip.
        """
        from planet.exceptions import APIError, ClientError

        staging = planet_dir / "temp"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            # directory= must already exist; the SDK raises if the state is not final.
            self.client.orders.download_order(
                record.order_id, directory=staging, overwrite=True, progress_bar=False
            )
        except (APIError, ClientError) as exc:
            print(f"\tDownload failed for {record.order_id}: {exc}")
            shutil.rmtree(staging, ignore_errors=True)
            return False
        written: list[Path] = []
        for delivered in sorted(staging.rglob("*")):
            if delivered.is_file():
                written.extend(self.rewrite_tif(delivered, planet_dir))
        if not any(path.suffix.lower() == ".png" for path in written):
            print(
                f"\tOrder {record.order_id} for {record.chain_id} delivered no usable chip; "
                f"kept in the log (delivery left in {staging})"
            )
            return False
        shutil.rmtree(staging, ignore_errors=True)
        self._say(f"\tOrder {record.order_id} collected for {record.chain_id}")
        return True

    # -- writing chips ----------------------------------------------------
    def rewrite_tif(self, source: str | Path, planet_dir: str | Path) -> list[Path]:
        """Turn one delivered GeoTIFF into ``{band}/{YYYY-MM-DD}.png`` chips.

        UDM masks are copied through as ``nir/{date}_udm.tif``. When only the
        analytic bundle was delivered but RGB was requested, the true-colour
        chip is built from the analytic bands.

        A chip with no signal at all -- all nodata, or saturated to one value by
        cloud over the whole clip -- is skipped rather than written, the same
        rule the Sentinel backends apply. The alpha/UDM masks beside a chip are
        exempt: a uniformly 255 mask means every pixel is valid.
        """
        import numpy as np

        from cssic.imagery.chips import save_png

        source = Path(source)
        planet_dir = Path(planet_dir)
        kind = classify_planet_file(source)
        if kind is None:
            return []
        day = acquired_date_from_filename(source)
        if day is None:
            print(f"\t{source.name} has no YYYYMMDD in its filename; skipped")
            return []

        if kind == "udm":
            dest_dir = planet_dir / "nir"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"{day}_udm.tif"
            shutil.copy(str(source), str(dest))
            return [dest]

        image = _read_tif(source)
        if image is None:
            print(f"\t{source.name} could not be read as a GeoTIFF; skipped")
            return []

        written: list[Path] = []
        if kind == "rgb":
            if image.ndim != 3 or image.shape[-1] < 3:
                return []
            if not self._save_chip(image[:, :, :3], planet_dir / "rgb" / f"{day}.png", written):
                return []
            if image.shape[-1] >= 4:
                mask = np.asarray(image[:, :, 3]).astype(np.uint8)
                written.append(save_png(mask, planet_dir / "rgb" / f"{day}_mask.png"))
            return written

        self._save_chip(analytic_nir(image), planet_dir / "nir" / f"{day}.png", written)
        # The visual fallback is analytic_udm2: synthesise RGB when it was asked
        # for and the visual bundle did not arrive.
        rgb_dir = planet_dir / "rgb"
        rgb_path = rgb_dir / f"{day}.png"
        if image.ndim == 3 and image.shape[-1] >= 3 and rgb_dir.is_dir() and not rgb_path.is_file():
            self._save_chip(analytic_rgb(image), rgb_path, written)
        return written

    def _save_chip(self, image: Any, path: Path, written: list[Path]) -> bool:
        """Write one chip unless it is blank; appends to ``written`` when it lands."""
        from cssic.imagery.chips import is_blank, reflectance_to_uint8, save_png

        # is_blank judges what would be written, so scale first: a uint16
        # analytic chip is not constant until it has been clipped to 0-255.
        pixels = reflectance_to_uint8(image)
        if is_blank(pixels):
            print(f"\tSKIPPED blank chip {path.parent.name}/{path.name}")
            return False
        written.append(save_png(pixels, path))
        return True

    def write_empty_placeholders(
        self,
        chain_id: str,
        planet_dir: str | Path,
        gdf: Any = None,
    ) -> list[Path]:
        """Write a blank ``{date}_empty.png`` for every date with no acquisition.

        Only used with ``-n -1``, where the caller asked for every date in the
        construction period and the gaps are meaningful.
        """
        import numpy as np

        from cssic.imagery.chips import save_png

        planet_dir = Path(planet_dir)
        row = _row_for_chain(gdf, chain_id)
        if row is None:
            return []
        start = date.fromisoformat(str(row["start"]))
        end = date.fromisoformat(str(row["end"]))
        days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]

        written: list[Path] = []
        for band in self.cfg.bands:
            band_dir = planet_dir / band
            if not band_dir.is_dir():
                continue
            template = next(iter(sorted(band_dir.glob("*.png"))), None)
            if template is None:
                continue
            blank = np.zeros_like(_read_png(template))
            for day in days:
                if not (band_dir / f"{day.isoformat()}.png").is_file():
                    written.append(save_png(blank, band_dir / f"{day.isoformat()}_empty.png"))
        return written


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _scene_from_item(item: dict[str, Any]) -> Scene | None:
    """Build a :class:`Scene` from a Data API search result."""
    properties = item.get("properties") or {}
    acquired = str(properties.get("acquired") or "")[:10]
    try:
        acquired_date = date.fromisoformat(acquired)
    except ValueError:
        return None
    item_id = item.get("id")
    if not item_id:
        return None
    return Scene(
        id=str(item_id),
        acquired=acquired_date,
        source=SOURCE_NAME,
        href=None,
        cloud_cover=properties.get("cloud_cover"),
        metadata={"item_type": properties.get("item_type", ITEM_TYPE)},
    )


def _images_dir(workspace: Any | None, chain_id: str) -> Path:
    if workspace is not None:
        path = Path(workspace.images_dir(chain_id, SOURCE_NAME))
    else:
        path = Path("output") / str(chain_id) / "images" / SOURCE_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_collection(workspace: Any | None) -> Any:
    if workspace is None:
        return None
    try:
        return workspace.load_collection()
    except (OSError, ValueError):
        return None


def _row_for_chain(gdf: Any, chain_id: str) -> Any:
    if gdf is None:
        return None
    matches = gdf.loc[gdf["chain_id"].astype(str) == str(chain_id)]
    if len(matches.index) == 0:
        return None
    return matches.iloc[0]


def _read_tif(path: Path) -> Any:
    """Read a GeoTIFF as an HxWxC array (or HxW for a single band)."""
    import numpy as np

    try:
        import rasterio
    except ImportError:  # pragma: no cover - rasterio is a declared dependency
        return None
    try:
        with rasterio.open(path) as dataset:
            data = dataset.read()
    except Exception:
        return None
    array = np.asarray(data)
    if array.ndim == 2:
        return array
    if array.shape[0] == 1:
        return array[0]
    return np.moveaxis(array, 0, -1)


def _read_png(path: Path) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as handle:
        return np.asarray(handle)
