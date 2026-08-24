"""The ``cssic extract`` command over a synthetic history. No network."""

from __future__ import annotations

import pytest

from cssic.cli import main


def poly_file(tmp_path):
    """A minimal Osmosis .poly file covering the synthetic history below."""
    path = tmp_path / "aoi.poly"
    path.write_text(
        "aoi\n1\n   -1.0  -1.0\n   3.0  -1.0\n   3.0  3.0\n   -1.0  3.0\n   -1.0  -1.0\nEND\nEND\n",
        encoding="utf-8",
    )
    return path


class FakeHistory:
    """A synthetic ``HistorySource``: fixed intervals, empty daily snapshots."""

    def __init__(self, intervals):
        self.intervals = list(intervals)
        self.snapshot_days: list = []

    def construction_intervals(self, bbox, window):
        start, end = window
        return [i for i in self.intervals if i.valid_to >= start and i.valid_from <= end]

    def snapshot(self, day, bbox):
        self.snapshot_days.append(day)
        return []


def extract_history(monkeypatch):
    """One chain that finishes inside the window and one still under way."""
    from datetime import date as _date

    from shapely.geometry import box as _box

    import cssic.history as history_module
    from cssic.history.base import Interval

    site = _box(0.0, 0.0, 1.0, 1.0)
    history = FakeHistory(
        [
            Interval("way-1", _date(2018, 3, 2), _date(2018, 3, 5), site, "landuse"),
            Interval("way-2", _date(2018, 3, 2), _date(2018, 4, 30), site, "landuse"),
        ]
    )
    monkeypatch.setattr(history_module, "get_history_source", lambda *a, **kw: history)
    return history


def extract_argv(tmp_path, *extra):
    return [
        "extract",
        "-s",
        "2018-03-01",
        "-e",
        "2018-03-10",
        "-p",
        str(poly_file(tmp_path)),
        "--restrict-window",
        *extra,
    ]


def test_extract_reports_an_unparseable_poly_file(tmp_path, monkeypatch, capsys):
    """The path exists, so ExtractConfig accepts it; load_poly is what rejects it."""
    monkeypatch.chdir(tmp_path)
    broken = tmp_path / "notes.md"
    broken.write_text("# this is not a poly file\n", encoding="utf-8")

    assert main(["extract", "-s", "2018-03-01", "-e", "2018-03-10", "-p", str(broken)]) == 2
    out = capsys.readouterr().out
    assert out.startswith("ERROR:")
    assert "Traceback" not in out


def test_extract_reports_a_missing_osmium_tool(tmp_path, monkeypatch, capsys):
    """``--backend osmium`` without the binary is an install problem, not a crash."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("shutil.which", lambda name: None)
    region = tmp_path / "region.osh.pbf"
    region.write_bytes(b"")

    argv = extract_argv(tmp_path, "--backend", "osmium", "-r", str(region))
    assert main(argv) == 2
    out = capsys.readouterr().out
    assert out.startswith("ERROR:")
    assert "osmium-tool is not on PATH" in out


def test_extract_says_how_many_chains_are_still_in_progress(tmp_path, monkeypatch, capsys):
    """Chains missing from output/ because they never finished must be accounted for."""
    pytest.importorskip("geopandas")
    extract_history(monkeypatch)

    assert main(extract_argv(tmp_path)) == 0
    out = capsys.readouterr().out
    assert "Saved 1 construction chains" in out
    assert "Skipped 1 chain(s) still in progress at 2018-03-10 (keep them with --save-wip)" in out


def test_extract_keeps_the_ohsome_snapshot_cache(tmp_path, monkeypatch, capsys):
    """The cache turns a 15-minute cold run into a 25-second one; never bin it."""
    pytest.importorskip("geopandas")
    extract_history(monkeypatch)
    from cssic.store import Workspace

    ws = Workspace(tmp_path)
    ws.setup()
    cached = ws.snapshot_dir / "snapshot_2018-03-01_x.geojson"
    cached.write_text("{}", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(extract_argv(tmp_path)) == 0
    assert cached.is_file()


def test_extract_clears_the_osmium_dumps_unless_keep_temp(tmp_path, monkeypatch):
    """--keep-temp still means what the v2 README said: keep osmium's per-day dumps."""
    pytest.importorskip("geopandas")
    extract_history(monkeypatch)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/osmium")
    from cssic.store import Workspace

    ws = Workspace(tmp_path)
    ws.setup()
    region = tmp_path / "region.osh.pbf"
    region.write_bytes(b"")
    dump = ws.snapshot_dir / "2018-03-02-construction.osm"
    dump.write_text("x", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    argv = extract_argv(tmp_path, "--backend", "osmium", "-r", str(region))
    assert main(argv) == 0
    assert not dump.exists()

    dump.write_text("x", encoding="utf-8")
    assert main([*argv, "--keep-temp"]) == 0
    assert dump.is_file()


def test_extract_counts_snapshots_by_default_and_says_nothing_with_quiet(
    tmp_path, monkeypatch, capsys
):
    """Default output is phase headers plus one line per snapshot; -q is summary only."""
    pytest.importorskip("geopandas")
    history = extract_history(monkeypatch)

    assert main(extract_argv(tmp_path)) == 0
    default = capsys.readouterr().out
    assert history.snapshot_days, "the fixture must exercise at least one snapshot"
    assert "[snapshot 1]" in default
    assert f"Read {len(history.snapshot_days)} daily snapshot(s)" in default
    assert "[Observing Construction Changes]" not in default

    extract_history(monkeypatch)
    assert main(extract_argv(tmp_path, "-q")) == 0
    quiet = capsys.readouterr().out
    assert "[snapshot 1]" not in quiet
    assert "Searching ohsome history" not in quiet
    assert "Saved 1 construction chains" in quiet


def test_extract_verbose_echoes_the_day_by_day_walk(tmp_path, monkeypatch, capsys):
    pytest.importorskip("geopandas")
    extract_history(monkeypatch)

    assert main(extract_argv(tmp_path, "-v")) == 0
    out = capsys.readouterr().out
    assert "[Observing Construction Changes] 2018-03-02" in out
