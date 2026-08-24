"""Preflight checks for the optional backend dependencies.

Optional heavy packages (``osmium``, ``pystac_client``, ``sentinelhub``,
``planet``, ``rasterio``) are imported lazily inside the backend that needs
them, so a missing extra would otherwise surface as an ImportError traceback
from deep inside a fetch. These checks run before any credential or network
work happens and raise :class:`ValueError` with an install hint instead; the
CLI turns that into one ``ERROR: ...`` line and exit code 2.
"""

from __future__ import annotations

import importlib.util

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
    """Fail with an install hint when a ``gather`` backend's modules are missing."""
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
    if importlib.util.find_spec("osmium") is None:
        raise ValueError(
            f"the osmium backend needs pyosmium; install it with {install_hint('osmium')}"
        )
    from cssic.history.osmium import require_osmium

    try:
        require_osmium()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
