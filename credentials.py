"""Load Planet and Sentinel Hub credentials from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env")


@dataclass(frozen=True)
class PlanetCredentials:
    api_key: str | None


@dataclass(frozen=True)
class SentinelCredentials:
    client_id: str | None
    client_secret: str | None
    # "cdse" = Copernicus Data Space (free). "sentinelhub" = commercial.
    provider: str = "cdse"


def planet_credentials(explicit_key: str | None = None) -> PlanetCredentials:
    key = (
        explicit_key
        or os.environ.get("PLANET_API_KEY")
        or os.environ.get("PL_API_KEY")
        or os.environ.get("PL_AUTH_API_KEY")
    )
    if key:
        key = key.strip() or None
    return PlanetCredentials(api_key=key)


def sentinel_credentials(
    explicit_id: str | None = None,
    explicit_secret: str | None = None,
    provider: str | None = None,
) -> SentinelCredentials:
    chosen_provider = (provider or os.environ.get("SENTINEL_PROVIDER") or "cdse").lower()
    if chosen_provider not in {"cdse", "sentinelhub"}:
        raise ValueError(f"Unknown Sentinel provider {chosen_provider!r}; use cdse or sentinelhub")
    client_id = explicit_id or os.environ.get("SH_CLIENT_ID")
    client_secret = explicit_secret or os.environ.get("SH_CLIENT_SECRET")
    if client_id:
        client_id = client_id.strip() or None
    if client_secret:
        client_secret = client_secret.strip() or None
    return SentinelCredentials(
        client_id=client_id,
        client_secret=client_secret,
        provider=chosen_provider,
    )
