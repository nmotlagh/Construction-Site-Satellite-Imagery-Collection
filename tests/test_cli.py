"""CLI parsing and dispatch tests. Do not call osmium, ohsome, or imagery APIs.

The command pipelines themselves are tested in ``test_extract_cmd.py`` and
``test_gather_cmd.py``; the optional-dependency preflight in ``test_deps.py``.
"""

from __future__ import annotations

import pytest

from cssic.cli import build_parser, gather_config, main


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


def test_day_padding_reaches_the_config():
    """v2 ``get_dates_sentinel`` widened each sampled date by six days."""
    default = gather_config(build_parser().parse_args(["gather", "-s", "stac", "--rgb"]))
    assert default.day_padding == 6

    args = build_parser().parse_args(["gather", "-s", "stac", "--rgb", "--day-padding", "3"])
    assert gather_config(args).day_padding == 3


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


@pytest.mark.parametrize("padding", ["nan", "inf", "1e309"])
def test_nonfinite_padding_is_rejected_before_credentials_or_gather(monkeypatch, capsys, padding):
    import cssic.cli as cli

    def unexpected_call(*args, **kwargs):
        pytest.fail("invalid padding must be rejected before credentials or gather")

    monkeypatch.setattr(cli.Credentials, "from_env", unexpected_call)
    monkeypatch.setattr(cli, "run_gather", unexpected_call)
    assert main(["gather", "-s", "stac", "--rgb", "--padding", padding]) == 2
    out = capsys.readouterr().out
    assert out.count("ERROR:") == 1
    assert "padding must be finite and > 0" in out


def test_setup_uses_selected_workspace_without_changing_directory(tmp_path, monkeypatch):
    from pathlib import Path

    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)

    assert main(["--workspace", "../selected", "setup"]) == 0
    assert (tmp_path / "selected" / "temp" / "snapshots").is_dir()
    assert (tmp_path / "selected" / "output" / "collection").is_dir()
    assert list(caller.iterdir()) == []
    assert Path.cwd() == caller


@pytest.mark.parametrize("command", ["reset-extract", "reset-images"])
def test_reset_only_changes_the_selected_workspace(tmp_path, monkeypatch, command):
    from pathlib import Path

    from cssic.store import Workspace

    markers = {}
    for name in ("selected", "caller", "other"):
        ws = Workspace(tmp_path / name)
        paths = [
            ws.collection_dir / "collection.gpkg",
            ws.snapshot_dir / "snapshot.geojson",
            ws.chip_path("10-0", "sentinel", "rgb", "2018-01-01"),
        ]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        markers[name] = paths
    monkeypatch.chdir(tmp_path / "caller")

    assert main(["--workspace", "../selected", command]) == 0
    assert Path.cwd() == tmp_path / "caller"
    for name in ("caller", "other"):
        assert all(path.read_text() == name for path in markers[name])
    assert not markers["selected"][2].exists()
    for path in markers["selected"][:2]:
        if command == "reset-images":
            assert path.read_text() == "selected"
        else:
            assert not path.exists()
