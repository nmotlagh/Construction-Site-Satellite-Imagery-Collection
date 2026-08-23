"""``cssic`` command line: setup | extract | gather | reset-extract | reset-images.

Flags stay compatible with the v2 README. The one deliberate change is ``-n``,
which now accepts any ``n >= 1`` or ``-1`` (the v2 ``>= 3`` rule is gone).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from cssic.config import Credentials, ExtractConfig, GatherConfig
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


#: What each ``gather -s`` backend needs installed, and the extra that brings it.
BACKEND_REQUIREMENTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "stac": ("stac", ("pystac_client", "planetary_computer", "rasterio", "PIL")),
    "sentinel": ("sentinelhub", ("sentinelhub", "rasterio", "PIL")),
    "planet": ("planet", ("planet",)),
}


def install_hint(extra: str) -> str:
    """How to install one of the optional extras.

    The distribution is not on PyPI, so the hint has to be the checkout form the
    README uses -- ``uv pip install 'construction-site-satellite-imagery[x]'``
    would resolve against an index that has never heard of it.
    """
    return f"`uv sync --extra {extra}` (or `pip install -e '.[{extra}]'`)"


def require_backend(source: str) -> None:
    """Fail with an install hint before any credential or network work happens.

    Optional backends are imported lazily, so without this the first missing
    module surfaces as an ImportError traceback from deep inside a fetch.
    """
    import importlib.util

    extra, modules = BACKEND_REQUIREMENTS.get(source, ("", ()))
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if not missing:
        return
    raise ValueError(
        f"the {source} backend needs {', '.join(missing)}; install it with {install_hint(extra)}"
    )


def require_history_backend(backend: str) -> None:
    """The :func:`require_backend` check for ``extract``'s history backends.

    ohsome needs nothing beyond the core dependencies. osmium needs both an
    importable ``osmium`` module and the ``osmium-tool`` binary, which is not a
    Python package at all -- without this check either one missing surfaces as a
    ``RuntimeError`` traceback halfway through ``run_extract``.
    """
    if backend != "osmium":
        return
    import importlib.util

    if importlib.util.find_spec("osmium") is None:
        raise ValueError(
            f"the osmium backend needs pyosmium; install it with {install_hint('osmium')}"
        )
    from cssic.history.osmium import require_osmium

    try:
        require_osmium()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc


class _SnapshotProgress:
    """History source wrapper that reports a running snapshot count.

    A cold ohsome extract spends ~20 s per daily snapshot, so a two-month window
    is a quarter of an hour during which nothing else prints. Counting here (and
    not in the backend) keeps all output decisions in the CLI.
    """

    def __init__(self, history: object, report=None) -> None:
        self._history = history
        self._report = report
        self.fetches = 0

    def __getattr__(self, name: str):  # data_until() and friends pass through
        return getattr(self._history, name)

    def construction_intervals(self, bbox, window):
        return self._history.construction_intervals(bbox, window)  # type: ignore[attr-defined]

    def snapshot(self, day, bbox):
        self.fetches += 1
        if self._report is not None:
            self._report(f"[snapshot {self.fetches}] {day}")
        return self._history.snapshot(day, bbox)  # type: ignore[attr-defined]


def run_extract(cfg: ExtractConfig, workspace: Workspace | None = None) -> int:
    """Extract construction chains and write them to the workspace via ``store``."""
    from cssic.chains import build_chains
    from cssic.history import get_history_source
    from cssic.poly import load_poly, polygon_bounds

    require_history_backend(cfg.backend)
    ws = workspace or Workspace()
    ws.setup()
    region = load_poly(cfg.poly)
    bbox = polygon_bounds(region)
    if cfg.backend == "osmium":
        history = get_history_source(
            "osmium",
            region=cfg.region,
            poly=cfg.poly,
            workspace=ws,
            keep_temp=cfg.keep_temp,
        )
    else:
        history = get_history_source("ohsome", cache_dir=ws)

    verbosity = cfg.verbosity
    say = print if verbosity >= 1 else _silent
    say(f"Searching {cfg.backend} history over bbox {bbox} for {cfg.start}..{cfg.end}")
    counted = _SnapshotProgress(history, report=say if verbosity >= 1 else None)
    completed, wip = build_chains(
        counted, cfg, bbox=bbox, progress=print if verbosity >= 2 else None
    )
    say(f"Read {counted.fetches} daily snapshot(s)")

    status = 0
    collection_gdf = completed.to_gdf()
    if collection_gdf is None:
        print("No completed construction sites found!")
        status = 2
    else:
        ws.save_collection(collection_gdf)
        count = ws.create_dataset(collection_gdf)
        print(f"Saved {count} construction chains to {ws.collection_dir}")

    _report_wip(cfg, ws, wip)
    # --keep-temp governs osmium's per-day dumps only: the ohsome snapshot cache
    # is keyed by day+bbox and turns a 15-minute cold run into a 25-second one,
    # so deleting it behind the user's back would be a trap, not a cleanup.
    if cfg.backend == "osmium" and not cfg.keep_temp:
        _clear_snapshots(ws)
    return status


def _silent(_message: str) -> None:
    """``print`` for ``--quiet``."""


def _report_wip(cfg: ExtractConfig, ws: Workspace, wip) -> None:
    """Say what happened to the chains still under construction at ``cfg.end``.

    Without ``--save-wip`` these are dropped, and silently dropping sites the
    user can see missing from ``output/`` reads as a bug.
    """
    wip_gdf = wip.to_gdf()
    count = 0 if wip_gdf is None else len(wip_gdf.index)
    if not cfg.save_wip:
        if count:
            print(
                f"Skipped {count} chain(s) still in progress at {cfg.end} "
                "(keep them with --save-wip)"
            )
        return
    if wip_gdf is None:
        print("No in-progress construction sites found.")
    else:
        ws.save_collection(wip_gdf, stem="in_progress")
        print(f"Saved {count} in-progress chains to {ws.collection_dir}")


def _clear_snapshots(ws: Workspace) -> None:
    """Drop cached daily snapshots unless the user asked to keep them."""
    import shutil

    if ws.snapshot_dir.is_dir():
        shutil.rmtree(ws.snapshot_dir, ignore_errors=True)
        ws.snapshot_dir.mkdir(parents=True, exist_ok=True)


def _chain_logger(chain_id: str):
    """A one-line logger for one construction chain, prefixed with its id."""

    def log(message: str) -> None:
        print(f"{chain_id}: {message}")

    return log


def _log_empty_windows(windows: list[tuple[date, date]], scenes: list, log=None) -> None:
    """Name every sampled window that came back without an acquisition.

    ``-n 3`` over a site whose windows are shorter than the Sentinel-2 revisit
    silently wrote one chip in v3.0; a user reading "Wrote 1 chip(s)" has no way
    to tell that from "this site only existed for a week".
    """
    if log is None:
        return
    covered = {scene.acquired for scene in scenes}
    for window_start, window_end in windows:
        if not any(window_start <= day <= window_end for day in covered):
            log(f"no acquisition between {window_start} and {window_end}")


def _sample_scenes(
    source: object,
    aoi: object,
    windows: list[tuple[date, date]],
    all_dates: bool = False,
    log=None,
) -> list:
    """One scene per sampled window, using the backend's sampler when it has one.

    ``all_dates`` is ``-n -1``: keep every acquisition (one per date) instead of
    the least cloudy one. It is an explicit flag rather than "there is only one
    window", because ``-n 1`` also produces exactly one window and must still
    come back with a single image.
    """
    sampler = getattr(source, "sample_scenes", None)
    if callable(sampler):
        sampled = list(sampler(aoi, windows, all_dates=all_dates))
        _log_empty_windows(windows, sampled, log)
        return sampled

    picked: list = []
    seen: set[str] = set()
    seen_dates: set[date] = set()
    for window_start, window_end in windows:
        found = source.find_scenes(aoi, window_start, window_end)  # type: ignore[attr-defined]
        if not found:
            continue
        if all_dates:
            chosen = [scene for scene in found if scene.acquired not in seen_dates]
            seen_dates.update(scene.acquired for scene in chosen)
        else:
            with_cloud = [s for s in found if s.cloud_cover is not None]
            chosen = [min(with_cloud, key=lambda s: s.cloud_cover)] if with_cloud else [found[0]]
        for scene in chosen:
            if scene.id not in seen:
                seen.add(scene.id)
                picked.append(scene)
    picked.sort(key=lambda scene: scene.acquired)
    _log_empty_windows(windows, picked, log)
    return picked


#: Third-party packages whose exceptions mean "the OAuth handshake was rejected".
AUTH_ERROR_PACKAGES = ("oauthlib", "requests_oauthlib")
#: HTTP statuses the CDSE / Sentinel Hub token endpoint answers bad keys with.
AUTH_ERROR_STATUSES = (400, 401, 403)


def _is_auth_error(exc: BaseException) -> bool:
    """True for the errors a rejected Sentinel Hub client id raises.

    The SDK does not wrap them, so the only handle is the defining package
    (``oauthlib`` raises ``InvalidClientError``) or the status on an attached
    ``requests`` response. Matching that narrowly keeps genuine bugs and
    transport failures raising instead of being relabelled as bad credentials.
    """
    for cls in type(exc).__mro__:
        if (getattr(cls, "__module__", "") or "").split(".")[0] in AUTH_ERROR_PACKAGES:
            return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in AUTH_ERROR_STATUSES


def _check_sentinel_credentials(source: object) -> None:
    """Fetch an OAuth token now, so bad keys are one ERROR line and not 25.

    ``SentinelHubSource`` authenticates lazily on the first catalog search, i.e.
    once per site, deep inside the per-site try/except -- an invalid client id
    would otherwise print SEARCH FAILED for every chain and then still exit 1
    with an oauthlib traceback.
    """
    config = getattr(source, "config", None)
    if config is None:  # a fake or a backend that does not authenticate
        return
    try:
        from sentinelhub import SentinelHubSession
    except ImportError:  # pragma: no cover - require_backend already checked
        return
    try:
        SentinelHubSession(config=config)
    except Exception as exc:
        if not _is_auth_error(exc):
            raise
        raise ValueError(
            "Sentinel Hub rejected the credentials. Create an OAuth client at "
            "https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings "
            "and set SH_CLIENT_ID / SH_CLIENT_SECRET (see .env.example), or pass "
            f"--sh-client-id / --sh-client-secret. API said: {exc}"
        ) from exc


def _planet_exceptions() -> tuple[tuple[type[BaseException], ...], type[BaseException]]:
    """``(auth errors, every SDK error)``, tolerant of planet SDK renames."""
    from planet import exceptions

    base = getattr(exceptions, "PlanetError", Exception)
    auth = tuple(
        cls
        for cls in (
            getattr(exceptions, name, None)
            for name in ("InvalidAPIKey", "InvalidIdentity", "NoPermission")
        )
        if isinstance(cls, type) and issubclass(cls, BaseException)
    )
    return auth, base


def _run_planet(cfg: GatherConfig, credentials: Credentials, ws: Workspace, gdf) -> int:
    """Create or download Planet orders (Planet has no synchronous pixel endpoint)."""
    from cssic.imagery import get_image_source

    source = get_image_source("planet", credentials=credentials, cfg=cfg)
    auth_errors, PlanetError = _planet_exceptions()

    try:
        if cfg.download_planet:
            collected = source.download_orders(workspace=ws)
            print(f"Collected {len(collected)} Planet order(s)")
        else:
            orders = source.bulk_order(gdf, workspace=ws)
            print(
                f"Created {len(orders)} Planet order(s); rerun with --download when they are ready"
            )
    except auth_errors as exc:
        # Planet is the one backend that bills, so fail loudly but readably.
        raise ValueError(
            "Planet rejected the credentials. Set PLANET_API_KEY (see .env.example), "
            f"pass --api-key, or run `planet auth login`. API said: {exc}"
        ) from exc
    except PlanetError as exc:
        raise ValueError(f"Planet API error: {exc}") from exc
    return 0


def run_gather(
    cfg: GatherConfig,
    credentials: Credentials,
    workspace: Workspace | None = None,
) -> int:
    """Download imagery chips for every extracted construction chain."""
    from shapely.geometry import box

    from cssic.dates import sample_date_windows
    from cssic.imagery import get_image_source
    from cssic.imagery.chips import is_blank, padded_bounds, save_png

    ws = workspace or Workspace()
    ws.setup()
    try:
        gdf = ws.load_collection()
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"ERROR: cannot read {ws.collection_path()}: {exc}")
        return 2
    if gdf is None:
        print("No construction sites found! Run `cssic extract` first.")
        return 2
    ws.prepare_image_dirs(gdf, cfg.source_dir, cfg.bands)

    try:
        require_backend(cfg.source)
        if cfg.source == "planet":
            return _run_planet(cfg, credentials, ws, gdf)
        if cfg.source == "stac":
            source = get_image_source(
                "stac", max_cloud_cover=cfg.max_cloud_cover, verbose=cfg.verbose
            )
        else:
            source = get_image_source(
                "sentinel", credentials=credentials, max_cloud_cover=cfg.max_cloud_cover
            )
            _check_sentinel_credentials(source)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    all_dates = cfg.num_images == -1
    written = 0
    for _, row in gdf.iterrows():
        chain_id = str(row["chain_id"])
        aoi = box(*padded_bounds(row["geometry"], cfg.padding))
        start = date.fromisoformat(str(row["start"]))
        end = date.fromisoformat(str(row["end"]))
        windows = sample_date_windows(start, end, cfg.num_images, cfg.day_padding)
        log = _chain_logger(chain_id)
        # One site's search failing (a transient API error, a bad geometry) must
        # not lose the sites after it: a gather run can take hours.
        try:
            scenes = _sample_scenes(source, aoi, windows, all_dates=all_dates, log=log)
        except (RuntimeError, ValueError, OSError) as exc:
            print(f"{chain_id}: SEARCH FAILED for {start}..{end}: {exc}")
            continue
        if not scenes:
            print(f"{chain_id}: no acquisitions found for {start}..{end}")
            continue
        sampled = 0
        for scene in scenes:
            saved_bands = 0
            for band in cfg.bands:
                path = ws.chip_path(chain_id, cfg.source_dir, band, scene.acquired)
                try:
                    array = source.fetch(scene, aoi, band)
                    if is_blank(array):
                        # No signal: nodata fill, or saturated white by cloud over the site.
                        print(f"{chain_id}: SKIPPED blank {band.upper()} for {scene.acquired}")
                        continue
                    save_png(array, path)
                except (RuntimeError, ValueError, OSError) as exc:
                    print(f"{chain_id}: FAILED TO GET {band.upper()} for {scene.acquired}: {exc}")
                    continue
                written += 1
                saved_bands += 1
                if cfg.verbose:
                    print(f"{chain_id}: wrote {path}")
            sampled += 1 if saved_bands else 0
        # ``-n -1`` asks for one window covering everything, so the number of
        # acquisitions found is the honest denominator there.
        requested = len(scenes) if all_dates else len(windows)
        log(f"wrote {sampled} of {requested} requested samples")
    print(f"Wrote {written} chip(s) under {ws.output_dir}")
    return 0


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
        try:
            cfg = gather_config(args)
            credentials = Credentials.from_env(
                planet_api_key=args.api,
                sh_client_id=args.sh_client_id,
                sh_client_secret=args.sh_client_secret,
                sentinel_provider=args.sentinel_provider,
            )
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        return run_gather(cfg, credentials, ws)

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
