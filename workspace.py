"""Create and locate working directories used by extract/gather."""

from __future__ import annotations

from pathlib import Path

TEMP_DIR = Path("temp")
SNAPSHOT_DIR = TEMP_DIR / "snapshots"
OUTPUT_DIR = Path("output")
COLLECTION_DIR = OUTPUT_DIR / "collection"


def setup_directory() -> None:
    """Create temp/snapshots and output/collection if they do not exist."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    COLLECTION_DIR.mkdir(parents=True, exist_ok=True)


def collection_path() -> Path | None:
    """Return the newest saved construction collection, if any."""
    for candidate in (
        COLLECTION_DIR / "collection.gpkg",
        COLLECTION_DIR / "collection.geojson",
        COLLECTION_DIR / "collection.shp",
    ):
        if candidate.is_file():
            return candidate
    return None


if __name__ == "__main__":
    setup_directory()
