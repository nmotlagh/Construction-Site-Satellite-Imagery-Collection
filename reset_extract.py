"""
This module contains a function which will delete all files and directories created in the extract_sites module.
"""

__author__ = """Nicholas Kashani Motlagh @ Ohio State University\n
                Aswathnarayan Radhakrishnan @ Ohio State University\n
                Jim Davis @ Ohio State University (Point of Contact, see __email__)\n
                Roman Ilin @ AFRL/RYAP, Wright-Patterson AFB"""
__email__ = 'davis.1719@osu.edu'
__date__ = "2020-08-05"

from pathlib import Path
import shutil

from workspace import COLLECTION_DIR, OUTPUT_DIR, SNAPSHOT_DIR


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.is_file() or path.is_symlink():
        path.unlink()


def reset() -> None:
    """
    Recreate a clean extract workspace:
    ``temp/snapshots/`` (empty) and ``output/collection/`` (empty).
    Deletes ``outputpoly.osh.pbf`` if present.
    Missing directories are created rather than treated as an error.
    """
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for snapshot in SNAPSHOT_DIR.glob("*"):
        _remove_path(snapshot)

    for output in OUTPUT_DIR.glob("*"):
        if output.name == "collection":
            continue
        _remove_path(output)

    if COLLECTION_DIR.is_dir():
        for item in COLLECTION_DIR.glob("*"):
            _remove_path(item)
    else:
        COLLECTION_DIR.mkdir(parents=True, exist_ok=True)

    poly = Path("outputpoly.osh.pbf")
    if poly.is_file() or poly.is_symlink():
        poly.unlink()

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    COLLECTION_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    reset()
