"""Construction-chain data structures for OSM ways that change identity over time."""

from __future__ import annotations

from typing import Any

_GEOM_ERRORS: tuple[type[BaseException], ...] = (ValueError, TypeError, AttributeError)


def _geom_errors() -> tuple[type[BaseException], ...]:
    """Return shapely topology errors plus built-in failures, if shapely is present."""
    try:
        from shapely.errors import GEOSException
    except ImportError:
        return _GEOM_ERRORS
    return _GEOM_ERRORS + (GEOSException,)


def _prepare_geom(geom: Any) -> Any | None:
    """Return a valid, non-empty geometry, or None if it cannot be used."""
    if geom is None:
        return None
    try:
        from shapely import make_valid
    except ImportError:
        try:
            from shapely.validation import make_valid
        except ImportError:
            make_valid = None
    try:
        if geom.is_empty:
            return None
        if make_valid is not None and not geom.is_valid:
            geom = make_valid(geom)
        if geom is None or geom.is_empty:
            return None
        return geom
    except _geom_errors():
        return None


def _safe_union(left: Any, right: Any) -> Any | None:
    """Union two geometries, skipping empty/invalid inputs (shapely 2-safe)."""
    left = _prepare_geom(left)
    right = _prepare_geom(right)
    if left is None:
        return right
    if right is None:
        return left
    try:
        try:
            from shapely import union_all
        except ImportError:
            from shapely.ops import unary_union as union_all
        merged = union_all([left, right])
        prepared = _prepare_geom(merged)
        return prepared if prepared is not None else left
    except _geom_errors():
        return left


def _intersection_over_union(left: Any, right: Any) -> float:
    """IOU of two geometries; 0.0 when union area is 0 or GEOS fails."""
    left = _prepare_geom(left)
    right = _prepare_geom(right)
    if left is None or right is None:
        return 0.0
    try:
        try:
            from shapely import union_all
        except ImportError:
            from shapely.ops import unary_union as union_all
        if not left.intersects(right):
            return 0.0
        inter_area = left.intersection(right).area
        union_geom = union_all([left, right])
        union_area = 0.0 if union_geom is None or union_geom.is_empty else union_geom.area
        if union_area <= 0.0:
            return 0.0
        return inter_area / union_area
    except _geom_errors() + (ZeroDivisionError,):
        return 0.0


class WayChain:
    """A construction chain: one OSM site that may be represented by several way IDs."""

    serial_no = 0

    def __init__(self, way_id, start, end, constr_type, prev, post, geo):
        self.ids = [way_id]
        self.start = start
        self.end = end
        self.construction_type = constr_type
        self.boundary_previous = prev
        self.boundary_post = post
        self.geometry = geo
        self.serial_no = WayChain.serial_no
        WayChain.serial_no += 1

    def __str__(self) -> str:
        pad = "\t\t\t" if self.construction_type == "landuse" else "\t\t"
        return (
            f"{self.start}\t{self.end}\t{self.construction_type}{pad}"
            f"{self.boundary_previous}\t{self.boundary_post}"
        )

    def update_geometry(self, geo) -> None:
        self.geometry = _safe_union(self.geometry, geo)

    @staticmethod
    def get_next_serial() -> int:
        current_serial = WayChain.serial_no
        WayChain.serial_no += 1
        return current_serial


class WayChainMap:
    """A collection of WayChains keyed by chain_id."""

    def __init__(self):
        self.map = {}

    def __str__(self) -> str:
        header = "chain_id\tstart\t\tend\t\tconstruction_tag\tprevious_tag\tfinal_tag\n"
        body = [f"{k}\t" + str(self.map[k]) for k in self.map]
        return header + "\n".join(body)

    def get_map_ways(self) -> list:
        ids = []
        for chain in self.map.values():
            ids.extend(chain.ids)
        return ids

    def find_key(self, way_id) -> str:
        """Return the newest chain key that contains ``way_id``.

        Matching is by the chain's ID list, not a substring of the key string.
        The original substring check treated way 12 as a match for chain 123.
        """
        needle = str(way_id)
        matches = []
        for chain_key, chain in self.map.items():
            id_set = {str(item) for item in chain.ids}
            if needle in id_set:
                matches.append((chain_key, chain.serial_no))
        if not matches:
            raise KeyError(f"No way chain contains way_id {way_id}")
        matches.sort(key=lambda item: item[1])
        return matches[-1][0]

    def update_map(
        self,
        way_id,
        start=None,
        end=None,
        construction_type=None,
        prev=None,
        post=None,
        geo=None,
    ) -> None:
        key = self.find_key(way_id)
        chain = self.map[key]
        if start is not None:
            chain.start = start
        if end is not None:
            chain.end = end
        if construction_type is not None:
            chain.construction_type = construction_type
        if prev is not None:
            chain.boundary_previous = prev
        if post is not None:
            chain.boundary_post = post
        if geo is not None:
            chain.update_geometry(geo)

    def update_chain_key(self, way_id, new_id, is_prev: bool = False) -> None:
        old_key = self.find_key(way_id)
        bare_old_key = old_key.split("-")[0]
        if is_prev:
            new_key = f"{new_id}_{bare_old_key}"
        else:
            new_key = f"{bare_old_key}_{new_id}"
        new_key = f"{new_key}-{self.map[old_key].serial_no}"
        self.map[new_key] = self.map[old_key]
        self.map.pop(old_key)

    def increment_chain(self, id_in_chain, new_id, is_prev: bool = False) -> None:
        self.update_chain_key(id_in_chain, new_id, is_prev=is_prev)
        # Look up by the id already on the chain. The new id is not in
        # chain.ids until we insert/append it (find_key no longer matches
        # the key string, so find_key(new_id) would miss here).
        chain = self.map[self.find_key(id_in_chain)]
        if is_prev:
            chain.ids.insert(0, new_id)
        else:
            chain.ids.append(new_id)

    def get_way_chain(self, way_id) -> WayChain:
        return self.map[self.find_key(way_id)]

    def get_gdf(self):
        import geopandas as gpd
        from shapely.geometry import Polygon, box

        def _bounds_box(geom):
            prepared = _prepare_geom(geom)
            if prepared is None:
                return Polygon()
            minx, miny, maxx, maxy = prepared.bounds
            if any(v != v for v in (minx, miny, maxx, maxy)):
                return Polygon()
            return box(minx, miny, maxx, maxy)

        def _generate_row(key):
            row = [
                key,
                str(self.map[key].start),
                str(self.map[key].end),
                str(self.map[key].construction_type),
                str(self.map[key].boundary_previous),
                str(self.map[key].boundary_post),
            ]
            return row, _bounds_box(self.map[key].geometry)

        data_col_names = ["chain_id", "start", "end", "constr_tag", "prev_tag", "final_tag"]
        rows = []
        geometries = []
        for key in self.map:
            row, geometry = _generate_row(key)
            rows.append(row)
            geometries.append(geometry)
        gdf = gpd.GeoDataFrame(rows, columns=data_col_names, geometry=geometries, crs="EPSG:4326")
        gdf = gdf.sort_values(by=["start", "end"]).reset_index(drop=True)
        if len(gdf.index) == 0:
            return None
        return gdf
