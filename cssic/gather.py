"""The ``cssic gather`` command: download imagery chips for extracted chains.

The application layer between :mod:`cssic.cli` (argument parsing, dispatch)
and the :class:`~cssic.imagery.base.ImageSource` backends: it loads the saved
collection, samples one scene per requested date window, fetches bands, drops
blank chips, and owns all progress output for the run. One site failing must
never lose the sites after it -- a gather run can take hours.
"""

from __future__ import annotations

from datetime import date

from cssic.config import Credentials, GatherConfig
from cssic.deps import require_backend
from cssic.store import Workspace


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
