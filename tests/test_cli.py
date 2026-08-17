"""CLI parsing tests. Do not call osmium or imagery APIs."""

from __future__ import annotations

import pytest

from cli import build_parser, main


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


def test_setup_creates_workspace_dirs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["setup"]) == 0
    assert (tmp_path / "temp" / "snapshots").is_dir()
    assert (tmp_path / "output" / "collection").is_dir()


def test_main_help_does_not_run_extract_or_gather():
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_gather_argv_is_accepted_by_gather_images_get_input():
    pytest.importorskip("geopandas")
    import gather_images
    from cli import _gather_argv

    args = build_parser().parse_args(
        ["gather", "-s", "sentinel", "--rgb", "--nir", "-n", "3", "--sentinel-provider", "cdse"]
    )
    params = gather_images.get_input(_gather_argv(args))
    assert params["source"] == "s"
    assert params["rgb"] is True
    assert params["nir"] is True
    assert params["num-images"] == 3
    assert params["sentinel-provider"] == "cdse"
    assert params["download-planet"] is False


def test_gather_stac_source_is_accepted():
    pytest.importorskip("geopandas")
    import gather_images
    from cli import _gather_argv

    args = build_parser().parse_args(["gather", "-s", "stac", "--rgb", "--nir", "-n", "3"])
    params = gather_images.get_input(_gather_argv(args))
    assert params["source"] == "stac"
    assert params["rgb"] is True
