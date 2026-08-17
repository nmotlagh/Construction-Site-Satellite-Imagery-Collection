import argparse

import pytest

import gather_images
from credentials import planet_credentials, sentinel_credentials
from gather_images import (
    SentinelHandler,
    check_num_images,
    check_padding,
    check_source,
    get_input,
    set_params,
)


def test_gather_script_help_lists_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        get_input(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "stac" in help_text
    assert "--api" in help_text
    assert "--download" in help_text


def test_get_input_requires_rgb_or_nir():
    with pytest.raises(SystemExit) as exc:
        get_input(["--source", "p", "--num-images", "3"])
    assert exc.value.code == 2


def test_get_input_accepts_api_key_and_download_flags():
    params = get_input(
        [
            "--source",
            "planet",
            "--rgb",
            "--api",
            "test-planet-key",
            "--download",
        ]
    )
    assert params["source"] == "p"
    assert params["rgb"] is True
    assert params["nir"] is False
    assert params["api"] == "test-planet-key"
    assert params["download-planet"] is True


def test_get_input_download_planet_alias_and_api_key_alias():
    params = get_input(
        ["-s", "s", "--nir", "--api-key", "k2", "--download-planet"]
    )
    assert params["source"] == "s"
    assert params["nir"] is True
    assert params["api"] == "k2"
    assert params["download-planet"] is True


def test_get_input_api_requires_a_value():
    with pytest.raises(SystemExit):
        get_input(["--source", "p", "--rgb", "--api"])


def test_rgb_and_nir_are_flags_not_options_with_args():
    params = get_input(["-s", "p", "-C", "-N", "-v"])
    assert params["rgb"] is True
    assert params["nir"] is True
    assert params["verbose"] is True
    assert params["num-images"] == 3


def test_padding_and_num_images_validation():
    assert check_num_images("3") == 3
    assert check_num_images(-1) == -1
    assert check_num_images("2") is None
    assert check_num_images("nope") is None
    assert check_padding("1") == 1.0
    assert check_padding("2.25") == 2.25
    assert check_padding("0") is None
    assert check_padding("-1") is None
    assert check_padding("wide") is None


def test_get_input_rejects_invalid_padding_and_num_images():
    with pytest.raises(SystemExit) as exc:
        get_input(["-s", "p", "-C", "--padding", "0"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        get_input(["-s", "p", "-C", "--num-images", "2"])


def test_set_params_requires_band_and_keeps_public_dict():
    original = dict(gather_images.parameters)
    try:
        set_params({"source": "p", "rgb": True, "num-images": 4, "padding": 2})
        assert gather_images.parameters["source"] == "p"
        assert gather_images.parameters["rgb"] is True
        assert gather_images.parameters["num-images"] == 4
        assert gather_images.parameters["padding"] == 2.0
        with pytest.raises(SystemExit):
            set_params({"source": "p"})
    finally:
        gather_images.parameters = original


def test_check_source_aliases():
    assert check_source("p") == "p"
    assert check_source("planet") == "p"
    assert check_source("sentinel") == "s"
    assert check_source("stac") == "stac"
    assert check_source("landsat") is None


def test_planet_credentials_env_loading(monkeypatch):
    monkeypatch.delenv("PLANET_API_KEY", raising=False)
    monkeypatch.delenv("PL_API_KEY", raising=False)
    monkeypatch.delenv("PL_AUTH_API_KEY", raising=False)
    assert planet_credentials().api_key is None

    monkeypatch.setenv("PL_API_KEY", " from-pl ")
    assert planet_credentials().api_key == "from-pl"

    monkeypatch.setenv("PLANET_API_KEY", "from-planet")
    assert planet_credentials().api_key == "from-planet"
    assert planet_credentials("explicit").api_key == "explicit"
    assert planet_credentials("   ").api_key is None


def test_sentinel_credentials_env_loading(monkeypatch):
    monkeypatch.delenv("SH_CLIENT_ID", raising=False)
    monkeypatch.delenv("SH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("SENTINEL_PROVIDER", raising=False)
    creds = sentinel_credentials()
    assert creds.client_id is None
    assert creds.client_secret is None
    assert creds.provider == "cdse"

    monkeypatch.setenv("SH_CLIENT_ID", " id ")
    monkeypatch.setenv("SH_CLIENT_SECRET", " secret ")
    monkeypatch.setenv("SENTINEL_PROVIDER", "sentinelhub")
    creds = sentinel_credentials()
    assert creds.client_id == "id"
    assert creds.client_secret == "secret"
    assert creds.provider == "sentinelhub"

    creds = sentinel_credentials("cli-id", "cli-secret", "cdse")
    assert creds.client_id == "cli-id"
    assert creds.provider == "cdse"
    with pytest.raises(ValueError):
        sentinel_credentials(provider="google")


def test_sentinel_handler_is_exported():
    assert SentinelHandler.__name__ == "SentinelHandler"


def test_gather_from_source_uses_passed_params(monkeypatch):
    seen = {}

    class FakeSTAC:
        def __init__(self, params, gdf):
            seen["params"] = params
            seen["gdf"] = gdf

        def get_all_imagery(self):
            seen["called"] = True

    monkeypatch.setattr("sentinel_stac.SentinelSTACHandler", FakeSTAC)
    cfg = {
        "source": "stac",
        "rgb": True,
        "nir": False,
        "num-images": 3,
        "padding": 1.0,
        "verbose": False,
        "email": False,
        "download-planet": False,
        "api": None,
        "sh-client-id": None,
        "sh-client-secret": None,
        "sentinel-provider": "cdse",
    }
    gather_images.gather_from_source("gdf-sentinel", cfg)
    assert seen["called"] is True
    assert seen["params"] is cfg
    assert seen["gdf"] == "gdf-sentinel"


def test_argparse_does_not_treat_api_as_store_true():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", "--api-key", dest="api", default=None)
    args = parser.parse_args(["--api", "KEY"])
    assert args.api == "KEY"
