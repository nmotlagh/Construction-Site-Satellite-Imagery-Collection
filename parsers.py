"""Shared argparse definitions for cssic, extract_sites.py, and gather_images.py."""

from __future__ import annotations

import argparse

EXTRACT_FIELDS = (
    ("start", "start"),
    ("end", "end"),
    ("poly", "poly"),
    ("region", "region"),
    ("backend", "backend"),
    ("keep_temp", "keep-temp"),
    ("restrict_window", "restrict-window"),
    ("save_wip", "save-wip"),
)

GATHER_FIELDS = (
    ("source", "source"),
    ("num_images", "num-images"),
    ("padding", "padding"),
    ("rgb", "rgb"),
    ("nir", "nir"),
    ("verbose", "verbose"),
    ("email", "email"),
    ("api", "api"),
    ("download_planet", "download-planet"),
    ("sh_client_id", "sh-client-id"),
    ("sh_client_secret", "sh-client-secret"),
    ("sentinel_provider", "sentinel-provider"),
)


def namespace_to_dict(args: argparse.Namespace, fields: tuple[tuple[str, str], ...]) -> dict:
    """Map argparse dest names to the hyphenated param keys used by extract/gather."""
    return {key: getattr(args, dest) for dest, key in fields}


def add_extract_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-s", "--start", required=True, help="Start date YYYY-MM-DD (on or after 2015-06-22)")
    parser.add_argument("-e", "--end", required=True, help="End date YYYY-MM-DD (at least 10 days before today)")
    parser.add_argument("-p", "--poly", required=True, help="Path to an Osmosis .poly file")
    parser.add_argument(
        "-r",
        "--region",
        default=None,
        help="Path to an OSM history .osh.pbf file (required for --backend osmium)",
    )
    parser.add_argument(
        "--backend",
        choices=("ohsome", "osmium"),
        default="ohsome",
        help="ohsome API (default, no history dump) or original osmium daily snapshots",
    )
    parser.add_argument("--keep-temp", action="store_true", help="Keep daily osmium snapshots")
    parser.add_argument(
        "--restrict-window",
        action="store_true",
        help="Do not search before start / after end for true construction dates",
    )
    parser.add_argument("--save-wip", action="store_true", help="Also save in-progress sites")


def add_gather_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-s",
        "--source",
        required=True,
        help="'p'/'planet', 's'/'sentinel' (Process API), or 'stac'",
    )
    parser.add_argument("-n", "--num-images", default=3, help=">=3 samples, or -1 for every available date")
    parser.add_argument("-p", "--padding", default=1, help="Area scale factor (1 = exact bounding box)")
    parser.add_argument("-C", "--rgb", action="store_true", help="Download RGB (at least one of --rgb / --nir required)")
    parser.add_argument("-N", "--nir", action="store_true", help="Download NIR")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-e", "--email", action="store_true", help="Planet email notification when an order is ready")
    parser.add_argument(
        "--api",
        "--api-key",
        dest="api",
        default=None,
        help="Planet API key (or set PLANET_API_KEY). Ignored for Sentinel.",
    )
    parser.add_argument(
        "--download",
        "--download-planet",
        dest="download_planet",
        action="store_true",
        help="Download previously created Planet orders",
    )
    parser.add_argument("--sh-client-id", default=None, help="Sentinel Hub / CDSE OAuth client id")
    parser.add_argument("--sh-client-secret", default=None, help="Sentinel Hub / CDSE OAuth client secret")
    parser.add_argument(
        "--sentinel-provider",
        choices=("cdse", "sentinelhub"),
        default=None,
        help="cdse (default, free Copernicus Data Space) or commercial sentinelhub",
    )


def build_extract_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract OSM construction sites in a polygon over a date window."
    )
    add_extract_arguments(parser)
    return parser


def build_gather_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download Sentinel-2 or Planet imagery for extracted construction sites."
    )
    add_gather_arguments(parser)
    return parser
