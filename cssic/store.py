"""On-disk layout for extracted sites and downloaded imagery.

The layout is unchanged from v2 so existing datasets stay valid::

    output/collection/collection.{gpkg,geojson,shp,...}
    output/{chain_id}/info.txt
        start,end,prev_tag,final_tag,minx,miny,maxx,maxy
    output/{chain_id}/images/{planet|sentinel}/{rgb|nir}/{YYYY-MM-DD}.png
    temp/snapshots/            daily snapshots, order logs

Ported from v2 ``workspace.py``, ``extract_sites.create_dataset``/``_save_gdf``,
``gather_images.setup``, ``reset_extract.py`` and ``reset_images.py``. Every
path is relative to a :class:`Workspace` root so tests can use ``tmp_path``.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

#: Collection file stems written by :meth:`Workspace.save_collection`, in the
#: order :meth:`Workspace.collection_path` prefers to read them back.
COLLECTION_FORMATS: tuple[tuple[str, str | None], ...] = (
    ("gpkg", "GPKG"),
    ("geojson", "GeoJSON"),
    ("shp", None),
)
INFO_FILENAME = "info.txt"
INFO_FIELDS = ("start", "end", "prev_tag", "final_tag", "minx", "miny", "maxx", "maxy")


@dataclass(frozen=True)
class SiteInfo:
    """Parsed ``output/{chain_id}/info.txt``."""

    chain_id: str
    start: str
    end: str
    prev_tag: str
    final_tag: str
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True)
class Workspace:
    """Directory layout rooted at ``root`` (default: the current directory)."""

    root: Path = field(default_factory=Path)

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    # -- directories ------------------------------------------------------
    @property
    def temp_dir(self) -> Path:
        return self.root / "temp"

    @property
    def snapshot_dir(self) -> Path:
        return self.temp_dir / "snapshots"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def collection_dir(self) -> Path:
        return self.output_dir / "collection"

    def setup(self) -> None:
        """Create ``temp/snapshots`` and ``output/collection`` if missing."""
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.collection_dir.mkdir(parents=True, exist_ok=True)

    # -- collection -------------------------------------------------------
    def collection_path(self, stem: str = "collection") -> Path | None:
        """Return the newest saved construction collection, if any."""
        for suffix, _driver in COLLECTION_FORMATS:
            candidate = self.collection_dir / f"{stem}.{suffix}"
            if candidate.is_file():
                return candidate
        return None

    def save_collection(self, gdf: Any, stem: str = "collection") -> list[Path]:
        """Write GeoPackage, GeoJSON and Shapefile copies of a collection."""
        self.collection_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for suffix, driver in COLLECTION_FORMATS:
            path = self.collection_dir / f"{stem}.{suffix}"
            if driver is None:
                gdf.to_file(path)
            else:
                gdf.to_file(path, driver=driver)
            written.append(path)
        return written

    def load_collection(self, stem: str = "collection") -> Any | None:
        """Read the saved collection back as a GeoDataFrame, or None."""
        path = self.collection_path(stem)
        if path is None:
            return None
        import geopandas as gpd

        return gpd.read_file(path)

    # -- per-site files ---------------------------------------------------
    def site_dir(self, chain_id: str) -> Path:
        return self.output_dir / str(chain_id)

    def info_path(self, chain_id: str) -> Path:
        return self.site_dir(chain_id) / INFO_FILENAME

    @staticmethod
    def _info_field(value: Any) -> str:
        """One ``info.txt`` field, kept free of the separators v2 used.

        OSM tag values may contain commas and newlines; ``info.txt`` is a single
        comma-separated line, so a raw tag would silently add a field and make
        the file unreadable.
        """
        text = " ".join(str(value).split())
        return text.replace(",", ";")

    def write_info(
        self,
        chain_id: str,
        start: Any,
        end: Any,
        prev_tag: Any,
        final_tag: Any,
        bounds: Iterable[float],
    ) -> Path:
        """Write ``output/{chain_id}/info.txt`` in the v2 comma-separated form."""
        minx, miny, maxx, maxy = (float(v) for v in bounds)
        fields = [self._info_field(value) for value in (start, end, prev_tag, final_tag)]
        path = self.info_path(chain_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            ",".join([*fields, str(minx), str(miny), str(maxx), str(maxy)]),
            encoding="utf-8",
        )
        return path

    def read_info(self, chain_id: str) -> SiteInfo:
        """Parse ``output/{chain_id}/info.txt``."""
        parts = self.info_path(chain_id).read_text(encoding="utf-8").strip().split(",")
        if len(parts) != len(INFO_FIELDS):
            raise ValueError(
                f"{self.info_path(chain_id)} has {len(parts)} fields, expected {len(INFO_FIELDS)}"
            )
        start, end, prev_tag, final_tag = parts[:4]
        bounds = tuple(float(v) for v in parts[4:])
        return SiteInfo(
            chain_id=str(chain_id),
            start=start,
            end=end,
            prev_tag=prev_tag,
            final_tag=final_tag,
            bounds=bounds,  # type: ignore[arg-type]
        )

    def create_dataset(self, gdf: Any) -> int:
        """Write an ``info.txt`` for every chain in ``gdf``; returns the count."""
        if gdf is None:
            return 0
        self.output_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for _, row in gdf.iterrows():
            self.write_info(
                row["chain_id"],
                row["start"],
                row["end"],
                row["prev_tag"],
                row["final_tag"],
                row["geometry"].bounds,
            )
            count += 1
        return count

    # -- imagery ----------------------------------------------------------
    def images_dir(self, chain_id: str, source: str, band: str | None = None) -> Path:
        """``output/{chain_id}/images/{source}[/{band}]``."""
        path = self.site_dir(chain_id) / "images" / source
        return path if band is None else path / band

    def chip_path(self, chain_id: str, source: str, band: str, day: date | str) -> Path:
        """``output/{chain_id}/images/{source}/{band}/{YYYY-MM-DD}.png``."""
        stem = day.isoformat() if isinstance(day, date) else str(day)
        return self.images_dir(chain_id, source, band) / f"{stem}.png"

    def prepare_image_dirs(self, gdf: Any, source: str, bands: Iterable[str]) -> None:
        """Create the band directories for every chain in ``gdf`` (v2 ``gather.setup``)."""
        bands = list(bands)
        for _, row in gdf.iterrows():
            for band in bands:
                self.images_dir(row["chain_id"], source, band).mkdir(parents=True, exist_ok=True)

    # -- reset ------------------------------------------------------------
    def reset_extract(self) -> int:
        """Recreate a clean extract workspace: empty snapshots and collection.

        Returns the number of files removed so the CLI can say what it did.
        """
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        removed = 0
        for snapshot in self.snapshot_dir.glob("*"):
            removed += _remove_path(snapshot)

        for output in self.output_dir.glob("*"):
            if output.name == "collection":
                continue
            removed += _remove_path(output)

        if self.collection_dir.is_dir():
            for item in self.collection_dir.glob("*"):
                removed += _remove_path(item)

        removed += _remove_path(self.root / "outputpoly.osh.pbf")

        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.collection_dir.mkdir(parents=True, exist_ok=True)
        return removed

    def reset_images(self) -> int:
        """Delete downloaded imagery, keeping the extracted sites themselves.

        Returns the number of image files removed (the CLI reports it).
        """
        if not self.output_dir.is_dir():
            return 0
        removed = 0
        for site in self.output_dir.glob("*"):
            if site.name == "collection":
                continue
            for image_dir in site.glob("images/*"):
                removed += _remove_path(image_dir)
        return removed


def _remove_path(path: Path) -> int:
    """Delete ``path``; returns how many files went with it."""
    if path.is_dir():
        count = sum(1 for item in path.rglob("*") if item.is_file() or item.is_symlink())
        shutil.rmtree(path)
        return count
    if path.is_file() or path.is_symlink():
        path.unlink()
        return 1
    return 0
