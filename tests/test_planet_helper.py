from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from planet_helper import (
    ITEM_TYPE,
    PlanetHandlerV2,
    _as_acquired_datetime,
    acquired_date_from_filename,
    classify_planet_file,
    reflectance_to_uint8,
)


PSSCENE_VISUAL = Path("20200925_161029_69_2223_3B_Visual_clip.tif")
PSSCENE_ANALYTIC = Path("20200925_161029_69_2223_3B_AnalyticMS_clip.tif")
PSSCENE_UDM = Path("20200925_161029_69_2223_3B_udm2_clip.tif")
PSSCENE_SR = Path("20221003_002705_38_2461_3B_AnalyticMS_SR.tif")


def test_acquired_date_from_psscene_prefix_not_underscore_index_two():
    # split("_")[2] would be "69" / "38" (satellite fragment), not the date.
    assert acquired_date_from_filename(PSSCENE_VISUAL) == "2020-09-25"
    assert acquired_date_from_filename(PSSCENE_ANALYTIC) == "2020-09-25"
    assert acquired_date_from_filename(PSSCENE_UDM) == "2020-09-25"
    assert acquired_date_from_filename(PSSCENE_SR) == "2022-10-03"
    assert acquired_date_from_filename(Path("not_a_planet_file.tif")) is None


def test_classify_planet_file_visual_analytic_udm():
    assert classify_planet_file(PSSCENE_VISUAL) == "rgb"
    assert classify_planet_file(PSSCENE_ANALYTIC) == "nir"
    assert classify_planet_file(PSSCENE_SR) == "nir"
    assert classify_planet_file(PSSCENE_UDM) == "udm"
    assert classify_planet_file(Path("20200925_161029_69_2223_manifest.json")) is None


def test_udm_wins_when_filename_also_contains_analytic():
    path = Path("20200925_161029_69_2223_3B_AnalyticMS_DN_udm.tif")
    assert classify_planet_file(path) == "udm"


def test_reflectance_to_uint8_scales_float_and_uint16():
    float_nir = np.array([[0.0, 0.5, 1.0]], dtype=np.float32)
    scaled = reflectance_to_uint8(float_nir)
    assert scaled.dtype == np.uint8
    assert scaled.tolist() == [[0, 127, 255]]

    raw = np.array([[0, 5000, 10000]], dtype=np.uint16)
    scaled_sr = reflectance_to_uint8(raw)
    assert scaled_sr.dtype == np.uint8
    assert scaled_sr.tolist() == [[0, 127, 255]]

    already = np.array([[10, 20, 30]], dtype=np.uint8)
    assert reflectance_to_uint8(already).tolist() == [[10, 20, 30]]


def test_item_type_is_psscene_not_legacy_tiles():
    assert ITEM_TYPE == "PSScene"
    assert ITEM_TYPE not in {"PSOrthoTile", "PSScene4Band"}


def test_date_range_helper_accepts_iso_and_date():
    expected = datetime(2018, 1, 2, tzinfo=timezone.utc)
    assert _as_acquired_datetime("2018-01-02") == expected
    assert _as_acquired_datetime(date(2018, 1, 2)) == expected
    assert _as_acquired_datetime(expected) == expected


def test_planet_sync_constructor_accepts_session_keyword():
    planet = pytest.importorskip("planet")
    import inspect

    signature = inspect.signature(planet.Planet.__init__)
    params = list(signature.parameters)
    assert params[1] == "session"
    assert params[2] == "base_url"
    session_sig = inspect.signature(planet.Session.__init__)
    assert "auth" in session_sig.parameters


def test_planet_handler_bundles_rgb_and_nir(monkeypatch):
    monkeypatch.setattr("planet_helper._planet_client", lambda key: object())
    params = {
        "rgb": True,
        "nir": True,
        "num-images": 3,
        "padding": 1,
        "verbose": False,
        "email": False,
    }
    handler = PlanetHandlerV2("dummy-key", params, gdf=None)
    assert handler.bundle == ["visual", "analytic_udm2"]
    with pytest.raises(ValueError):
        PlanetHandlerV2("dummy-key", {**params, "rgb": False, "nir": False}, gdf=None)
