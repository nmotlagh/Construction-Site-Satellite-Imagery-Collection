"""Validation lives in cssic.config; these are the v2 check_* rules."""

from datetime import date, timedelta

import pytest

from cssic.config import Credentials, ExtractConfig, GatherConfig

GOOD_START = "2018-04-15"
GOOD_END = "2020-07-01"


@pytest.fixture
def poly_file(tmp_path):
    """A minimal Osmosis .poly file (ExtractConfig only checks that it exists)."""
    path = tmp_path / "campus.poly"
    path.write_text("campus\n1\n  -83.0 40.0\n  -83.0 40.1\n  -82.9 40.1\n  -82.9 40.0\nEND\nEND\n")
    return path


def test_extract_config_parses_dates_and_paths(poly_file):
    cfg = ExtractConfig(start=GOOD_START, end=GOOD_END, poly=poly_file)
    assert cfg.start == date(2018, 4, 15)
    assert cfg.end == date(2020, 7, 1)
    assert cfg.backend == "ohsome"
    assert cfg.window == (date(2018, 4, 15), date(2020, 7, 1))
    assert cfg.poly.name == "campus.poly"


def test_extract_config_is_frozen(poly_file):
    cfg = ExtractConfig(start=GOOD_START, end=GOOD_END, poly=poly_file)
    with pytest.raises(AttributeError):
        cfg.start = date(2019, 1, 1)


def test_extract_config_rejects_start_before_sentinel_2(poly_file):
    with pytest.raises(ValueError):
        ExtractConfig(start="2015-06-21", end=GOOD_END, poly=poly_file)


def test_extract_config_rejects_end_before_start(poly_file):
    with pytest.raises(ValueError):
        ExtractConfig(start="2019-01-01", end="2018-01-01", poly=poly_file)


def test_extract_config_rejects_recent_end_date(poly_file):
    too_recent = (date.today() - timedelta(days=3)).isoformat()
    with pytest.raises(ValueError):
        ExtractConfig(start=GOOD_START, end=too_recent, poly=poly_file)


def test_extract_config_rejects_missing_poly():
    with pytest.raises(ValueError):
        ExtractConfig(start=GOOD_START, end=GOOD_END, poly="does-not-exist.poly")


def test_extract_config_osmium_backend_requires_region(poly_file):
    with pytest.raises(ValueError):
        ExtractConfig(start=GOOD_START, end=GOOD_END, poly=poly_file, backend="osmium")


def test_extract_config_rejects_unknown_backend(poly_file):
    with pytest.raises(ValueError):
        ExtractConfig(start=GOOD_START, end=GOOD_END, poly=poly_file, backend="overpass")


def test_extract_config_confidences_default_to_the_paper_thresholds(poly_file):
    cfg = ExtractConfig(start=GOOD_START, end=GOOD_END, poly=poly_file)
    assert cfg.construction_chain_confidence == 0.5
    assert cfg.tag_confidence == 0.5


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("p", "planet"),
        ("planet", "planet"),
        ("s", "sentinel"),
        ("sentinel", "sentinel"),
        ("stac", "stac"),
    ],
)
def test_gather_config_source_aliases(given, expected):
    assert GatherConfig(source=given, rgb=True).source == expected


def test_gather_config_rejects_unknown_source():
    with pytest.raises(ValueError):
        GatherConfig(source="landsat", rgb=True)


def test_gather_config_requires_a_band():
    with pytest.raises(ValueError):
        GatherConfig(source="stac")


def test_gather_config_bands_and_source_dir():
    cfg = GatherConfig(source="p", rgb=True, nir=True)
    assert cfg.bands == ("rgb", "nir")
    assert cfg.source_dir == "planet"
    assert GatherConfig(source="stac", nir=True).source_dir == "sentinel"


@pytest.mark.parametrize("n", [1, 2, 3, 12, -1])
def test_gather_config_accepts_any_positive_n_or_minus_one(n):
    assert GatherConfig(source="stac", rgb=True, num_images=n).num_images == n


@pytest.mark.parametrize("n", [0, -2, "many"])
def test_gather_config_rejects_bad_n(n):
    with pytest.raises(ValueError):
        GatherConfig(source="stac", rgb=True, num_images=n)


@pytest.mark.parametrize("padding", [0, -1, "wide", "nan", "inf", "-inf", "1e309"])
def test_gather_config_rejects_bad_padding(padding):
    with pytest.raises(ValueError):
        GatherConfig(source="stac", rgb=True, padding=padding)


def test_gather_config_coerces_string_numbers():
    cfg = GatherConfig(source="stac", rgb=True, num_images="4", padding="2.5")
    assert cfg.num_images == 4
    assert cfg.padding == 2.5


def test_credentials_from_explicit_env_mapping():
    creds = Credentials.from_env(
        env={"PLANET_API_KEY": " key ", "SH_CLIENT_ID": "id", "SH_CLIENT_SECRET": "secret"}
    )
    assert creds.planet_api_key == "key"
    assert creds.sentinel_provider == "cdse"
    assert creds.has_sentinel_hub


def test_credentials_explicit_values_win_over_env():
    creds = Credentials.from_env(planet_api_key="explicit", env={"PLANET_API_KEY": "from-env"})
    assert creds.planet_api_key == "explicit"


def test_credentials_missing_values_are_none():
    creds = Credentials.from_env(env={})
    assert creds.planet_api_key is None
    assert not creds.has_sentinel_hub


def test_credentials_rejects_unknown_provider():
    with pytest.raises(ValueError):
        Credentials(sentinel_provider="landsat-hub")


def test_day_padding_reports_the_same_wording_as_the_other_numeric_flags():
    """``-n abc`` and ``--day-padding abc`` must not read like different programs."""
    with pytest.raises(ValueError, match=r"day-padding must be an int, got 'abc'"):
        GatherConfig(source="stac", rgb=True, day_padding="abc")


def test_day_padding_still_rejects_negative_values():
    with pytest.raises(ValueError, match="day-padding must be >= 0"):
        GatherConfig(source="stac", rgb=True, day_padding=-1)


def test_extract_config_verbosity_levels(poly_file):
    quiet = ExtractConfig(start=GOOD_START, end=GOOD_END, poly=poly_file, quiet=True)
    loud = ExtractConfig(start=GOOD_START, end=GOOD_END, poly=poly_file, verbose=True)
    default = ExtractConfig(start=GOOD_START, end=GOOD_END, poly=poly_file)
    assert (quiet.verbosity, default.verbosity, loud.verbosity) == (0, 1, 2)


def test_extract_config_rejects_verbose_and_quiet_together(poly_file):
    with pytest.raises(ValueError):
        ExtractConfig(start=GOOD_START, end=GOOD_END, poly=poly_file, verbose=True, quiet=True)
