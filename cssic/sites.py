"""Construction chains: OSM sites whose way ids change identity over time.

Port of v2 ``way_chain.py`` (``WayChain``/``WayChainMap``) with the same
semantics and a cleaner API. In the paper's vocabulary a :class:`Site` *is* a
construction chain: one physical site, possibly several OSM way ids, carrying a
start date, an end date, a construction tag, a **previous tag** and a **final
tag** (the two **boundary tags**).

Chain keys look like ``"<id>_<id>_...-<serial>"``: the ids that make up the
chain joined with ``_``, then the chain's creation serial number. The serial
disambiguates chains that end up with the same id string. It is handed out by
the :class:`SiteCollection` the chain is created in, so an extraction run is
reproducible: running the same extraction twice in one process yields the same
chain ids.
"""

from __future__ import annotations

from typing import Any

from cssic.geom import bounds_box, prepare_geom, safe_union

#: Column order of the collection GeoDataFrame / ``collection.gpkg``.
GDF_COLUMNS = ("chain_id", "start", "end", "constr_tag", "prev_tag", "final_tag")


class Site:
    """A construction chain: one site that may be several OSM way ids over time."""

    def __init__(
        self,
        way_id: Any,
        start: Any = None,
        end: Any = None,
        constr_tag: str | None = None,
        prev_tag: str | None = None,
        final_tag: str | None = None,
        geometry: Any = None,
    ) -> None:
        self.ids: list[Any] = [way_id]
        self.start = start
        self.end = end
        self.constr_tag = constr_tag
        self.prev_tag = prev_tag
        self.final_tag = final_tag
        self.geometry = geometry
        #: Creation order within a collection; set by :meth:`SiteCollection.add`.
        self.serial_no = 0

    def __repr__(self) -> str:
        return (
            f"Site(ids={self.ids!r}, start={self.start!r}, end={self.end!r}, "
            f"constr_tag={self.constr_tag!r}, prev_tag={self.prev_tag!r}, "
            f"final_tag={self.final_tag!r})"
        )

    def __str__(self) -> str:
        pad = "\t\t\t" if self.constr_tag == "landuse" else "\t\t"
        return f"{self.start}\t{self.end}\t{self.constr_tag}{pad}{self.prev_tag}\t{self.final_tag}"

    @property
    def chain_id(self) -> str:
        """The key this chain has (or would have) in a :class:`SiteCollection`."""
        return f"{'_'.join(str(i) for i in self.ids)}-{self.serial_no}"

    def update_geometry(self, geometry: Any) -> None:
        """Union ``geometry`` into the chain's footprint."""
        self.geometry = safe_union(self.geometry, geometry)

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        prepared = prepare_geom(self.geometry)
        return None if prepared is None else tuple(prepared.bounds)  # type: ignore[return-value]


