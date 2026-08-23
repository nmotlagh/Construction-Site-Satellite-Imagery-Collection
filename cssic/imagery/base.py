"""The ``ImageSource`` protocol shared by the Sentinel-2 and Planet backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol, runtime_checkable

#: The two bands the paper collects.
Band = Literal["rgb", "nir"]
BANDS: tuple[str, ...] = ("rgb", "nir")


@dataclass(frozen=True)
class Scene:
    """One acquisition covering an AOI.

    ``href`` is whatever the backend needs to fetch the pixels (a STAC asset
    href, a Planet item id, ...); ``metadata`` carries backend extras such as
    cloud cover.
    """

    id: str
    acquired: date
    source: str
    href: str | None = None
    cloud_cover: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ImageSource(Protocol):
    """Find and fetch imagery chips for an area of interest."""

    def find_scenes(self, aoi: Any, start: date, end: date) -> list[Scene]:
        """Scenes covering ``aoi`` (a shapely geometry, EPSG:4326) in the window."""
        ...

    def fetch(self, scene: Scene, aoi: Any, band: Band) -> Any:
        """Return a uint8 ``numpy`` array, HxWx3 for ``rgb`` and HxWx1 for ``nir``."""
        ...
