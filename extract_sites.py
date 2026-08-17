"""
The purpose of this module is to extract construction sites and their characteristics from a large region of interest.
First, a start date, end date, polygon file, and an osh file containing the polygon are fed as input.
The module will use the osmhandler module to extract all ways which underwent construction in the time window.
WayChainMap and WayChain objects are used from the way_chain module to record the changes observed across time in the
given polygon. Two WayChainMap objects for the completed and in-progress zones will contain information for each
construction site's start date, end date, construction type, previous tag and final tag (should they exist).
This module also contains functions to convert a WayChainMap to a geopandas data frame. To observe construction changes,
this module will also generate daily shape files of construction sites in a region of interest.
"""

from __future__ import annotations

__author__ = """Nicholas Kashani Motlagh @ Ohio State University\n
                Aswathnarayan Radhakrishnan @ Ohio State University\n
                Jim Davis @ Ohio State University (Point of Contact, see __email__)\n
                Roman Ilin @ AFRL/RYAP, Wright-Patterson AFB"""
__email__ = 'davis.1719@osu.edu'
__date__ = "2020-08-05"

# Built-in/Generic Imports
from datetime import date, datetime, timedelta
from typing import Any
import shutil
import subprocess
import sys
import os

# Own modules
import way_chain
from parsers import EXTRACT_FIELDS, build_extract_parser, namespace_to_dict
from workspace import (
    COLLECTION_DIR,
    OUTPUT_DIR,
    SNAPSHOT_DIR,
    TEMP_DIR,
    setup_directory,
)

# Notebook / set_params store. CLI and typed callers pass a dict instead.
params = {
    "start": None,
    "end": None,
    "poly": None,
    "region": None,
    "backend": "ohsome",
    "keep-temp": False,
    "restrict-window": False,
    "save-wip": False,
}

# File paths that are used in the script
temp_dir = TEMP_DIR
snapshot_dir = SNAPSHOT_DIR
ids_filtered_path = snapshot_dir / "out.osh.pbf"
tag_filtered_path = snapshot_dir / "filtered.osh.pbf"
output_dir = OUTPUT_DIR
collection_dir = COLLECTION_DIR


def require_osmium() -> None:
    if shutil.which("osmium") is None:
        sys.exit(
            "osmium-tool is not on PATH. Install it first "
            "(Debian/Ubuntu: sudo apt install osmium-tool; macOS: brew install osmium-tool)."
        )


def run_osmium(*args: str) -> None:
    cmd = ["osmium", *args]
    if "-o" in args and "--overwrite" not in args:
        cmd.append("--overwrite")
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"{' '.join(cmd)} failed ({result.returncode}):\n{detail}")


def _osm_handler():
    """Lazy-import OSMHandler so CLI/date tests do not require pyosmium."""
    from osmhandler import OSMHandler

    return OSMHandler()


