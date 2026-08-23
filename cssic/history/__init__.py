"""OSM history backends behind the :class:`~cssic.history.base.HistorySource` protocol.

The backends themselves are imported lazily (``osmium`` is optional and the
ohsome backend needs ``requests``), so ``import cssic.history`` is cheap.
"""

from __future__ import annotations

from cssic.history.base import CONSTRUCTION_TAGS, BBox, DateWindow, HistorySource, Interval

__all__ = [
    "CONSTRUCTION_TAGS",
    "BBox",
    "DateWindow",
    "HistorySource",
    "Interval",
    "get_history_source",
]


def get_history_source(backend: str, **kwargs):
    """Construct the named backend (``"ohsome"`` or ``"osmium"``)."""
    if backend == "ohsome":
        from cssic.history.ohsome import OhsomeHistory

        return OhsomeHistory(**kwargs)
    if backend == "osmium":
        from cssic.history.osmium import OsmiumHistory

        return OsmiumHistory(**kwargs)
    raise ValueError(f"unknown history backend {backend!r}; use 'ohsome' or 'osmium'")