class SiteCollection:
    """Construction chains keyed by ``chain_id``."""

    def __init__(self) -> None:
        self.chains: dict[str, Site] = {}
        #: Serials are per-collection, not per-process, so two identical
        #: extraction runs in one process hand out the same chain ids.
        self._serial = 0

    # ``map`` is the v2 attribute name; kept as an alias so ported code reads
    # the same dictionary.
    @property
    def map(self) -> dict[str, Site]:
        return self.chains

    def __len__(self) -> int:
        return len(self.chains)

    def __iter__(self):
        return iter(self.chains)

    def __contains__(self, key: object) -> bool:
        return key in self.chains

    def __getitem__(self, key: str) -> Site:
        return self.chains[key]

    def __setitem__(self, key: str, site: Site) -> None:
        self.chains[key] = site

    def items(self):
        return self.chains.items()

    def values(self):
        return self.chains.values()

    def keys(self):
        return self.chains.keys()

    def __str__(self) -> str:
        header = "chain_id\tstart\t\tend\t\tconstruction_tag\tprevious_tag\tfinal_tag\n"
        body = [f"{key}\t{site}" for key, site in self.chains.items()]
        return header + "\n".join(body)

    def add(self, site: Site, key: str | None = None) -> str:
        """Insert ``site`` under ``key`` (default: the site's own ``chain_id``).

        Without a key the chain is being created here, so it takes this
        collection's next serial number. With one it is an existing chain being
        moved (in-progress -> completed), which keeps the serial it was given.
        """
        if key is None:
            site.serial_no = self._serial
            self._serial += 1
            key = site.chain_id
        self.chains[key] = site
        return key

    def pop(self, key: str) -> Site:
        return self.chains.pop(key)

    def way_ids(self) -> list[Any]:
        """Every OSM way id in the collection, across all chains."""
        ids: list[Any] = []
        for site in self.chains.values():
            ids.extend(site.ids)
        return ids

    def find_key(self, way_id: Any) -> str:
        """Return the newest chain key that contains ``way_id``.

        Matching is by the chain's ID list, not a substring of the key string.
        The original substring check treated way 12 as a match for chain 123.
        """
        needle = str(way_id)
        matches = []
        for chain_key, site in self.chains.items():
            if needle in {str(item) for item in site.ids}:
                matches.append((chain_key, site.serial_no))
        if not matches:
            raise KeyError(f"No construction chain contains way_id {way_id}")
        matches.sort(key=lambda item: item[1])
        return matches[-1][0]

    def has_way(self, way_id: Any) -> bool:
        try:
            self.find_key(way_id)
        except KeyError:
            return False
        return True

    def get(self, way_id: Any) -> Site:
        """Return the chain containing ``way_id``."""
        return self.chains[self.find_key(way_id)]

    def update(
        self,
        way_id: Any,
        start: Any = None,
        end: Any = None,
        constr_tag: str | None = None,
        prev_tag: str | None = None,
        final_tag: str | None = None,
        geometry: Any = None,
    ) -> None:
        """Update the chain containing ``way_id``; ``None`` leaves a field alone."""
        site = self.chains[self.find_key(way_id)]
        if start is not None:
            site.start = start
        if end is not None:
            site.end = end
        if constr_tag is not None:
            site.constr_tag = constr_tag
        if prev_tag is not None:
            site.prev_tag = prev_tag
        if final_tag is not None:
            site.final_tag = final_tag
        if geometry is not None:
            site.update_geometry(geometry)

    def rekey(self, way_id: Any, new_id: Any, is_prev: bool = False) -> str:
        """Re-key the chain containing ``way_id`` to include ``new_id``."""
        old_key = self.find_key(way_id)
        # Strip only the trailing ``-{serial_no}``: ids themselves may contain
        # a dash (ohsome normalises ``way/123`` to ``way-123``).
        bare_old_key = old_key.rsplit("-", 1)[0]
        if is_prev:
            new_key = f"{new_id}_{bare_old_key}"
        else:
            new_key = f"{bare_old_key}_{new_id}"
        new_key = f"{new_key}-{self.chains[old_key].serial_no}"
        self.chains[new_key] = self.chains[old_key]
        self.chains.pop(old_key)
        return new_key

    def link(self, id_in_chain: Any, new_id: Any, is_prev: bool = False) -> str:
        """Extend the chain holding ``id_in_chain`` with ``new_id``.

        ``is_prev`` extends backwards in time (the new id goes to the front of
        the id list), otherwise forwards.
        """
        new_key = self.rekey(id_in_chain, new_id, is_prev=is_prev)
        # Look up by the id already on the chain: the new id is not in
        # site.ids until we insert/append it here.
        site = self.chains[new_key]
        if is_prev:
            site.ids.insert(0, new_id)
        else:
            site.ids.append(new_id)
        return new_key

    def to_gdf(self):
        """Return a GeoDataFrame of chains (bounding boxes), or None if empty."""
        import geopandas as gpd

        rows = []
        geometries = []
        for key, site in self.chains.items():
            rows.append(
                [
                    key,
                    str(site.start),
                    str(site.end),
                    str(site.constr_tag),
                    str(site.prev_tag),
                    str(site.final_tag),
                ]
            )
            geometries.append(bounds_box(site.geometry))
        gdf = gpd.GeoDataFrame(
            rows, columns=list(GDF_COLUMNS), geometry=geometries, crs="EPSG:4326"
        )
        gdf = gdf.sort_values(by=["start", "end"]).reset_index(drop=True)
        if len(gdf.index) == 0:
            return None
        return gdf
