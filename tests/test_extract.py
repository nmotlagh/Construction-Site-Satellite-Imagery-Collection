from datetime import date, timedelta
from pathlib import Path

import pytest

from extract_sites import (
    check_end_date,
    check_start_date,
    get_input,
    locate_construction,
    set_params,
)


def _touch_extract_files(tmp_path: Path) -> tuple[str, str]:
    poly = tmp_path / "region.poly"
    region = tmp_path / "history.osh.pbf"
    poly.write_text("polygon")
    region.write_bytes(b"osh")
    return str(poly), str(region)


def _valid_argv(tmp_path: Path, extra: list[str] | None = None) -> list[str]:
    poly, region = _touch_extract_files(tmp_path)
    argv = [
        "--start",
        "2016-01-01",
        "--end",
        "2016-06-01",
        "--poly",
        poly,
        "--region",
        region,
    ]
    if extra:
        argv.extend(extra)
    return argv


def test_get_input_ohsome_does_not_require_region(tmp_path):
    poly = tmp_path / "region.poly"
    poly.write_text("polygon")
    parsed = get_input(
        ["--start", "2016-01-01", "--end", "2016-06-01", "--poly", str(poly)]
    )
    assert parsed["backend"] == "ohsome"
    assert parsed["region"] is None


def test_get_input_parses_region(tmp_path):
    """``--region`` takes a path. Original getopt short ``-r`` did not take a value."""
    poly, region = _touch_extract_files(tmp_path)
    parsed = get_input(
        [
            "--start",
            "2016-01-01",
            "--end",
            "2016-06-01",
            "--poly",
            poly,
            "--region",
            region,
        ]
    )
    assert parsed["region"] == region
    assert parsed["region"] is not True
    assert parsed["poly"] == poly

    parsed_short = get_input(
        [
            "-s",
            "2016-01-01",
            "-e",
            "2016-06-01",
            "-p",
            poly,
            "-r",
            region,
        ]
    )
    assert parsed_short["region"] == region


def test_get_input_osmium_requires_region(tmp_path):
    poly = tmp_path / "region.poly"
    poly.write_text("polygon")
    with pytest.raises(SystemExit) as exc:
        get_input(
            [
                "--start",
                "2016-01-01",
                "--end",
                "2016-06-01",
                "--poly",
                str(poly),
                "--backend",
                "osmium",
            ]
        )
    assert exc.value.code == 2


def test_set_params_ohsome_allows_missing_region(tmp_path):
    poly = tmp_path / "region.poly"
    poly.write_text("polygon")
    set_params(
        {
            "start": "2016-01-01",
            "end": "2016-06-01",
            "poly": str(poly),
            "region": "",
        }
    )
    from extract_sites import params

    assert params["backend"] == "ohsome"
    assert params["region"] is None
    assert params["poly"] == str(poly)


def test_get_input_save_wip_does_not_keyerror(tmp_path):
    """Returned params always include hyphenated ``save-wip`` (not the old ``wip-df`` key)."""
    with_flag = get_input(_valid_argv(tmp_path, ["--save-wip"]))
    assert with_flag["save-wip"] is True

    without_flag = get_input(_valid_argv(tmp_path))
    assert without_flag["save-wip"] is False
    assert "wip-df" not in without_flag


def test_extract_script_help_lists_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        get_input(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--backend" in help_text
    assert "--poly" in help_text
    assert "--region" in help_text


def test_locate_construction_accepts_params_dict(monkeypatch, tmp_path):
    poly = tmp_path / "region.poly"
    poly.write_text("polygon")
    seen = {}

    def fake_ohsome(p):
        seen["p"] = p
        return None, None

    monkeypatch.setattr("extract_ohsome.locate_construction_ohsome", fake_ohsome)
    cfg = {
        "start": "2016-01-01",
        "end": "2016-06-01",
        "poly": str(poly),
        "region": None,
        "backend": "ohsome",
        "keep-temp": False,
        "restrict-window": False,
        "save-wip": False,
    }
    locate_construction(cfg)
    assert seen["p"] is cfg


def test_locate_construction_uses_set_params_store(monkeypatch, tmp_path):
    poly = tmp_path / "region.poly"
    poly.write_text("polygon")
    seen = {}

    def fake_ohsome(p):
        seen["backend"] = p.get("backend")
        seen["poly"] = p.get("poly")
        return None, None

    monkeypatch.setattr("extract_ohsome.locate_construction_ohsome", fake_ohsome)
    set_params(
        {
            "start": "2016-01-01",
            "end": "2016-06-01",
            "poly": str(poly),
            "region": "",
        }
    )
    locate_construction()
    assert seen["backend"] == "ohsome"
    assert seen["poly"] == str(poly)


def test_locate_construction_osmium_backend_dispatches(monkeypatch):
    seen = {}

    def fake_osmium(p):
        seen["p"] = p
        return None, None

    monkeypatch.setattr("extract_sites.require_osmium", lambda: None)
    monkeypatch.setattr("extract_sites.locate_construction_osmium", fake_osmium)
    locate_construction({"backend": "osmium", "region": "missing.osh.pbf"})
    assert seen["p"]["backend"] == "osmium"


def test_check_start_date_validation():
    assert check_start_date("2015-06-22") == date(2015, 6, 22)
    assert check_start_date("2015-06-21") is None
    assert check_start_date("not-a-date") is None
    assert check_start_date(None) is None
    assert check_start_date("2015/06/22") is None
    assert check_start_date(date(2018, 4, 15)) == date(2018, 4, 15)


def test_check_end_date_validation():
    start = date(2016, 1, 1)
    assert check_end_date("2016-06-01", start) == date(2016, 6, 1)
    assert check_end_date("2016-01-01", start) is None
    assert check_end_date("2015-12-31", start) is None
    assert check_end_date("bad", start) is None
    assert check_end_date("2016-06-01", None) is None
    assert check_end_date(None, start) is None

    today = date.today()
    assert check_end_date(today.isoformat(), start) is None
    ten_days_ago = today - timedelta(days=10)
    assert check_end_date(ten_days_ago.isoformat(), start) is None
    eleven_days_ago = today - timedelta(days=11)
    assert check_end_date(eleven_days_ago.isoformat(), start) == eleven_days_ago
    assert check_end_date(eleven_days_ago, start) == eleven_days_ago
