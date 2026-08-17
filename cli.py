"""cssic: extract OSM construction sites and download Sentinel-2 / Planet imagery."""

from __future__ import annotations

import argparse
import sys

from parsers import add_extract_arguments, add_gather_arguments


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
    add_extract_arguments(extract)

    gather = sub.add_parser("gather", help="Download imagery for extracted sites")
    add_gather_arguments(gather)

    sub.add_parser("reset-extract", help="Delete extract_sites outputs")
    sub.add_parser("reset-images", help="Delete downloaded imagery")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "setup":
        from workspace import setup_directory

        setup_directory()
        return 0

    if args.command == "extract":
        import extract_sites

        _, collection_gdf = extract_sites.locate_construction(extract_sites.params_from_args(args))
        extract_sites.create_dataset(collection_gdf)
        return 0

    if args.command == "gather":
        import gather_images

        params = gather_images.params_from_args(args)
        geodf = gather_images.setup(params=params)
        if geodf is None:
            return 2
        gather_images.gather_from_source(geodf, params)
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
