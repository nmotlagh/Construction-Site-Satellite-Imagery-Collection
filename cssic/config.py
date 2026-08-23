"""Frozen configuration objects. All validation for the package lives here.

Ported from the v2 ``extract_sites.set_params``/``get_input`` and
``gather_images.check_*`` helpers, with two differences:

* nothing is stored in module-level mutable state; and
* invalid input raises :class:`ValueError` instead of printing and returning
  ``None`` (the CLI turns that into an ``ERROR: ...`` message and exit code 2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

# Sentinel-2 imagery is not available before this date, so extraction windows
# may not start earlier (v2: extract_sites.check_start_date).
EARLIEST_START = date(2015, 6, 22)
# OSM history dumps and the ohsome API lag reality; v2 required the end date to
# be at least this many days in the past.
END_DATE_LAG_DAYS = 10

BACKENDS = ("ohsome", "osmium")
SOURCES = ("planet", "sentinel", "stac")
#: v2 accepted abbreviations on the command line.
SOURCE_ALIASES = {
    "p": "planet",
    "planet": "planet",
    "s": "sentinel",
    "sentinel": "sentinel",
    "stac": "stac",
}
SENTINEL_PROVIDERS = ("cdse", "sentinelhub")

#: IOU above which a finished site immediately re-tagged construction is
#: treated as the same construction chain (paper: chain linking).
DEFAULT_CHAIN_CONFIDENCE = 0.5
#: IOU above which an overlapping tagged way is accepted as a boundary tag;
#: weaker overlaps become ``NO TAG FOUND``.
DEFAULT_TAG_CONFIDENCE = 0.5
#: Boundary tag written when no candidate clears the confidence threshold.
NO_TAG = "NO TAG FOUND"


def parse_iso_date(value: str | date | datetime) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string, or pass through a ``datetime.date``."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"date must be an isoformat string or datetime.date, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"date must be in isoformat yyyy-mm-dd, got {value!r}") from exc


def _existing_file(value: str | os.PathLike[str], label: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"{label} file {path} does not exist")
    return path


@dataclass(frozen=True)
class ExtractConfig:
    """Everything ``cssic extract`` needs. Validated on construction."""

    start: date
    end: date
    poly: Path
    region: Path | None = None
    backend: str = "ohsome"
    keep_temp: bool = False
    restrict_window: bool = False
    save_wip: bool = False
    #: IOU thresholds from the paper (see module constants).
    construction_chain_confidence: float = DEFAULT_CHAIN_CONFIDENCE
    tag_confidence: float = DEFAULT_TAG_CONFIDENCE
    #: ``-v``: also echo the day-by-day walk. Mutually exclusive with ``quiet``.
    verbose: bool = False
    #: ``-q``: print nothing but the final summary.
    quiet: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", parse_iso_date(self.start))
        object.__setattr__(self, "end", parse_iso_date(self.end))
        if self.start < EARLIEST_START:
            raise ValueError(f"start date must be on or after {EARLIEST_START}")
        if self.end <= self.start:
            raise ValueError("end date must be after start date")
        cutoff = date.today() - timedelta(days=END_DATE_LAG_DAYS)
        if self.end >= cutoff:
            raise ValueError(f"end date must be before {cutoff} ({END_DATE_LAG_DAYS} days ago)")

        if self.backend not in BACKENDS:
            raise ValueError(f"backend must be one of {BACKENDS}, got {self.backend!r}")
        object.__setattr__(self, "poly", _existing_file(self.poly, "poly"))
        if self.region is not None:
            object.__setattr__(self, "region", _existing_file(self.region, "region"))
        elif self.backend == "osmium":
            raise ValueError("--region (an .osh.pbf history file) is required with backend osmium")

        for name in ("construction_chain_confidence", "tag_confidence"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1, got {value}")
            object.__setattr__(self, name, value)

        for name in ("keep_temp", "restrict_window", "save_wip", "verbose", "quiet"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise ValueError(f"{name} is not a boolean")
        if self.verbose and self.quiet:
            raise ValueError("choose at most one of verbose / quiet")

    @property
    def window(self) -> tuple[date, date]:
        """The requested ``(start, end)`` date window."""
        return (self.start, self.end)

    @property
    def verbosity(self) -> int:
        """0 with ``-q``, 2 with ``-v``, 1 by default (see :mod:`cssic.cli`)."""
        if self.quiet:
            return 0
        return 2 if self.verbose else 1


@dataclass(frozen=True)
class GatherConfig:
    """Everything ``cssic gather`` needs. Validated on construction."""

    source: str
    num_images: int = 3
    padding: float = 1.0
    rgb: bool = False
    nir: bool = False
    verbose: bool = False
    email: bool = False
    download_planet: bool = False
    #: Days added after each sampled date when searching for an acquisition.
    day_padding: int = 6
    #: Discard acquisitions cloudier than this percentage (None = keep all).
    max_cloud_cover: float | None = None

    def __post_init__(self) -> None:
        source = SOURCE_ALIASES.get(str(self.source).strip().lower())
        if source is None:
            raise ValueError(
                f"{self.source!r} is not a valid source; "
                "choose 's'/'sentinel', 'stac', or 'p'/'planet'"
            )
        object.__setattr__(self, "source", source)

        try:
            num_images = int(self.num_images)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"num-images must be an int, got {self.num_images!r}") from exc
        if num_images != -1 and num_images < 1:
            raise ValueError(f"num-images must be >= 1 or -1, got {num_images}")
        object.__setattr__(self, "num_images", num_images)

        try:
            padding = float(self.padding)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"padding must be a number, got {self.padding!r}") from exc
        if padding <= 0:
            raise ValueError(f"padding must be > 0, got {padding}")
        object.__setattr__(self, "padding", padding)

        try:
            day_padding = int(self.day_padding)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"day-padding must be an int, got {self.day_padding!r}") from exc
        if day_padding < 0:
            raise ValueError(f"day-padding must be >= 0, got {day_padding}")
        object.__setattr__(self, "day_padding", day_padding)

        if self.max_cloud_cover is not None:
            try:
                max_cloud = float(self.max_cloud_cover)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"max-cloud must be a number, got {self.max_cloud_cover!r}"
                ) from exc
            if not 0.0 <= max_cloud <= 100.0:
                raise ValueError(f"max-cloud must be between 0 and 100, got {max_cloud}")
            object.__setattr__(self, "max_cloud_cover", max_cloud)

        if not self.rgb and not self.nir:
            raise ValueError("select at least one of --rgb / --nir")

    @property
    def bands(self) -> tuple[str, ...]:
        """The requested bands, in ``rgb``-then-``nir`` order."""
        bands = []
        if self.rgb:
            bands.append("rgb")
        if self.nir:
            bands.append("nir")
        return tuple(bands)

    @property
    def source_dir(self) -> str:
        """Directory name imagery is stored under: ``planet`` or ``sentinel``."""
        return "planet" if self.source == "planet" else "sentinel"


@dataclass(frozen=True)
class Credentials:
    """API credentials, normally read from the environment / ``.env``.

    Ported from v2 ``credentials.py``. Nothing here touches the network; the
    backends decide what they need and fail with a clear message if it is
    missing.
    """

    planet_api_key: str | None = None
    sh_client_id: str | None = None
    sh_client_secret: str | None = None
    sentinel_provider: str = "cdse"
    #: Extra values a backend may want (kept out of the way of the common ones).
    extra: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("planet_api_key", "sh_client_id", "sh_client_secret"):
            value = getattr(self, name)
            if value is not None:
                value = str(value).strip() or None
            object.__setattr__(self, name, value)
        provider = str(self.sentinel_provider or "cdse").lower()
        if provider not in SENTINEL_PROVIDERS:
            raise ValueError(
                f"unknown Sentinel provider {provider!r}; use {' or '.join(SENTINEL_PROVIDERS)}"
            )
        object.__setattr__(self, "sentinel_provider", provider)

    @classmethod
    def from_env(
        cls,
        planet_api_key: str | None = None,
        sh_client_id: str | None = None,
        sh_client_secret: str | None = None,
        sentinel_provider: str | None = None,
        env: dict[str, str] | None = None,
        load_dotenv_file: bool = True,
    ) -> Credentials:
        """Build credentials from explicit values, falling back to the environment.

        Reads ``.env`` in the current working directory when ``python-dotenv``
        is installed (v2 behaviour) unless ``load_dotenv_file`` is False or an
        explicit ``env`` mapping is given.
        """
        if env is None:
            if load_dotenv_file:
                try:
                    from dotenv import load_dotenv
                except ImportError:
                    pass
                else:
                    load_dotenv(Path.cwd() / ".env")
            env = dict(os.environ)
        return cls(
            planet_api_key=(
                planet_api_key
                or env.get("PLANET_API_KEY")
                or env.get("PL_API_KEY")
                or env.get("PL_AUTH_API_KEY")
            ),
            sh_client_id=sh_client_id or env.get("SH_CLIENT_ID"),
            sh_client_secret=sh_client_secret or env.get("SH_CLIENT_SECRET"),
            sentinel_provider=sentinel_provider or env.get("SENTINEL_PROVIDER") or "cdse",
        )

    @property
    def has_sentinel_hub(self) -> bool:
        return bool(self.sh_client_id and self.sh_client_secret)
