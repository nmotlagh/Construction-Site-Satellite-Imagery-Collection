"""CLI parsing tests. Do not call osmium, ohsome, or imagery APIs."""

from __future__ import annotations

import pytest

from cssic.cli import build_parser, gather_config, main
from cssic.config import Credentials


def test_missing_subcommand_exits_nonzero():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args([])
    assert exc.value.code != 0


def test_help_exits_zero():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0


def test_extract_help_lists_poly_and_region(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["extract", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--poly" in help_text
    assert "--region" in help_text
    assert "--backend" in help_text
    assert "--restrict-window" in help_text


def test_gather_help_lists_env_backed_flags(capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["gather", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "--api" in help_text
    assert "--download" in help_text
    assert "--sh-client-id" in help_text
    assert "--sh-client-secret" in help_text
    assert "--sentinel-provider" in help_text
    assert "--rgb" in help_text
    assert "--nir" in help_text
    assert "stac" in help_text


def test_extract_parse_does_not_require_files_to_exist():
    args = build_parser().parse_args(
        [
            "extract",
            "-s",
            "2018-04-15",
            "-e",
            "2020-07-01",
            "--poly",
            "poly/campus.poly",
            "--keep-temp",
            "--save-wip",
        ]
    )
    assert args.command == "extract"
    assert args.start == "2018-04-15"
    assert args.backend == "ohsome"
    assert args.region is None
    assert args.keep_temp is True
    assert args.save_wip is True
    assert args.restrict_window is False


def test_extract_parse_osmium_backend():
    args = build_parser().parse_args(
        [
            "extract",
            "-s",
            "2018-04-15",
            "-e",
            "2020-07-01",
            "--poly",
            "poly/campus.poly",
            "--backend",
            "osmium",
            "--region",
            "missing.osh.pbf",
        ]
    )
    assert args.backend == "osmium"
    assert args.region == "missing.osh.pbf"


def test_gather_parse_forwards_new_flags():
    args = build_parser().parse_args(
        [
            "gather",
            "-s",
            "p",
            "--rgb",
            "--nir",
            "--download",
            "--sh-client-id",
            "demo-id",
            "--sentinel-provider",
            "cdse",
        ]
    )
    assert args.command == "gather"
    assert args.source == "p"
    assert args.rgb is True
    assert args.nir is True
    assert args.download_planet is True
    assert args.sh_client_id == "demo-id"
    assert args.sentinel_provider == "cdse"


def test_gather_config_from_parsed_args():
    args = build_parser().parse_args(
        ["gather", "-s", "sentinel", "--rgb", "--nir", "-n", "3", "--sentinel-provider", "cdse"]
    )
    cfg = gather_config(args)
    assert cfg.source == "sentinel"
    assert cfg.rgb is True
    assert cfg.nir is True
    assert cfg.num_images == 3
    assert cfg.download_planet is False


def test_gather_stac_source_is_accepted():
    args = build_parser().parse_args(["gather", "-s", "stac", "--rgb", "-n", "1"])
    cfg = gather_config(args)
    assert cfg.source == "stac"
    assert cfg.num_images == 1
    assert cfg.source_dir == "sentinel"


def test_gather_rejects_missing_band(capsys):
    assert main(["gather", "-s", "stac"]) == 2
    assert "ERROR" in capsys.readouterr().out


def test_gather_with_no_collection_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["gather", "-s", "stac", "--rgb"]) == 2
    assert "No construction sites found" in capsys.readouterr().out


def test_gather_planet_reports_bad_credentials_without_a_traceback(monkeypatch, capsys):
    """A rejected Planet key is a user error, not a crash: ``ERROR: ...`` and exit 2."""
    pytest.importorskip("planet")
    from planet.exceptions import InvalidAPIKey

    import cssic.cli as cli_module
    import cssic.imagery as imagery

    class RejectingSource:
        def bulk_order(self, gdf, workspace):
            raise InvalidAPIKey("Please provide valid credentials.")

    monkeypatch.setattr(imagery, "get_image_source", lambda *a, **kw: RejectingSource())
    cfg = gather_config(build_parser().parse_args(["gather", "-s", "p", "--rgb"]))
    with pytest.raises(ValueError, match="PLANET_API_KEY"):
        cli_module._run_planet(cfg, Credentials(), object(), None)


def test_extract_rejects_bad_dates(capsys):
    assert main(["extract", "-s", "2015-01-01", "-e", "2020-07-01", "-p", "poly/campus.poly"]) == 2
    assert "ERROR" in capsys.readouterr().out


def test_setup_creates_workspace_dirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["setup"]) == 0
    assert (tmp_path / "temp" / "snapshots").is_dir()
    assert (tmp_path / "output" / "collection").is_dir()


def test_reset_commands_run_on_a_fresh_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["reset-extract"]) == 0
    assert main(["reset-images"]) == 0


def test_main_help_does_not_run_extract_or_gather():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


# --- optional backends are checked before any network or credential work ----


def test_require_backend_names_the_module_and_the_extra(monkeypatch):
    import cssic.cli as cli_module

    monkeypatch.setattr(
        cli_module, "BACKEND_REQUIREMENTS", {"stac": ("stac", ("no_such_module_xyz",))}
    )
    with pytest.raises(ValueError) as exc:
        cli_module.require_backend("stac")

    message = str(exc.value)
    assert "no_such_module_xyz" in message
    assert "uv sync --extra stac" in message


def test_require_backend_is_quiet_when_everything_is_installed(monkeypatch):
    import cssic.cli as cli_module

    monkeypatch.setattr(cli_module, "BACKEND_REQUIREMENTS", {"stac": ("stac", ("json",))})
    assert cli_module.require_backend("stac") is None


def test_an_unknown_source_has_no_requirements():
    import cssic.cli as cli_module

    assert cli_module.require_backend("nothing-like-this") is None


def test_the_real_backend_requirements_name_real_extras():
    import cssic.cli as cli_module

    extras = {extra for extra, _modules in cli_module.BACKEND_REQUIREMENTS.values()}
    assert extras == {"stac", "sentinelhub", "planet"}


# --- gather over a fake image source ---------------------------------------


def collection(tmp_path):
    """A one-chain workspace, as ``cssic extract`` would have left it."""
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import box

    from cssic.store import Workspace

    ws = Workspace(tmp_path)
    ws.save_collection(
        gpd.GeoDataFrame(
            {
                "chain_id": ["10-0", "20-1"],
                "start": ["2018-01-01", "2018-01-01"],
                "end": ["2018-06-01", "2018-06-01"],
                "constr_tag": ["landuse", "landuse"],
                "prev_tag": ["farmland", "farmland"],
                "final_tag": ["residential", "residential"],
            },
            geometry=[box(0, 0, 0.01, 0.01), box(5, 5, 5.01, 5.01)],
            crs="EPSG:4326",
        )
    )
    return ws


def chip_array():
    """A chip with actual variation: a constant array is discarded as blank."""
    import numpy as np

    return np.arange(8 * 8 * 3, dtype="uint8").reshape(8, 8, 3)


class FakeScene:
    def __init__(self, scene_id="a", acquired=None, cloud_cover=1.0):
        from datetime import date as _date

        self.id = scene_id
        self.acquired = acquired or _date(2018, 3, 1)
        self.cloud_cover = cloud_cover
        self.metadata: dict = {}


def gather_with(monkeypatch, tmp_path, source, argv=("gather", "-s", "stac", "--rgb")):
    """Run ``run_gather`` against ``source`` with the backend check disabled."""
    import cssic.cli as cli_module
    import cssic.imagery as imagery

    monkeypatch.setattr(cli_module, "BACKEND_REQUIREMENTS", {})
    monkeypatch.setattr(imagery, "get_image_source", lambda *a, **kw: source)
    cfg = gather_config(build_parser().parse_args(list(argv)))
    return cli_module.run_gather(cfg, Credentials(), collection(tmp_path))


def test_gather_reports_a_missing_backend_instead_of_an_import_traceback(
    tmp_path, monkeypatch, capsys
):
    import cssic.cli as cli_module

    monkeypatch.setattr(
        cli_module, "BACKEND_REQUIREMENTS", {"stac": ("stac", ("no_such_module_xyz",))}
    )
    cfg = gather_config(build_parser().parse_args(["gather", "-s", "stac", "--rgb"]))

    assert cli_module.run_gather(cfg, Credentials(), collection(tmp_path)) == 2
    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "no_such_module_xyz" in out


def test_gather_skips_an_all_nodata_chip(tmp_path, monkeypatch, capsys):
    """A scene that only clips the site fetches as black fill; do not save it."""
    import numpy as np

    class BlankSource:
        def find_scenes(self, aoi, start, end, limit=None):
            return [FakeScene()]

        def fetch(self, scene, aoi, band):
            return np.zeros((8, 8, 3), dtype="uint8")

    assert gather_with(monkeypatch, tmp_path, BlankSource()) == 0
    out = capsys.readouterr().out
    assert "SKIPPED blank RGB" in out
    assert "Wrote 0 chip(s)" in out
    assert list(tmp_path.rglob("*.png")) == []


def test_gather_survives_one_sites_search_failing(tmp_path, monkeypatch, capsys):
    """A gather run takes hours; one transient API error must not lose the rest."""

    class FlakySource:
        def find_scenes(self, aoi, start, end, limit=None):
            if aoi.bounds[0] < 1:  # the first chain only
                raise RuntimeError("upstream 503")
            return [FakeScene()]

        def fetch(self, scene, aoi, band):
            return chip_array()

    assert gather_with(monkeypatch, tmp_path, FlakySource()) == 0
    out = capsys.readouterr().out
    assert "10-0: SEARCH FAILED" in out
    assert "upstream 503" in out
    assert "Wrote 1 chip(s)" in out


def test_gather_asks_a_sampling_source_for_every_date_on_n_minus_one(tmp_path, monkeypatch):
    """``-n -1`` is an explicit flag, not something the window count implies."""

    class SamplingSource:
        def __init__(self):
            self.calls: list[bool] = []

        def find_scenes(self, aoi, start, end, limit=None):  # pragma: no cover - unused
            raise AssertionError("sample_scenes should be preferred")

        def sample_scenes(self, aoi, windows, all_dates=False):
            self.calls.append(all_dates)
            return [FakeScene()]

        def fetch(self, scene, aoi, band):
            return chip_array()

    source = SamplingSource()
    argv = ("gather", "-s", "stac", "--rgb", "-n", "-1")
    assert gather_with(monkeypatch, tmp_path, source, argv=argv) == 0
    assert source.calls == [True, True]

    source.calls.clear()
    one = ("gather", "-s", "stac", "--rgb", "-n", "1")
    assert gather_with(monkeypatch, tmp_path, source, one) == 0
    assert source.calls == [False, False]


def test_day_padding_reaches_the_config():
    """v2 ``get_dates_sentinel`` widened each sampled date by six days."""
    default = gather_config(build_parser().parse_args(["gather", "-s", "stac", "--rgb"]))
    assert default.day_padding == 6

    args = build_parser().parse_args(["gather", "-s", "stac", "--rgb", "--day-padding", "3"])
    assert gather_config(args).day_padding == 3


def test_the_install_hint_names_a_command_that_can_actually_run(monkeypatch):
    """The distribution is not on PyPI, so ``pip install 'cssic[stac]'`` is a dead end."""
    import cssic.cli as cli_module

    monkeypatch.setattr(
        cli_module, "BACKEND_REQUIREMENTS", {"stac": ("stac", ("no_such_module_xyz",))}
    )
    with pytest.raises(ValueError) as exc:
        cli_module.require_backend("stac")

    message = str(exc.value)
    assert "uv sync --extra stac" in message
    assert "pip install -e '.[stac]'" in message
    assert "cssic[stac]" not in message


# --- the fallback sampler ---------------------------------------------------


class WindowSource:
    """``find_scenes`` over a fixed scene list, honouring the window."""

    def __init__(self, scenes):
        self.scenes = list(scenes)

    def find_scenes(self, aoi, start, end, limit=None):
        return [scene for scene in self.scenes if start <= scene.acquired <= end]


def test_the_fallback_sampler_keeps_the_least_cloudy_scene_per_window():
    """SentinelHub has no ``sample_scenes``; the CLI does the ranking for it."""
    from datetime import date as _date

    from cssic.cli import _sample_scenes

    day = _date(2018, 3, 1)
    source = WindowSource(
        [
            FakeScene("cloudy", day, cloud_cover=80.0),
            FakeScene("clear", day, cloud_cover=2.0),
            FakeScene("meh", day, cloud_cover=40.0),
        ]
    )
    picked = _sample_scenes(source, object(), [(day, day)])
    assert [scene.id for scene in picked] == ["clear"]


class ScriptedSource:
    """``find_scenes`` returns the next scripted result, whatever the window."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def find_scenes(self, aoi, start, end, limit=None):
        found = self.results[self.calls]
        self.calls += 1
        return found


def test_the_fallback_sampler_keeps_one_scene_per_date_on_all_dates():
    """Padded windows overlap, so ``-n -1`` must dedupe by date, not only by id.

    Two tiles covering the same day are two catalog items with different ids,
    and the chip is named after the date -- keeping both means the second
    silently overwrites the first.
    """
    from datetime import date as _date

    from cssic.cli import _sample_scenes

    first, second = _date(2018, 3, 1), _date(2018, 3, 4)
    source = ScriptedSource(
        [
            [FakeScene("a", first, cloud_cover=10.0)],
            [FakeScene("b", first, cloud_cover=20.0), FakeScene("c", second, cloud_cover=30.0)],
        ]
    )
    windows = [(first, second), (first, second)]
    picked = _sample_scenes(source, object(), windows, all_dates=True)
    assert [scene.id for scene in picked] == ["a", "c"]


def test_the_fallback_sampler_names_windows_that_came_back_empty():
    """A window shorter than the Sentinel-2 revisit must not fail silently."""
    from datetime import date as _date

    from cssic.cli import _sample_scenes

    lines: list[str] = []
    source = WindowSource([FakeScene("a", _date(2018, 3, 1))])
    windows = [(_date(2018, 3, 1), _date(2018, 3, 2)), (_date(2018, 6, 1), _date(2018, 6, 2))]
    picked = _sample_scenes(source, object(), windows, log=lines.append)

    assert [scene.id for scene in picked] == ["a"]
    assert lines == ["no acquisition between 2018-06-01 and 2018-06-02"]


def test_gather_reports_how_many_of_the_requested_samples_it_wrote(tmp_path, monkeypatch, capsys):
    """``-n 3`` writing one chip is under-delivery, and has to say so."""
    from datetime import date as _date

    class OneSceneSource:
        def find_scenes(self, aoi, start, end, limit=None):
            scene = FakeScene("a", _date(2018, 1, 2))
            return [scene] if start <= scene.acquired <= end else []

        def fetch(self, scene, aoi, band):
            return chip_array()

    assert gather_with(monkeypatch, tmp_path, OneSceneSource()) == 0
    out = capsys.readouterr().out
    assert "10-0: no acquisition between 2018-03-17 and 2018-03-23" in out
    assert "10-0: wrote 1 of 3 requested samples" in out


def test_gather_pads_the_aoi_by_the_requested_area_factor(tmp_path, monkeypatch):
    """``-p 4`` is an *area* scale factor about the site centre (v2 semantics)."""

    class RecordingSource:
        def __init__(self):
            self.areas: list[float] = []

        def find_scenes(self, aoi, start, end, limit=None):
            self.areas.append(aoi.area)
            return []

    plain = RecordingSource()
    assert gather_with(monkeypatch, tmp_path, plain) == 0

    padded = RecordingSource()
    argv = ("gather", "-s", "stac", "--rgb", "-p", "4")
    assert gather_with(monkeypatch, tmp_path, padded, argv=argv) == 0

    assert plain.areas and len(padded.areas) == len(plain.areas)
    for before, after in zip(plain.areas, padded.areas, strict=True):
        assert after == pytest.approx(4 * before)


# --- Sentinel Hub credentials are checked once, not once per site -----------


def test_bad_sentinel_hub_credentials_are_one_error_line(monkeypatch, capsys):
    """An oauthlib traceback after 25 SEARCH FAILED lines is not a user error message."""
    pytest.importorskip("sentinelhub")
    import sentinelhub
    from oauthlib.oauth2.rfc6749.errors import InvalidClientError

    import cssic.cli as cli_module

    def reject(config=None):
        raise InvalidClientError(description="Invalid client credentials")

    monkeypatch.setattr(sentinelhub, "SentinelHubSession", reject)
    source = type("Source", (), {"config": object()})()

    with pytest.raises(ValueError, match="SH_CLIENT_ID"):
        cli_module._check_sentinel_credentials(source)


def test_a_transport_failure_is_not_relabelled_as_bad_credentials(monkeypatch):
    """Only the OAuth rejection is a credential error; a socket timeout is not."""
    pytest.importorskip("sentinelhub")
    import sentinelhub

    import cssic.cli as cli_module

    def die(config=None):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(sentinelhub, "SentinelHubSession", die)
    with pytest.raises(TimeoutError):
        cli_module._check_sentinel_credentials(type("S", (), {"config": object()})())


def test_gather_exits_two_when_sentinel_hub_rejects_the_keys(tmp_path, monkeypatch, capsys):
    pytest.importorskip("sentinelhub")
    import sentinelhub
    from oauthlib.oauth2.rfc6749.errors import InvalidClientError

    import cssic.cli as cli_module
    import cssic.imagery as imagery

    def reject(config=None):
        raise InvalidClientError(description="Invalid client credentials")

    monkeypatch.setattr(sentinelhub, "SentinelHubSession", reject)
    monkeypatch.setattr(cli_module, "BACKEND_REQUIREMENTS", {})
    monkeypatch.setattr(
        imagery, "get_image_source", lambda *a, **kw: type("S", (), {"config": object()})()
    )
    cfg = gather_config(build_parser().parse_args(["gather", "-s", "s", "--rgb"]))

    assert cli_module.run_gather(cfg, Credentials(), collection(tmp_path)) == 2
    out = capsys.readouterr().out
    assert out.count("ERROR:") == 1
    assert "SEARCH FAILED" not in out


def test_gather_reports_an_unreadable_collection(tmp_path, monkeypatch, capsys):
    """A truncated collection.gpkg is a workspace problem, not a pyogrio traceback."""
    pytest.importorskip("geopandas")
    from cssic.store import Workspace

    ws = Workspace(tmp_path)
    ws.setup()
    (ws.collection_dir / "collection.gpkg").write_bytes(b"not a geopackage")
    monkeypatch.chdir(tmp_path)

    assert main(["gather", "-s", "stac", "--rgb"]) == 2
    assert "ERROR:" in capsys.readouterr().out


# --- extract: user errors, verbosity, and the snapshot cache ----------------


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


def test_extract_reports_a_missing_pyosmium_with_a_real_install_command(monkeypatch):
    import importlib.util

    import cssic.cli as cli_module

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ValueError) as exc:
        cli_module.require_history_backend("osmium")
    assert "uv sync --extra osmium" in str(exc.value)


def test_require_history_backend_lets_ohsome_through():
    import cssic.cli as cli_module

    assert cli_module.require_history_backend("ohsome") is None


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


def test_extract_help_documents_what_keep_temp_actually_keeps(capsys):
    """The flag governs osmium's dumps; the ohsome cache is not up for deletion."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["extract", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Keep the per-day osmium dumps in temp/snapshots" in help_text
    assert "the ohsome snapshot cache is always kept" in help_text
    assert "--quiet" in help_text


# --- setup / reset say what they did ---------------------------------------


def test_setup_reports_the_directories_it_created(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["setup"]) == 0
    out = capsys.readouterr().out
    assert "temp/snapshots" in out
    assert "output/collection" in out


def test_reset_images_reports_how_many_files_it_removed(tmp_path, monkeypatch, capsys):
    from cssic.store import Workspace

    ws = Workspace(tmp_path)
    ws.setup()
    for day in ("2018-01-01", "2018-02-01"):
        chip = ws.chip_path("10-0", "sentinel", "rgb", day)
        chip.parent.mkdir(parents=True, exist_ok=True)
        chip.write_text("png", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["reset-images"]) == 0
    assert "Removed 2 image file(s)" in capsys.readouterr().out


def test_reset_extract_reports_how_many_files_it_removed(tmp_path, monkeypatch, capsys):
    from cssic.store import Workspace

    ws = Workspace(tmp_path)
    ws.setup()
    (ws.snapshot_dir / "2018-01-01.osm").write_text("x", encoding="utf-8")
    (ws.collection_dir / "collection.gpkg").write_text("x", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert main(["reset-extract"]) == 0
    assert "Removed 2 file(s)" in capsys.readouterr().out
