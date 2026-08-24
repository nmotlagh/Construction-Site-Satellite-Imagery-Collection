"""The ``cssic extract`` command: run the paper algorithm and store the chains.

This is the application layer between :mod:`cssic.cli` (argument parsing,
dispatch) and :mod:`cssic.chains` (the pure algorithm): it picks the history
backend, owns all progress output for the run, and writes results through
:class:`~cssic.store.Workspace`.
"""

from __future__ import annotations

from cssic.config import ExtractConfig
from cssic.deps import require_history_backend
from cssic.store import Workspace


class _SnapshotProgress:
    """History source wrapper that reports a running snapshot count.

    A cold ohsome extract spends ~20 s per daily snapshot, so a two-month window
    is a quarter of an hour during which nothing else prints. Counting here (and
    not in the backend) keeps all output decisions in this layer.
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