def _parse_iso_date(value: str | date | datetime | None) -> date:
    """Parse an ISO date string or pass through a datetime.date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError("date must be an isoformat string or datetime.date")
    return date.fromisoformat(value)


def _extract_cfg(p: dict | None) -> dict:
    return params if p is None else p


def set_params(p: dict) -> None:
    """
    Sets the module parameters to p. p is checked for correct start and end dates first.
    Start is validated before end so dict insertion order cannot skip start conversion.
    :param p: The dictionary used to assign params.
    :return: None
    """
    global params
    # Initialize params each time
    params = {
        "start": None,
        "end": None,
        "poly": None,
        "region": None,
        "backend": "ohsome",
        "keep-temp": False,
        "restrict-window": False,
        "save-wip": False,
    }

    if "start" in p:
        p["start"] = check_start_date(p["start"])
    if "end" in p:
        p["end"] = check_end_date(p["end"], p.get("start"))
    for k in p.keys():
        if k in ("start", "end"):
            continue
        if k in ("poly", "region"):
            if not p[k]:
                p[k] = None
                continue
            p[k] = check_file_path(p[k])
        elif k in ("restrict-window", "save-wip", "keep-temp"):
            p[k] = check_boolean(k, p[k])
        elif k == "backend":
            if p[k] not in ("ohsome", "osmium"):
                print(f"ERROR: backend must be ohsome or osmium, got {p[k]}")
                p[k] = None
    backend = p.get("backend", params["backend"])
    # Check for a missing param. ohsome does not need a history dump.
    for k in p.keys():
        if p[k] is None:
            if k == "region" and backend == "ohsome":
                params[k] = None
                continue
            return
        params[k] = p[k]


def check_start_date(start: str | date | datetime | None) -> date | None:
    """
    Checks that the user entered a valid start date on or after 2015-06-22.
    :param start: Start date entered by user (isoformat string or datetime.date)
    :return: A datetime.date object representing the start date
    """
    try:
        start_date = _parse_iso_date(start)
    except (ValueError, TypeError):
        print("ERROR: Start date must be in isoformat yyyy-mm-dd!")
        return None
    if start_date < date(day=22, month=6, year=2015):
        print("ERROR: Start date must be on or after 2015-06-22")
        return None
    return start_date


def check_end_date(end: str | date | datetime | None, start_date: date | None) -> date | None:
    """
    Checks that the user entered a valid end date which falls after start date and 10 days before today
    :param end: User end date in string format yyyy-mm-dd or a datetime.date
    :param start_date: User start date as datetime.date object
    :return: A datetime.date object representing the end date
    """
    if start_date is None:
        print("ERROR: Enter start date before end date.")
        return None
    try:
        start_date = _parse_iso_date(start_date)
    except (ValueError, TypeError):
        print("ERROR: Enter start date before end date.")
        return None
    try:
        end_date = _parse_iso_date(end)
    except (ValueError, TypeError):
        print("ERROR: End date must be in isoformat yyyy-mm-dd!")
        return None
    if end_date <= start_date:
        print("ERROR: End date must be after start date!")
        return None
    if end_date >= date.today() - timedelta(days=10):
        print(f"ERROR: End date must be before {str(date.today() - timedelta(days=10))} (10 days ago)!")
        return None
    return end_date


def check_file_path(filepath: str | os.PathLike | None) -> str | None:
    """
    Check that filepath exists.
    :param filepath: Some filepath
    :return: Filepath or None if it does not exist
    """
    if filepath is None or not os.path.isfile(filepath):
        print(f"ERROR: File {filepath} does not exist!")
        return None
    return str(filepath)


def check_boolean(name: str, arg: Any) -> bool | None:
    """
    Check that the argument named name is a boolean.
    :param name: Name of the argument passed
    :param arg: Value of the argument
    :return: arg or None if arg is not a boolean
    """
    if not type(arg) == bool:
        print(f"ERROR: {name} is not a boolean!")
        return None
    return arg


def params_from_args(args) -> dict:
    """Validate an argparse namespace (cssic extract or extract_sites.py) into a params dict."""
    raw = namespace_to_dict(args, EXTRACT_FIELDS)
    start = check_start_date(raw["start"])
    end = check_end_date(raw["end"], start)
    poly = check_file_path(raw["poly"])
    backend = raw["backend"]
    region = None
    if backend == "osmium":
        if not raw["region"]:
            print("ERROR: --region is required with --backend osmium")
            sys.exit(2)
        region = check_file_path(raw["region"])
    elif raw["region"]:
        region = check_file_path(raw["region"])
    for value in (start, end, poly):
        if value is None:
            sys.exit(2)
    if backend == "osmium" and region is None:
        sys.exit(2)

    return {
        "start": start,
        "end": end,
        "poly": poly,
        "region": region,
        "backend": backend,
        "keep-temp": bool(raw["keep-temp"]),
        "restrict-window": bool(raw["restrict-window"]),
        "save-wip": bool(raw["save-wip"]),
    }


def get_input(argv: list[str] | None = None) -> dict:
    """Parse command-line arguments for site extraction (python extract_sites.py)."""
    return params_from_args(build_extract_parser().parse_args(argv))


def fill_start(uc, cfg: dict):
    """
    A function which travels backwards in time to fill information and build the way chain map. No new construction
    sites are added to the map.

    :param uc: A way chain map that has under construction sites
    :param cfg: Extract params (start date).
    :return: A way chain map with complete start and end dates.
    """
    print("Finding all start dates")
    # get oldest construction site.
    # There are no chains yet after initialization
    ids_need_start = [k for k in uc.get_map_ways()]
    # Single day decrement
    delta_single = timedelta(days=1)
    # Day before start date
    current_date = cfg["start"] - delta_single
    # We can finish when all start dates are found
    while len(ids_need_start) > 0 and current_date > date(year=2010, month=1, day=1):
        print(f"[Finding Start] {str(current_date)}")
        # generate necessary snapshots
        generate_construction_snapshot(current_date)
        # update uc with construction sites that started today
        fill_start_helper(ids_need_start, uc, current_date)
        # Go to next day
        current_date = current_date - delta_single
    return uc


def fill_start_helper(ids_need_start, uc, current_date):
    """
    A function which adds start dates and previous boundary tags to way_chains in uc that began on current_date.
    This function should be called while moving backwards through time dates.

    :param ids_need_start: A list of way ids which need a start tag and date
    :param uc: A way chain map with under construction sites
    :param current_date: The date to search for changes
    :return: None
    """
    # get all ids of ways from today
    construction_f_handler = _osm_handler()
    daily_filtered_path = str(snapshot_dir / f"filter_file_{str(current_date)}_only_construction.osm")
    construction_f_handler.apply_file(daily_filtered_path)

    # list of ways which have started construction today
    ways_start_found = []

    # Check through each id that needs a start date
    for way_id in ids_need_start:
        # since w_id still is in the list, we know that the construction site existed the day after today
        # (working backwards)
        uc.update_map(way_id, start=current_date+timedelta(days=1))

        # if the id was not found in construction_f_handler during file checking,
        # then it must have been created the day after today
        if way_id not in construction_f_handler.osm_data:
            # add to list of ids that started day after today
            ways_start_found.append(way_id)
        else:
            # update geometry to include today's geometry
            uc.update_map(way_id, geo=construction_f_handler.geometry[way_id])

    # we can remove way_id from ids_need_start
    # if id is part of a chain, that will be found in generate_boundary_tags
    for way_id in ways_start_found:
        ids_need_start.remove(way_id)

    # uc will be updated with new chain keys
    # incomplete_ways will contain int keys of ways added to the chain
    complete_ways, incomplete_ways = generate_boundary_tags(current_date, ways_start_found, uc, mode=0)

    # we need to update ids_need_start to hold ids to look for
    for k in incomplete_ways:
        ids_need_start.append(k)


def fill_end(uc, completed, cfg: dict):
    """A function which travels forward in time to fill information and build the way chain map. No new construction
    sites are added to the map.

    :param uc: A way chain map that has under construction sites.
    :param completed: A way chain map that has completed sites.
    :param cfg: Extract params (end date).
    :return: 2 updated way chain maps containing under construction sites and completed sites
    """
    print("Finding all end dates")
    # get last ids in chains
    ids_need_end = [int(k.split("-")[0].split("_")[-1]) for k in uc.map.keys()]
    # Single day decrement
    delta_single = timedelta(days=1)
    # Day before start date
    current_date = cfg["end"] + delta_single
    # We can finish when all start dates are found. Stop checking dates after today
    while len(ids_need_end) > 0 and current_date < date.today()-timedelta(days=7):
        print(f"[Finding End] {str(current_date)}")
        # Get a filter file of only construction ways
        generate_construction_snapshot(current_date)
        # fill in end tags and dates
        fill_end_helper(ids_need_end, uc, completed, current_date)
        current_date = current_date + delta_single
    return uc, completed


def fill_end_helper(ids_need_end, uc, completed, current_date):
    """
    This function will update WayChainMaps uc and completed to reflect changes in construction. If a site is completed,
    it will be added to completed way chain map.
    :param ids_need_end: A list of ids that need an end date
    :param uc: The WayChainMap of sites under construction
    :param completed: The WayChainMap of completed sites
    :param current_date: The date to update changes in construction
    :return: A list of ways whose construction was completed on current_date
    """
    # Filename of filtered construction of today
    daily_filtered_path = str(snapshot_dir / f"filter_file_{str(current_date)}_only_construction.osm")
    # get ways under construction for next day
    boundary_construction_handler = _osm_handler()
    boundary_construction_handler.apply_file(daily_filtered_path)
    # A list of ways whose ends may have been found
    ways_end_found = []

    # Check all sites that need an end
    for w_id in ids_need_end:
        # Update the end date
        uc.update_map(w_id, end=current_date - timedelta(days=1))
        # Check if the site was observed today
        if w_id not in boundary_construction_handler.osm_data:
            # Add it to completed candidates if not observed
            ways_end_found.append(w_id)

    # Remove all ways found from ids
    for c_way in ways_end_found:
        ids_need_end.remove(c_way)

    # Get post construction tags and any ways which were not complete
    complete_ways, incomplete_ways = generate_boundary_tags(current_date, ways_end_found, uc, mode=2)

    # update ids_need_end with ways that are a part of a chain
    for i_way in incomplete_ways:
        ids_need_end.append(i_way)

    # Transfer complete way chains from uc to completed
    for c_way in complete_ways:
        completed.map[uc.find_key(c_way)] = uc.map[uc.find_key(c_way)]
        uc.map.pop(uc.find_key(c_way))


def add_new_sites(uc, current_date):
    """Updates a WayChainMap uc to include ways that were constructed on current date

    :param uc: A WayChainMap of places under construction
    :param current_date: The date to check for new construction sites
    :return: None
    """
    # File name of only construction sites
    daily_filtered_file = str(snapshot_dir / f"filter_file_{str(current_date)}_only_construction.osm")
    construction_f_handler = _osm_handler()
    construction_f_handler.apply_file(daily_filtered_file)
    # A list of new sites which were found today
    new_sites = []
    # check new construction sites from today
    for way_id in construction_f_handler.osm_data:
        # check that the key is not already under construction
        if way_id not in uc.get_map_ways():
            # create a new WayChain
            new_chain = way_chain.WayChain(way_id, current_date, current_date, construction_f_handler.tags[way_id],
                                           "N/A", "N/A", construction_f_handler.geometry[way_id])
            # add the new WayChain with the serial no to the map
            uc.map[str(way_id)+"-"+str(new_chain.serial_no)] = new_chain
            # add to list of new found sites
            new_sites.append(way_id)
    # get previous tags of newly added sites
    complete_ways, incomplete_ways = generate_boundary_tags(current_date, new_sites, uc, mode=1)
    # get date before found
    prev_date = current_date - timedelta(days=1)
    # get start information of all ways that were just found
    while len(incomplete_ways) > 0:
        # get start information
        fill_start_helper(incomplete_ways, uc, prev_date)
        prev_date = prev_date - timedelta(days=1)


def generate_boundary_tags(current_date, transfer_ways, sites, mode=0):
    """A function which will try to all way chains in sites that contain ways in transfer_ways. If a boundary tag is
    another construction site, the chain will be incremented.

    :param current_date: The current date. Depending on mode, the current date may be a construction date or a boundary
        date.
    :param transfer_ways: A list of way_ids which have completed construction.
    :param sites: A way chain map of sites under construction.
    :param mode: What kind of boundary tag must be created
        0-The tag before construction is needed (came from fill start helper)
        1-The tag before construction is needed (came from add new sites)
        2-The tag after construction is needed
    :return: A list of ways which were completed and a list of ways which are still part of a way chain under
    construction. All ways returned in these lists come from transfer_ways.
    """
    # Check that there are ways which need a boundary tag
    if len(transfer_ways) == 0:
        return [], []
    # Called from fill start helper so sites are not present at current date
    if mode == 0:
        construction_date = str(current_date + timedelta(days=1))
        boundary_date = str(current_date)
    # Came from add new sites so sites are present at current date
    elif mode == 1:
        construction_date = str(current_date)
        boundary_date = str(current_date - timedelta(days=1))
    # End tags are needed
    else:
        construction_date = str(current_date - timedelta(days=1))
        boundary_date = str(current_date)
    # create lists for ways that are finished with construction
    incomplete_ways = []
    complete_ways = []
    for way in transfer_ways:
        # get a sub-region out of the original output
        # minx, miny, maxx, maxy
        geom = sites.map[sites.find_key(way)].geometry
        prepared = way_chain._prepare_geom(geom)
        if prepared is None:
            complete_ways.append(way)
            continue
        way_region = prepared.bounds
        if any(coord != coord for coord in way_region):
            complete_ways.append(way)
            continue
        # padding to include ways around the way-region
        padding = 0.001
        # string in long1,lat1,long2,lat2 s.t. long1,lat1 and long2,lat2 are on opposite corners
        way_region_string = f"{way_region[0] - padding},{way_region[1] - padding},{way_region[2] + padding}," \
                            f"{way_region[3] + padding}"
        # Create filenames for construction, extraction, and boundary
        construction_f = str(snapshot_dir / f"{construction_date}-candid.osm")
        boundary_f = str(snapshot_dir / f"{boundary_date}-{str(sites.get_way_chain(way).serial_no)}-boundary.osm")
        extract_f = str(snapshot_dir / f"{boundary_date}-{str(sites.get_way_chain(way).serial_no)}-extract.osh.pbf")
        # Get the smaller sub-region
        run_osmium(
            "extract",
            "-b",
            way_region_string,
            "outputpoly.osh.pbf",
            "-o",
            extract_f,
            "--with-history",
            "--overwrite",
        )
        run_osmium(
            "time-filter",
            extract_f,
            f"{boundary_date}T00:00:00Z",
            "-o",
            boundary_f,
            "--overwrite",
        )
        # get osmhandlers to the construction and boundary file
        construction_f_handler = _osm_handler()
        construction_f_handler.apply_file(construction_f)
        boundary_f_handler = _osm_handler()
        boundary_f_handler.apply_file(boundary_f)

        # update sites to reflect end of construction
        new_way = construction_f_handler.update_prev_post_tag(sites, way, boundary_f_handler, prev=(mode < 2))
        # if a a new_way exists, add it to the incomplete_ways
        if new_way is not None:
            incomplete_ways.append(new_way)
        else:
            complete_ways.append(way)

    return complete_ways, incomplete_ways


def update_existing_sites(uc, completed, current_date):
    """
    Updates the WayChains in the WayChainMaps uc and completed with new end dates as of current date. This function also
    transfers completed WayChains from uc to completed.
    :param uc: A WayChainMap of ways under construction
    :param completed: A WayChainMap of ways that have completed construction
    :param current_date: The date to check for construction changes
    :return: None
    """
    # Get new only construction file for current date
    filtered_construction = str(snapshot_dir / f"filter_file_{str(current_date)}_only_construction.osm")
    candid_daily_snapshot = str(snapshot_dir / f"{str(current_date)}-candid.osm")
    run_osmium("tags-filter", candid_daily_snapshot, "w/*=construction", "-o", filtered_construction)
    construction_f_handler = _osm_handler()
    construction_f_handler.apply_file(filtered_construction)
    # list of keys that need to be transferred from uc to complete because they completed construction
    transfer_keys = []
    # check which way chains have a way under construction today
    for site_key in uc.map.keys():
        uc.map[site_key].end = current_date - timedelta(days=1)
        # if most recent site in chain not observed as construction anymore
        most_recent_key = int(site_key.split("-")[0].split("_")[-1])
        if most_recent_key not in construction_f_handler.osm_data:
            transfer_keys.append(most_recent_key)
    # get updated end tags for ways and increment chains
    complete_ways, incomplete_ways = generate_boundary_tags(current_date, transfer_keys, uc, mode=2)
    # transfer complete ways from uc to completed
    for c_way in complete_ways:
        completed_way_key = uc.find_key(c_way)
        completed.map[completed_way_key] = uc.map[completed_way_key]
        uc.map.pop(completed_way_key)


def _save_gdf(gdf, stem: str) -> None:
    """Write GeoPackage, GeoJSON, and Shapefile copies of a collection."""
    collection_dir.mkdir(parents=True, exist_ok=True)
    gpkg = collection_dir / f"{stem}.gpkg"
    geojson = collection_dir / f"{stem}.geojson"
    shp = collection_dir / f"{stem}.shp"
    gdf.to_file(gpkg, driver="GPKG")
    gdf.to_file(geojson, driver="GeoJSON")
    gdf.to_file(shp)


def locate_construction(p: dict | None = None) -> tuple[Any, Any]:
    """Dispatch to ohsome (default) or the original osmium daily-snapshot backend.

    Pass a params dict from CLI / params_from_args. With no argument, uses the
    module store set by set_params (notebook).
    """
    cfg = _extract_cfg(p)
    if cfg.get("backend", "ohsome") == "ohsome":
        from extract_ohsome import locate_construction_ohsome

        return locate_construction_ohsome(cfg)
    require_osmium()
    return locate_construction_osmium(cfg)


def locate_construction_osmium(p: dict | None = None) -> tuple[Any, Any]:
    """Paper-faithful extract: daily osmium snapshots + WayChain ID rewiring.

    :return: In-progress and completed GeoDataFrames (or None).
    """
    cfg = _extract_cfg(p)
    print("Extracting polygon of history file")
    setup_directory()
    get_outputpoly(cfg)

    # Filter for ways that have been buildings under construction before
    run_osmium(
        "tags-filter",
        "-R",
        "outputpoly.osh.pbf",
        "w/building=construction",
        "w/landuse=construction",
        "-o",
        str(ids_filtered_path),
    )
    run_osmium(
        "getid",
        "--id-osm-file",
        str(ids_filtered_path),
        "--with-history",
        "outputpoly.osh.pbf",
        "-o",
        str(tag_filtered_path),
        "--add-referenced",
    )

    # iterate through each day and obtain snapshots
    delta = timedelta(days=1)
    current_date = cfg["start"]
    end_date = cfg["end"]
    print("Generating window snapshots")
    while current_date <= end_date:
        snapshot_path = str(snapshot_dir / f"{str(current_date)}-candid.osm")
        run_osmium(
            "time-filter",
            str(tag_filtered_path),
            f"{current_date}T00:00:00Z",
            "-o",
            snapshot_path,
        )
        current_date += delta

    # Initialize the under construction sites starting with the start date snapshot
    print("Initializing map")
    uc = initialize_construction_map(cfg)

    # create new way chain map for completed sites
    completed = way_chain.WayChainMap()

    # find changes between consecutive days
    current_date = cfg["start"] + delta
    end_date = cfg["end"]
    print("Locating Construction")
    while current_date <= end_date:
        print(f"[Observing Construction Changes] {str(current_date)}")
        update_existing_sites(uc, completed, current_date)
        add_new_sites(uc, current_date)
        current_date += delta

    if not cfg["restrict-window"]:
        fill_end(uc, completed, cfg)
    print("Getting GeoDataFrames")
    uc_gdf = uc.get_gdf()
    completed_gdf = completed.get_gdf()
    print("Saving GeoDataFrames")
    if uc_gdf is not None and cfg["save-wip"]:
        _save_gdf(uc_gdf, "in_progress")
    if completed_gdf is not None:
        _save_gdf(completed_gdf, "collection")
    if not cfg["keep-temp"]:
        for filepath in temp_dir.glob("*"):
            if filepath.is_dir():
                shutil.rmtree(str(filepath))
                filepath.mkdir()
    return uc_gdf, completed_gdf


def initialize_construction_map(p: dict | None = None):
    """Initializes an under construction dictionary as of the start date of the search.

    :return: An initialized under construction WayChainMap
    """
    cfg = _extract_cfg(p)
    # WayChainMap of under construction way chains
    uc = way_chain.WayChainMap()
    # create snapshot of sites only under construction
    construction_f = str(snapshot_dir / f"filter_file_{str(cfg['start'])}_only_construction.osm")
    candid_start_snapshot = str(snapshot_dir / f"{str(cfg['start'])}-candid.osm")
    run_osmium("tags-filter", candid_start_snapshot, "w/*=construction", "-o", construction_f)
    # get construction sites as of start date
    construction_f_handler = _osm_handler()
    construction_f_handler.apply_file(construction_f)

    # get way_ids of construction sites
    for k in construction_f_handler.osm_data:
        # if a new site, add a new WayChain
        # we know there cant be chains on initialization so this line is ok
        if k not in uc.get_map_ways():
            # add new way chain
            new_chain = way_chain.WayChain(k, cfg['start'], cfg['start'], construction_f_handler.tags[k], "N/A",
                                           "N/A", construction_f_handler.geometry[k])
            # key is the new way_id + serial_no
            uc.map[str(k)+f"-{new_chain.serial_no}"] = new_chain

    if not cfg["restrict-window"]:
        return fill_start(uc, cfg)
    else:
        return uc


def get_outputpoly(p: dict | None = None):
    """
    A function which will extract a polygon from a region (specified in params) and write to an output file.
    :return: None
    """
    cfg = _extract_cfg(p)
    run_osmium(
        "extract",
        "-p",
        str(cfg["poly"]),
        str(cfg["region"]),
        "-o",
        "outputpoly.osh.pbf",
        "--with-history",
        "--overwrite",
    )


def create_dataset(gdf) -> None:
    """
    Creates an info.txt file inside a chain directory for every completed chain
    :param gdf: The dataframe containing all way chains which need an info file.
    :return: None
    """
    if gdf is None:
        print("GeoDataFrame contained no sites!")
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    for i, row in gdf.iterrows():
        chain_path = output_dir / f"{row['chain_id']}"
        chain_path.mkdir(parents=True, exist_ok=True)
        info_file = chain_path / "info.txt"
        info_file.touch()
        bounds = row["geometry"].bounds
        info_file.write_text(f"{row['start']},{row['end']},{row['prev_tag']},{row['final_tag']},{bounds[0]},"
                             f"{bounds[1]},{bounds[2]},{bounds[3]}")


def generate_construction_snapshot(current_date):
    """
    Creates a snapshot on current_date of ways under construction in outputpoly.osh.
    :param current_date: The date to create a snapshot.
    :return: None
    """
    # Create snapshot name for today
    daily_snapshot_path = str(snapshot_dir / f"{str(current_date)}-candid.osm")
    daily_filtered_path = str(snapshot_dir / f"filter_file_{str(current_date)}_only_construction.osm")
    # Get snapshot of today
    run_osmium(
        "time-filter",
        str(tag_filtered_path),
        f"{current_date}T00:00:00Z",
        "-o",
        daily_snapshot_path,
    )
    run_osmium("tags-filter", daily_snapshot_path, "w/*=construction", "-o", daily_filtered_path)


if __name__ == "__main__":
    _, collection_gdf = locate_construction(get_input())
    create_dataset(collection_gdf)
