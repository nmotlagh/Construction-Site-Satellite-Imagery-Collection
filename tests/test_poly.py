"""Osmosis .poly parsing and bbox helpers."""

import pytest

from cssic.poly import bbox_csv, load_poly, polygon_bounds, scale_bbox

SIMPLE = """campus
1
  -83.0 40.0
  -83.0 40.1
  -82.9 40.1
  -82.9 40.0
  -83.0 40.0
END
END
"""


def test_load_poly_reads_the_exterior_ring(tmp_path):
    path = tmp_path / "campus.poly"
    path.write_text(SIMPLE)
    polygon = load_poly(path)
    assert polygon.is_valid
    assert polygon_bounds(polygon) == pytest.approx((-83.0, 40.0, -82.9, 40.1))


def test_bbox_csv_is_ohsome_shaped(tmp_path):
    path = tmp_path / "campus.poly"
    path.write_text(SIMPLE)
    assert bbox_csv(load_poly(path)).count(",") == 3


def test_load_poly_rejects_a_file_with_no_rings(tmp_path):
    path = tmp_path / "empty.poly"
    path.write_text("none\nEND\n")
    with pytest.raises(ValueError):
        load_poly(path)


def test_scale_bbox_is_centre_invariant():
    scaled = scale_bbox((0.0, 0.0, 2.0, 2.0), 2.0)
    assert scaled == pytest.approx((-1.0, -1.0, 3.0, 3.0))


def test_scale_bbox_identity():
    assert scale_bbox((1.0, 2.0, 3.0, 4.0), 1.0) == pytest.approx((1.0, 2.0, 3.0, 4.0))


def test_scale_bbox_rejects_non_positive():
    with pytest.raises(ValueError):
        scale_bbox((0.0, 0.0, 1.0, 1.0), 0.0)


MULTI_SECTION = """osu
1
  -83.00 40.00
  -83.00 40.01
  -82.99 40.01
  -82.99 40.00
  -83.00 40.00
END
2
  -83.10 40.10
  -83.10 40.20
  -83.00 40.20
  -83.00 40.10
  -83.10 40.10
END
!3
  -83.08 40.12
  -83.08 40.14
  -83.06 40.14
  -83.06 40.12
  -83.08 40.12
END
END
"""


def test_load_poly_unions_outer_sections_and_subtracts_bang_sections(tmp_path):
    """polygons.openstreetmap.fr files have several outer sections; the
    second one is not a hole in the first (that collapsed a campus to a
    sliver), and only ``!``-named sections are subtracted."""
    path = tmp_path / "osu.poly"
    path.write_text(MULTI_SECTION)
    polygon = load_poly(path)
    assert polygon.is_valid
    assert polygon_bounds(polygon) == pytest.approx((-83.10, 40.00, -82.99, 40.20))
    from shapely.geometry import Point

    assert polygon.contains(Point(-82.995, 40.005))  # first outer section
    assert polygon.contains(Point(-83.02, 40.18))  # second outer section
    assert not polygon.contains(Point(-83.07, 40.13))  # the hole
    assert polygon.area == pytest.approx(0.1 * 0.1 + 0.01 * 0.01 - 0.02 * 0.02)
