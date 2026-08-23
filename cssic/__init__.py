"""cssic: semi-automatic collection of temporal satellite imagery over construction sites.

Implements the method of *A Framework for Semi-automatic Collection of Temporal
Satellite Imagery for Analysis of Dynamic Regions* (Motlagh et al., ICCVW 2021).

The vocabulary of the paper is used throughout: a **construction chain** is one
physical site that may be represented by several OSM way ids over time; each
chain carries a **previous tag** (land use before construction), a **final tag**
(land use after construction) -- collectively **boundary tags** -- recovered by
ranking candidate polygons with **IOU confidence**.

Importing this package must not require any heavy or optional dependency
(``osmium``, ``sentinelhub``, ``planet``, ``rasterio``); backends import those
lazily.
"""

from __future__ import annotations

from importlib import metadata as _metadata

from cssic.config import Credentials, ExtractConfig, GatherConfig
from cssic.dates import padding_scale, sample_date_windows
from cssic.poly import bbox_csv, load_poly, polygon_bounds
from cssic.sites import Site, SiteCollection, intersection_over_union
from cssic.store import Workspace

try:
    __version__ = _metadata.version("construction-site-satellite-imagery")
except _metadata.PackageNotFoundError:  # running from a checkout, not installed
    __version__ = "3.0.0"

__all__ = [
    "Credentials",
    "ExtractConfig",
    "GatherConfig",
    "Site",
    "SiteCollection",
    "Workspace",
    "__version__",
    "bbox_csv",
    "intersection_over_union",
    "load_poly",
    "padding_scale",
    "polygon_bounds",
    "sample_date_windows",
]
