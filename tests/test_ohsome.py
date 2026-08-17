from datetime import date

from shapely.geometry import Polygon

from extract_ohsome import merge_construction_spans
from polyfile import load_poly


def test_campus_poly_loads():
    polygon = load_poly("poly/campus.poly")
    assert isinstance(polygon, Polygon)
    assert polygon.area > 0
    minx, miny, maxx, maxy = polygon.bounds
    assert -84 < minx < maxx < -82
    assert 39 < miny < maxy < 41


def test_merge_construction_spans_groups_versions():
    features = [
        {
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            },
            "properties": {
                "@osmId": "way/1",
                "@validFrom": "2018-01-01T00:00:00Z",
                "@validTo": "2018-03-01T00:00:00Z",
                "landuse": "construction",
            },
        },
        {
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
            },
            "properties": {
                "@osmId": "way/1",
                "@validFrom": "2018-03-01T00:00:00Z",
                "@validTo": "2018-06-01T00:00:00Z",
                "landuse": "construction",
            },
        },
        {
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
            },
            "properties": {
                "@osmId": "way/2",
                "@validFrom": "2018-04-01T00:00:00Z",
                "@validTo": "2020-07-01T00:00:00Z",
                "building": "construction",
            },
        },
    ]
    wip, completed = merge_construction_spans(features, query_end=date(2020, 7, 1))
    assert completed is not None
    assert len(completed) == 1
    assert completed.iloc[0]["chain_id"].startswith("way-1")
    assert completed.iloc[0]["start"] == "2018-01-01"
    assert completed.iloc[0]["end"] == "2018-06-01"
    assert wip is not None
    assert wip.iloc[0]["chain_id"].startswith("way-2")
    assert wip.iloc[0]["constr_tag"] == "building"
