"""Imagery backends behind the :class:`~cssic.imagery.base.ImageSource` protocol.

``sentinelhub``, ``planet``, ``rasterio`` and ``pystac_client`` are imported
lazily inside the backend that needs them.
"""

from __future__ import annotations

from cssic.imagery.base import BANDS, Band, ImageSource, Scene

__all__ = ["BANDS", "Band", "ImageSource", "Scene", "get_image_source"]


def get_image_source(source: str, **kwargs):
    """Construct the named backend (``"stac"``, ``"sentinel"`` or ``"planet"``)."""
    if source == "stac":
        from cssic.imagery.stac import STACSentinelSource

        return STACSentinelSource(**kwargs)
    if source == "sentinel":
        from cssic.imagery.sentinelhub import SentinelHubSource

        return SentinelHubSource(**kwargs)
    if source == "planet":
        from cssic.imagery.planet import PlanetSource

        return PlanetSource(**kwargs)
    raise ValueError(f"unknown imagery source {source!r}; use 'stac', 'sentinel' or 'planet'")
