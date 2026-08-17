"""cssic: extract OSM construction sites and download Sentinel-2 / Planet imagery."""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cssic",
        description="Extract OSM construction sites and download Sentinel-2 / Planet imagery.",
        epilog=(
            "No GUI: ohsome/osmium jobs can be long and Planet orders use quota. "
            "Credentials come from the environment (see .env.example). "
            "Planet: PLANET_API_KEY or `planet auth login`. "
            "Sentinel STAC (`gather -s stac`) needs no key. "
            "Process API (`gather -s s`): SH_CLIENT_ID / SH_CLIENT_SECRET from the CDSE dashboard. "
            "python extract_sites.py and python gather_images.py still work."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Create temp/ and output/ directories")

    extract = sub.add_parser("extract", help="Extract construction sites (ohsome by default)")
    extract.add_argument("-s", "--start", required=True, help="Start date YYYY-MM-DD (on or after 2015-06-22)")
    extract.add_argument("-e", "--end", required=True, help="End date YYYY-MM-DD (at least 10 days before today)")
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
    extract.add_argument("--keep-temp", action="store_true", help="Keep daily osmium snapshots")
    extract.add_argument(
        "--restrict-window",
        action="store_true",
        help="Do not search before start / after end for true construction dates",
    )
    extract.add_argument("--save-wip", action="store_true", help="Also save in-progress sites")

    gather = sub.add_parser("gather", help="Download imagery for extracted sites")
    gather.add_argument("-s", "--source", required=True, help="'p'/'planet', 's'/'sentinel' (Process API), or 'stac'")
    gather.add_argument("-n", "--num-images", default="3", help=">=3 samples, or -1 for every available date")
    gather.add_argument("-p", "--padding", default="1", help="Area scale factor (1 = exact bounding box)")
    gather.add_argument("-C", "--rgb", action="store_true", help="Download RGB (at least one of --rgb / --nir required)")
    gather.add_argument("-N", "--nir", action="store_true", help="Download NIR")
    gather.add_argument("-v", "--verbose", action="store_true")
    gather.add_argument("-e", "--email", action="store_true", help="Planet email notification when an order is ready")
    gather.add_argument("--api", "--api-key", dest="api", help="Planet API key (or set PLANET_API_KEY)")
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

    sub.add_parser("reset-extract", help="Delete extract_sites outputs")
    sub.add_parser("reset-images", help="Delete downloaded imagery")
    return parser


def _extract_argv(args: argparse.Namespace) -> list[str]:
    argv = ["--start", args.start, "--end", args.end, "--poly", args.poly, "--backend", args.backend]
    if args.region:
        argv.extend(["--region", args.region])
    if args.keep_temp:
        argv.append("--keep-temp")
    if args.restrict_window:
        argv.append("--restrict-window")
    if args.save_wip:
        argv.append("--save-wip")
    return argv


def _gather_argv(args: argparse.Namespace) -> list[str]:
    argv = ["--source", args.source, "--num-images", str(args.num_images), "--padding", str(args.padding)]
    if args.rgb:
        argv.append("--rgb")
    if args.nir:
        argv.append("--nir")
    if args.verbose:
        argv.append("--verbose")
    if args.email:
        argv.append("--email")
    if args.api:
        argv.extend(["--api", args.api])
    if args.download_planet:
        argv.append("--download")
    if args.sh_client_id:
        argv.extend(["--sh-client-id", args.sh_client_id])
    if args.sh_client_secret:
        argv.extend(["--sh-client-secret", args.sh_client_secret])
    if args.sentinel_provider:
        argv.extend(["--sentinel-provider", args.sentinel_provider])
    return argv


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "setup":
        from workspace import setup_directory

        setup_directory()
        return 0

    if args.command == "extract":
        import extract_sites

        extract_sites.params = extract_sites.get_input(_extract_argv(args))
        _, collection_gdf = extract_sites.locate_construction()
        extract_sites.create_dataset(collection_gdf)
        return 0

    if args.command == "gather":
        import gather_images

        gather_images.parameters = gather_images.get_input(_gather_argv(args))
        geodf = gather_images.setup()
        if geodf is None:
            return 2
        gather_images.gather_from_source(geodf)
        return 0

    if args.command == "reset-extract":
        import reset_extract

        reset_extract.reset()
        return 0

    if args.command == "reset-images":
        import reset_images

        reset_images.reset()
        return 0

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
