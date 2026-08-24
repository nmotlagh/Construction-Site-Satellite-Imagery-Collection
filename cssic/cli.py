"""``cssic`` command line: setup | extract | gather | reset-extract | reset-images.

Argument parsing and dispatch only: the commands themselves live in
:mod:`cssic.extract` and :mod:`cssic.gather`. Flags stay compatible with the
v2 README. The one deliberate change is ``-n``, which now accepts any
``n >= 1`` or ``-1`` (the v2 ``>= 3`` rule is gone). The commands raise
:class:`ValueError` for user errors -- bad flags, unreadable inputs, missing
optional backends, rejected credentials -- and ``main`` turns each into one
``ERROR: ...`` line and exit code 2. (An extraction that finds nothing also
exits 2, with a plain explanation rather than an ``ERROR:`` line.)
"""

from __future__ import annotations

import argparse
import sys

from cssic.config import Credentials, ExtractConfig, GatherConfig
from cssic.extract import run_extract
from cssic.gather import run_gather
from cssic.store import Workspace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cssic",
        description="Extract OSM construction sites and download Sentinel-2 / Planet imagery.",
        epilog=(
            "No GUI: ohsome/osmium jobs can be long and Planet orders use quota. "
            "Credentials come from the environment (see .env.example). "
            "Planet: PLANET_API_KEY or `planet auth login`. "
            "Sentinel STAC (`gather -s stac`) needs no key. "
            "Process API (`gather -s s`): SH_CLIENT_ID / SH_CLIENT_SECRET from the "
            "CDSE dashboard."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Create temp/ and output/ directories")

    extract = sub.add_parser("extract", help="Extract construction sites (ohsome by default)")
    extract.add_argument(
        "-s", "--start", required=True, help="Start date YYYY-MM-DD (on or after 2015-06-22)"
    )
    extract.add_argument(
        "-e", "--end", required=True, help="End date YYYY-MM-DD (more than 10 days before today)"
    )
    extract.add_argument("-p", "--poly", required=True, help="Path to an Osmosis .poly file")
    extract.add_argument(
        "-r",
        "--region",
        default=None,
        help="Path to an OSM history .osh.pbf file (required for --backend osmium)",
    )
    extract.add_argument(
        "--backend",
        choices=("ohsome", "osmium"),
        default="ohsome",
        help="ohsome API (default) or original osmium daily snapshots",
    )
    extract.add_argument(
        "--keep-temp",
        action="store_true",
        help=(
            "Keep the per-day osmium dumps in temp/snapshots "
            "(the ohsome snapshot cache is always kept)"
        ),
    )
    extract.add_argument(
        "--restrict-window",
        action="store_true",
        help="Do not search before start / after end for true construction dates",
    )
    extract.add_argument("--save-wip", action="store_true", help="Also save in-progress sites")
    extract_noise = extract.add_mutually_exclusive_group()
    extract_noise.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Also echo the day-by-day walk (one line per observed day)",
    )
    extract_noise.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Print nothing but the final summary",
    )

    gather = sub.add_parser("gather", help="Download imagery for extracted sites")
    gather.add_argument(
        "-s",
        "--source",
        required=True,
        help="'p'/'planet', 's'/'sentinel' (Process API), or 'stac'",
    )
    gather.add_argument(
        "-n",
        "--num-images",
        default="3",
        help="Number of samples across the construction period (>=1), or -1 for every date",
    )
    gather.add_argument("-p", "--padding", default="1", help="Area scale factor (1 = exact bbox)")
    gather.add_argument(
        "--day-padding",
        dest="day_padding",
        default="6",
        help="Days the search reaches past each sampled date (default: 6, the v2 value)",
    )
    gather.add_argument(
        "-C", "--rgb", action="store_true", help="Download RGB (at least one of --rgb / --nir)"
    )
    gather.add_argument("-N", "--nir", action="store_true", help="Download NIR")
    gather.add_argument(
        "--max-cloud",
        dest="max_cloud",
        default=None,
        help="Discard acquisitions cloudier than this percentage (default: keep all)",
    )
    gather.add_argument("-v", "--verbose", action="store_true")
    gather.add_argument(
        "-e",
        "--email",
        action="store_true",
        help="Planet email notification when an order is ready",
    )
    gather.add_argument("--api", "--api-key", dest="api", help="Planet API key (or PLANET_API_KEY)")
    gather.add_argument(
        "--download",
        "--download-planet",
        dest="download_planet",
        action="store_true",
        help="Download previously created Planet orders",
    )
    gather.add_argument("--sh-client-id", help="Sentinel Hub / CDSE OAuth client id")
    gather.add_argument("--sh-client-secret", help="Sentinel Hub / CDSE OAuth client secret")
    gather.add_argument(
        "--sentinel-provider",
        choices=("cdse", "sentinelhub"),
        help="cdse (default, Copernicus Data Space) or commercial sentinelhub",
    )

    sub.add_parser("reset-extract", help="Delete extract outputs")
    sub.add_parser("reset-images", help="Delete downloaded imagery")
    return parser


def extract_config(args: argparse.Namespace) -> ExtractConfig:
    """Build a validated :class:`~cssic.config.ExtractConfig` from parsed args."""
    return ExtractConfig(
        start=args.start,
        end=args.end,
        poly=args.poly,
        region=args.region,
        backend=args.backend,
        keep_temp=args.keep_temp,
        restrict_window=args.restrict_window,
        save_wip=args.save_wip,
        verbose=args.verbose,
        quiet=args.quiet,
    )


def gather_config(args: argparse.Namespace) -> GatherConfig:
    """Build a validated :class:`~cssic.config.GatherConfig` from parsed args."""
    return GatherConfig(
        source=args.source,
        num_images=args.num_images,
        padding=args.padding,
        rgb=args.rgb,
        nir=args.nir,
        verbose=args.verbose,
        email=args.email,
        download_planet=args.download_planet,
        max_cloud_cover=args.max_cloud,
        day_padding=args.day_padding,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ws = Workspace()

    if args.command == "setup":
        ws.setup()
        print(f"Ready: {ws.snapshot_dir} and {ws.collection_dir}")
        return 0

    if args.command == "reset-extract":
        removed = ws.reset_extract()
        print(f"Removed {removed} file(s) from {ws.output_dir} and {ws.snapshot_dir}")
        return 0

    if args.command == "reset-images":
        removed = ws.reset_images()
        print(f"Removed {removed} image file(s) under {ws.output_dir}")
        return 0

    if args.command == "extract":
        # run_extract shares the try: a poly file that exists but does not parse
        # is a user error like a bad date, not a crash.
        try:
            cfg = extract_config(args)
            return run_extract(cfg, ws)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2

    if args.command == "gather":
        # run_gather shares the try like run_extract: an unreadable collection,
        # a missing backend, or rejected credentials are user errors too.
        try:
            cfg = gather_config(args)
            credentials = Credentials.from_env(
                planet_api_key=args.api,
                sh_client_id=args.sh_client_id,
                sh_client_secret=args.sh_client_secret,
                sentinel_provider=args.sentinel_provider,
            )
            return run_gather(cfg, credentials, ws)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
