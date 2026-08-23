"""Sentinel-2 L2A chips from the Sentinel Hub Process API (CDSE or commercial).

Port of the v2 ``gather_images.SentinelHandler`` (plus ``_sentinel_config`` and
``_sentinel_collection``) onto the :class:`~cssic.imagery.base.ImageSource`
protocol:

* :meth:`SentinelHubSource.find_scenes` asks ``SentinelHubCatalog`` which days
  actually have an acquisition over the AOI, collapsing the several tiles of one
  day into a single :class:`~cssic.imagery.base.Scene`. v2 only did a catalog
  search for ``-n -1`` and otherwise fired blind Process API requests at padded
  date windows; here every request is backed by a known acquisition.
* :meth:`SentinelHubSource.fetch` renders one day through the Process API with
  the true-colour or NIR evalscript and returns a uint8 ``HxWx3`` / ``HxWx1``
  array. Writing the PNG is the caller's job (``chips.save_png``).

``sentinelhub`` is imported lazily so ``import cssic`` works without it. The
catalog and the Process API request factory are injectable, so tests mock at
the client boundary rather than patching module internals.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

from cssic.config import Credentials
from cssic.imagery.base import Band, Scene

#: Sentinel-2 has no imagery before this date (v2 ``gather_images``).
SENTINEL_CUTOFF = date(2015, 6, 23)

#: Copernicus Data Space Ecosystem: the free provider, and the default.
CDSE_BASE = "https://sh.dataspace.copernicus.eu"
CDSE_TOKEN = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
)

#: Sentinel-2 ground resolution, in metres, used to size Process API requests.
RESOLUTION = 10
#: The Process API refuses requests larger than this on a side.
MAX_DIMENSION = 2500

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


def build_config(credentials: Credentials, config: Any | None = None) -> Any:
    """Build an ``SHConfig`` from :class:`~cssic.config.Credentials`.

    Port of v2 ``_sentinel_config``, raising :class:`ValueError` instead of
    calling ``sys.exit``. ``sentinel_provider="cdse"`` points the config at the
    free Copernicus Data Space endpoints; ``"sentinelhub"`` leaves the
    commercial defaults alone.
    """
    if not credentials.has_sentinel_hub:
        raise ValueError(
            "Sentinel Hub credentials missing. Create an OAuth client at "
            "https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings "
            "and set SH_CLIENT_ID / SH_CLIENT_SECRET (see .env.example), or pass "
            "--sh-client-id / --sh-client-secret."
        )
    if config is None:
        from sentinelhub import SHConfig

        config = SHConfig()
    config.sh_client_id = credentials.sh_client_id
    config.sh_client_secret = credentials.sh_client_secret
    if credentials.sentinel_provider == "cdse":
        config.sh_base_url = CDSE_BASE
        config.sh_token_url = CDSE_TOKEN
    return config


def sentinel_collection(config: Any) -> Any:
    """The Sentinel-2 L2A ``DataCollection`` for the endpoint ``config`` points at.

    Port of v2 ``_sentinel_collection``: CDSE serves L2A from its own service
    url, so the stock collection has to be redefined against it.
    """
    from sentinelhub import DataCollection

    if getattr(config, "sh_base_url", None) == CDSE_BASE:
        return DataCollection.SENTINEL2_L2A.define_from("s2l2a_cdse", service_url=CDSE_BASE)
    return DataCollection.SENTINEL2_L2A


def _acquired_on(properties: dict[str, Any]) -> date | None:
    stamp = properties.get("datetime") or properties.get("start_datetime")
    if not stamp:
        return None
    try:
        return date.fromisoformat(str(stamp)[:10])
    except ValueError:
        return None


class SentinelHubSource:
    """An :class:`~cssic.imagery.base.ImageSource` over the Sentinel Hub Process API.

    ``credentials.sentinel_provider`` selects CDSE (free) or the commercial
    Sentinel Hub endpoints. Injection points, all optional and all used by the
    tests: ``config`` (an ``SHConfig``), ``collection`` (a ``DataCollection``),
    ``catalog`` (anything with ``.search(collection, bbox=, time=)``) and
    ``request_factory`` (called with ``bbox``, ``size``, ``time_interval``,
    ``evalscript``, ``mime`` and returning an object with ``.get_data()``).
    """

    source_name = "sentinel"

    def __init__(
        self,
        credentials: Credentials,
        config: Any | None = None,
        *,
        collection: Any | None = None,
        resolution: int = RESOLUTION,
        max_cloud_cover: float | None = None,
        mosaicking_order: str = "leastCC",
        catalog: Any | None = None,
        request_factory: Any | None = None,
    ) -> None:
        self.credentials = credentials
        self.config = build_config(credentials, config)
        self.collection = sentinel_collection(self.config) if collection is None else collection
        self.resolution = int(resolution)
        self.max_cloud_cover = max_cloud_cover
        self.mosaicking_order = mosaicking_order
        self._catalog = catalog
        self._request_factory = request_factory

    # -- clients -----------------------------------------------------------
    @property
    def catalog(self) -> Any:
        """The (lazily constructed) ``SentinelHubCatalog``."""
        if self._catalog is None:
            from sentinelhub import SentinelHubCatalog

            self._catalog = SentinelHubCatalog(config=self.config)
        return self._catalog

    def bbox(self, aoi: Any) -> Any:
        """The AOI's bounding box as a WGS84 ``sentinelhub.BBox``."""
        from sentinelhub import CRS, BBox

        bounds = aoi.bounds if hasattr(aoi, "bounds") else tuple(aoi)
        return BBox(bbox=tuple(bounds), crs=CRS.WGS84)

    def _size(self, bbox: Any) -> tuple[int, int]:
        from sentinelhub import bbox_to_dimensions

        width, height = bbox_to_dimensions(bbox, resolution=self.resolution)
        return (
            min(max(int(width), 1), MAX_DIMENSION),
            min(max(int(height), 1), MAX_DIMENSION),
        )

    def _request(
        self,
        bbox: Any,
        time_interval: tuple[str, str],
        evalscript: str,
        mime: Any,
    ) -> Any:
        size = self._size(bbox)
        if self._request_factory is not None:
            return self._request_factory(
                bbox=bbox,
                size=size,
                time_interval=time_interval,
                evalscript=evalscript,
                mime=mime,
            )
        from sentinelhub import SentinelHubRequest

        other_args = {
            "dataFilter": {
                "maxCloudCoverage": 100 if self.max_cloud_cover is None else self.max_cloud_cover,
                "mosaickingOrder": self.mosaicking_order,
            }
        }
        return SentinelHubRequest(
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

    # -- ImageSource -------------------------------------------------------
    def find_scenes(self, aoi: Any, start: date, end: date) -> list[Scene]:
        """One :class:`Scene` per acquisition day over ``aoi`` in ``[start, end]``.

        Days before the Sentinel-2 cutoff are dropped, the several tiles of one
        day are collapsed into a single scene (the Process API mosaics them
        anyway) keeping the lowest cloud cover, and scenes above
        ``max_cloud_cover`` are discarded when a threshold is set.
        """
        if end < SENTINEL_CUTOFF:
            return []
        search_start = max(start, SENTINEL_CUTOFF)
        if end < search_start:
            return []

        bbox = self.bbox(aoi)
        per_day: dict[date, Scene] = {}
        for item in self.catalog.search(
            self.collection,
            bbox=bbox,
            time=(search_start.isoformat(), end.isoformat()),
        ):
            properties = item.get("properties") or {}
            acquired = _acquired_on(properties)
            if acquired is None or not search_start <= acquired <= end:
                continue
            cloud_cover = properties.get("eo:cloud_cover")
            if (
                self.max_cloud_cover is not None
                and cloud_cover is not None
                and float(cloud_cover) > self.max_cloud_cover
            ):
                continue
            existing = per_day.get(acquired)
            if existing is not None and not _cloudier(existing.cloud_cover, cloud_cover):
                existing.metadata["ids"].append(item.get("id"))
                continue
            scene = Scene(
                id=str(item.get("id") or acquired.isoformat()),
                acquired=acquired,
                source=self.source_name,
                cloud_cover=None if cloud_cover is None else float(cloud_cover),
                metadata={
                    "ids": ([] if existing is None else existing.metadata["ids"])
                    + [item.get("id")],
                    "platform": properties.get("platform"),
                },
            )
            per_day[acquired] = scene
        return [per_day[day] for day in sorted(per_day)]

    def fetch(self, scene: Scene, aoi: Any, band: Band) -> np.ndarray:
        """Render ``scene``'s day over ``aoi`` and return a uint8 chip.

        ``HxWx3`` for ``rgb`` (true colour) and ``HxWx1`` for ``nir`` (B08).
        """
        from cssic.imagery.chips import reflectance_to_uint8

        if band not in ("rgb", "nir"):
            raise ValueError(f"band must be 'rgb' or 'nir', got {band!r}")

        mime = self._mime(band)
        evalscript = TRUE_COLOR_EVALSCRIPT if band == "rgb" else NIR_EVALSCRIPT
        day = scene.acquired
        time_interval = (day.isoformat(), day.isoformat())

        data = self._request(self.bbox(aoi), time_interval, evalscript, mime).get_data()
        if not data or data[0] is None:
            raise RuntimeError(
                f"Sentinel Hub returned no {band} data for {scene.id} on {day.isoformat()}"
            )

        array = np.asarray(data[0])
        if band == "nir":
            array = np.squeeze(array)
            if array.ndim == 3:
                array = array[..., 0]
            return reflectance_to_uint8(array)[..., np.newaxis]
        if array.ndim == 2:
            array = np.repeat(array[..., np.newaxis], 3, axis=-1)
        if array.shape[-1] < 3:
            raise ValueError(f"rgb chip needs 3 bands, got {array.shape[-1]}")
        return reflectance_to_uint8(array[..., :3])

    @staticmethod
    def _mime(band: Band) -> Any:
        from sentinelhub import MimeType

        # True colour comes back already scaled to bytes; NIR stays float
        # reflectance in a GeoTIFF and is scaled by ``reflectance_to_uint8``.
        return MimeType.PNG if band == "rgb" else MimeType.TIFF

    def dates(self, aoi: Any, start: date, end: date) -> list[date]:
        """Acquisition days over ``aoi`` -- v2's ``_catalog_dates``, as dates."""
        return [scene.acquired for scene in self.find_scenes(aoi, start, end)]


def _cloudier(current: float | None, candidate: float | None) -> bool:
    """True when ``candidate`` is a better (less cloudy) scene than ``current``."""
    if candidate is None:
        return False
    if current is None:
        return True
    return float(candidate) < float(current)
