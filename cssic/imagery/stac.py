"""Planetary Computer Sentinel-2 L2A via STAC.

Needs no credentials: the Planetary Computer STAC API is open and asset hrefs
are signed on the way out with :func:`planetary_computer.sign_inplace`,
installed as a *modifier* on the ``pystac_client.Client`` so every item this
source hands back already carries readable hrefs.

Port of the v2 ``sentinel_stac.py``. Two behaviour changes:

* windowing goes through :mod:`cssic.imagery.chips`, which reprojects the WGS84
  AOI into the scene's UTM CRS first (v2 masked WGS84 against a UTM COG and
  died with "Input shapes do not overlap raster");
* per sampled date window we keep the acquisition that is least cloudy *over
  the site*, ranked on the L2A scene-classification band (see
  :func:`scl_cloud_fraction`), rather than the first one returned;
* raw bands are offset-corrected for Sentinel-2 processing baseline 04.00,
  which shifted L2A digital numbers by ``BOA_ADD_OFFSET`` (see
  :data:`BOA_ADD_OFFSET`); v2 predates the baseline change and would render
  every post-2022 chip about 10% too bright.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any

import numpy as np

from cssic.dates import DateWindow
from cssic.imagery.base import Band, Scene
from cssic.imagery.chips import read_window, to_image_array

#: Sentinel-2 L2A is not available before this date.
SENTINEL_CUTOFF = date(2015, 6, 23)
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-2-l2a"
SOURCE = "sentinel"

#: Asset keys tried for each band, in order of preference.
RGB_ASSETS: tuple[str, ...] = ("B04", "B03", "B02")
NIR_ASSETS: tuple[str, ...] = ("B08", "nir")
#: Asset key of the L2A scene classification layer (20 m, one band).
SCL_ASSET = "SCL"

#: SCL classes that make a pixel unusable for observing a construction site:
#: 3 cloud shadow, 8/9 cloud medium/high probability, 10 thin cirrus, 11 snow.
CLOUDY_SCL_CLASSES: frozenset[int] = frozenset({3, 8, 9, 10, 11})
#: SCL class 0: the pixel was not observed at all. It is neither cloud nor
#: clear and must not count towards either.
SCL_NODATA = 0
#: How many acquisitions of a window are ranked on SCL. Each rank costs one
#: small range request, so the list is trimmed to the least cloudy by
#: ``eo:cloud_cover`` after collapsing reprocessed duplicates of one acquisition.
SCL_CANDIDATES = 5

#: Digital-number offset added to L2A bands from processing baseline 04.00
#: (ESA, 25 January 2022): reflectance is ``(DN + BOA_ADD_OFFSET) / 10000``.
#: The pre-rendered ``visual`` asset already has it applied.
BOA_ADD_OFFSET = -1000.0
#: First processing baseline that carries the offset.
OFFSET_BASELINE = 4.0

#: GDAL settings that make reading a remote COG one range request instead of a
#: directory listing.
_GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSOLIDATED_RANGES": "YES",
}


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def boa_offset(item: Any) -> float:
    """``BOA_ADD_OFFSET`` for a STAC item, or ``0.0`` for pre-baseline-04.00 data."""
    properties = getattr(item, "properties", {}) or {}
    raw = properties.get("s2:processing_baseline")
    try:
        baseline = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return BOA_ADD_OFFSET if baseline >= OFFSET_BASELINE else 0.0


def scene_from_item(item: Any) -> Scene:
    """Wrap a pystac ``Item`` in a :class:`~cssic.imagery.base.Scene`."""
    properties = getattr(item, "properties", {}) or {}
    acquired = _as_date(getattr(item, "datetime", None)) or _as_date(properties.get("datetime"))
    if acquired is None:
        raise ValueError(f"STAC item {getattr(item, 'id', '?')} has no acquisition date")
    cloud = properties.get("eo:cloud_cover")
    return Scene(
        id=str(item.id),
        acquired=acquired,
        source=SOURCE,
        href=None,
        cloud_cover=None if cloud is None else float(cloud),
        metadata={
            "item": item,
            "collection": getattr(item, "collection_id", None),
            "offset": boa_offset(item),
        },
    )


def covers_aoi(item: Any, aoi: Any) -> bool:
    """True when the item's footprint contains the whole AOI.

    A STAC ``intersects`` search also returns scenes that clip a corner of the
    site; the part of the chip outside the scene comes back as nodata fill, so
    a partial scene is only worth keeping when nothing covers the site.
    """
    from shapely.geometry import shape

    geometry = getattr(item, "geometry", None)
    if not geometry:
        return False
    try:
        return bool(shape(geometry).covers(aoi))
    except (ValueError, TypeError, AttributeError):
        return False


def _scene_cloud_key(scene: Scene) -> tuple[float, date]:
    return (
        float("inf") if scene.cloud_cover is None else scene.cloud_cover,
        scene.acquired,
    )


def one_per_acquisition(scenes: Iterable[Scene]) -> list[Scene]:
    """Collapse scenes sharing an acquisition date to the least cloudy tile.

    The Planetary Computer keeps reprocessed products (a newer processing
    baseline) next to the originals, so one overpass commonly shows up as two
    items with identical pixels. Ranking both wastes an SCL read and lets a
    duplicate crowd a genuinely different acquisition out of the candidate
    list; keeping both on ``-n -1`` writes the same day twice.
    """
    best: dict[date, Scene] = {}
    for scene in sorted(scenes, key=_scene_cloud_key):
        best.setdefault(scene.acquired, scene)
    return sorted(best.values(), key=_scene_cloud_key)


def least_cloudy(scenes: Iterable[Scene]) -> Scene | None:
    """The least cloudy scene by ``eo:cloud_cover``, ties broken by date."""
    candidates = list(scenes)
    if not candidates:
        return None
    return min(candidates, key=_scene_cloud_key)


def scl_cloud_fraction(array: Any) -> float | None:
    """Fraction of an SCL window that is cloud, cloud shadow, cirrus or snow.

    ``eo:cloud_cover`` describes a 110x110 km tile, which says nothing about a
    200 m construction site inside it: in the v3 smoke run a quarter of the RGB
    chips were uniformly white because the AOI itself sat under cloud on a tile
    reported as 20-36% cloudy. The L2A scene classification layer is per pixel,
    so the same window read that produces the chip answers the real question.

    ``None`` for an empty window, which carries no evidence either way.
    """
    pixels = np.asarray(array)
    observed = pixels[pixels != SCL_NODATA]
    if observed.size == 0:
        return None
    flagged = np.isin(observed, list(CLOUDY_SCL_CLASSES))
    return float(np.count_nonzero(flagged)) / float(observed.size)


class STACSentinelSource:
    """A :class:`~cssic.imagery.base.ImageSource` over Sentinel-2 L2A on the PC.

    ``client`` (a ``pystac_client.Client``) is injected so tests never hit the
    network. When it is ``None`` the client is opened lazily on first use with
    the Planetary Computer signing modifier.
    """

    def __init__(
        self,
        client: Any | None = None,
        collection: str = COLLECTION,
        url: str = STAC_URL,
        max_cloud_cover: float | None = None,
        verbose: bool = False,
    ) -> None:
        self._client = client
        self.collection = collection
        self.url = url
        self.max_cloud_cover = max_cloud_cover
        self.verbose = verbose

    def _say(self, message: str) -> None:
        if self.verbose:
            print(message)

    # -- search -----------------------------------------------------------
    @property
    def client(self) -> Any:
        """The STAC client, opened with ``planetary_computer.sign_inplace``."""
        if self._client is None:
            import planetary_computer
            from pystac_client import Client

            self._client = Client.open(self.url, modifier=planetary_computer.sign_inplace)
        return self._client

    def find_scenes(
        self,
        aoi: Any,
        start: date,
        end: date,
        limit: int | None = None,
    ) -> list[Scene]:
        """Scenes covering ``aoi`` (shapely geometry, EPSG:4326) in ``[start, end]``."""
        from shapely.geometry import mapping

        if end < SENTINEL_CUTOFF:
            return []
        if start < SENTINEL_CUTOFF:
            start = SENTINEL_CUTOFF

        query = {}
        if self.max_cloud_cover is not None:
            query["eo:cloud_cover"] = {"lt": self.max_cloud_cover}
        search = self.client.search(
            collections=[self.collection],
            intersects=mapping(aoi),
            datetime=f"{start.isoformat()}/{end.isoformat()}",
            query=query or None,
            sortby=[{"field": "properties.datetime", "direction": "asc"}],
            max_items=limit,
        )
        scenes: list[Scene] = []
        covering: list[Scene] = []
        for item in search.items():
            scene = scene_from_item(item)
            if covers_aoi(item, aoi):
                covering.append(scene)
            scenes.append(scene)
        chosen = covering or scenes
        chosen.sort(key=lambda scene: scene.acquired)
        return chosen

    def sample_scenes(
        self,
        aoi: Any,
        windows: Sequence[DateWindow],
        all_dates: bool = False,
    ) -> list[Scene]:
        """One scene per sampled date window, clearest over the site, de-duplicated.

        ``windows`` comes from :func:`cssic.dates.sample_date_windows`.
        ``all_dates`` (``-n -1``) keeps every acquisition in the window instead,
        one per date. It is passed explicitly rather than inferred from the
        number of windows, because ``-n 1`` also asks for a single window and
        must still yield a single image.
        """
        picked: list[Scene] = []
        seen: set[str] = set()
        dates: set[date] = set()
        for left, right in windows:
            found = self.find_scenes(aoi, left, right)
            if all_dates:
                chosen = [
                    scene
                    for scene in one_per_acquisition(found)
                    if scene.acquired not in dates and not dates.add(scene.acquired)
                ]
            else:
                chosen = [s for s in (self.clearest_over_aoi(aoi, found),) if s is not None]
            for scene in chosen:
                if scene.id not in seen:
                    seen.add(scene.id)
                    picked.append(scene)
        picked.sort(key=lambda scene: scene.acquired)
        return picked

    # -- cloud over the site ----------------------------------------------
    def aoi_cloud_fraction(self, scene: Scene, aoi: Any) -> float | None:
        """Cloud fraction of ``aoi`` in ``scene``, from its SCL band.

        ``None`` when the item publishes no SCL asset or the read fails; the
        caller then falls back to the scene-level ``eo:cloud_cover``.
        """
        href = self._asset_href(scene, SCL_ASSET)
        if href is None:
            return None
        try:
            window = self._read(href, aoi, indexes=[1])
        except (RuntimeError, ValueError, OSError) as exc:
            self._say(f"\tcould not read SCL for {scene.id}: {exc}")
            return None
        return scl_cloud_fraction(window)

    def clearest_over_aoi(self, aoi: Any, scenes: Iterable[Scene]) -> Scene | None:
        """The candidate whose *site* is least cloudy, not whose tile is.

        The SCL band is 20 m and one band, so ranking a candidate costs a single
        small range request over the AOI window -- but only the
        :data:`SCL_CANDIDATES` least cloudy scenes by ``eo:cloud_cover`` are
        ranked, so a busy window cannot turn one chip into a dozen reads. A
        candidate with no SCL asset keeps its scene-level score.
        """
        candidates = one_per_acquisition(scenes)[:SCL_CANDIDATES]
        if len(candidates) < 2:
            # Nothing to rank. Most sampled windows land here, so paying for an
            # SCL read would double the requests without changing the pick; a
            # single cloudy candidate is caught by is_blank at write time.
            return candidates[0] if candidates else None
        best, best_score = candidates[0], float("inf")
        for scene in candidates:
            fraction = self.aoi_cloud_fraction(scene, aoi)
            source = "SCL"
            if fraction is None:
                fraction = _scene_cloud_key(scene)[0] / 100.0
                source = "eo:cloud_cover"
            self._say(f"\t{scene.acquired} {scene.id}: site cloud {fraction:.2f} ({source})")
            if fraction < best_score:
                best, best_score = scene, fraction
            if best_score <= 0.0:
                break
        self._say(f"\tpicked {best.id} ({best.acquired}) at {best_score:.2f} cloud over the site")
        return best

    # -- fetch ------------------------------------------------------------
    def _asset_href(self, scene: Scene, key: str) -> str | None:
        item = scene.metadata.get("item")
        assets = getattr(item, "assets", None) or {}
        asset = assets.get(key)
        return None if asset is None else asset.href

    def _read(self, href: str, aoi: Any, indexes: Any = None) -> np.ndarray:
        import rasterio

        with rasterio.Env(**_GDAL_ENV), rasterio.open(href) as src:
            return read_window(src, aoi, aoi_crs="EPSG:4326", indexes=indexes)

    def fetch(self, scene: Scene, aoi: Any, band: Band) -> np.ndarray:
        """Return a uint8 array, HxWx3 for ``rgb`` and HxWx1 for ``nir``."""
        # The pre-rendered ``visual`` asset is already 8-bit and already
        # offset-corrected; only raw bands need BOA_ADD_OFFSET.
        offset = float(scene.metadata.get("offset") or 0.0)
        if band == "rgb":
            visual = self._asset_href(scene, "visual")
            if visual is not None:
                return to_image_array(self._read(visual, aoi, indexes=[1, 2, 3]), "rgb")
            planes = []
            for key in RGB_ASSETS:
                href = self._asset_href(scene, key)
                if href is None:
                    raise ValueError(f"scene {scene.id} has no {key} or visual asset")
                planes.append(self._read(href, aoi, indexes=[1])[0])
            rows = min(plane.shape[0] for plane in planes)
            cols = min(plane.shape[1] for plane in planes)
            stacked = np.stack([plane[:rows, :cols] for plane in planes], axis=0)
            return to_image_array(stacked, "rgb", offset=offset)

        for key in NIR_ASSETS:
            href = self._asset_href(scene, key)
            if href is not None:
                return to_image_array(self._read(href, aoi, indexes=[1]), "nir", offset=offset)
        raise ValueError(f"scene {scene.id} has no NIR asset (tried {', '.join(NIR_ASSETS)})")
