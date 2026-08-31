"""The ``cssic gather`` pipeline over fake image sources. No network."""

from __future__ import annotations

import pytest

import cssic.deps as deps_module
import cssic.gather as gather_module
from cssic.cli import build_parser, gather_config, main
from cssic.config import Credentials
from cssic.gather import _sample_scenes, run_gather


def test_gather_with_no_collection_exits_two(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["gather", "-s", "stac", "--rgb"]) == 2
    assert "No construction sites found" in capsys.readouterr().out


def test_gather_planet_reports_bad_credentials_without_a_traceback(monkeypatch):
    """A rejected Planet key is a user error, not a crash: ``ERROR: ...`` and exit 2."""
    pytest.importorskip("planet")
    from planet.exceptions import InvalidAPIKey

    import cssic.imagery as imagery

    class RejectingSource:
        def bulk_order(self, gdf, workspace):
            raise InvalidAPIKey("Please provide valid credentials.")

    monkeypatch.setattr(imagery, "get_image_source", lambda *a, **kw: RejectingSource())
    cfg = gather_config(build_parser().parse_args(["gather", "-s", "p", "--rgb"]))
    with pytest.raises(ValueError, match="PLANET_API_KEY"):
        gather_module._run_planet(cfg, Credentials(), object(), None)


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
    import cssic.imagery as imagery

    monkeypatch.setattr(deps_module, "BACKEND_REQUIREMENTS", {})
    monkeypatch.setattr(imagery, "get_image_source", lambda *a, **kw: source)
    cfg = gather_config(build_parser().parse_args(list(argv)))
    return run_gather(cfg, Credentials(), collection(tmp_path))


def test_gather_reports_a_missing_backend_instead_of_an_import_traceback(
    tmp_path, monkeypatch, capsys, missing_stac_backend
):
    collection(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["gather", "-s", "stac", "--rgb"]) == 2
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


# --- the fallback sampler ---------------------------------------------------


class WindowSource:
    """``find_scenes`` over a fixed scene list, honouring the window."""

    def __init__(self, scenes):
        self.scenes = list(scenes)

    def find_scenes(self, aoi, start, end, limit=None):
        return [scene for scene in self.scenes if start <= scene.acquired <= end]


def test_the_fallback_sampler_keeps_the_least_cloudy_scene_per_window():
    """SentinelHub has no ``sample_scenes``; the pipeline does the ranking for it."""
    from datetime import date as _date

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


def test_bad_sentinel_hub_credentials_are_one_error_line(monkeypatch):
    """An oauthlib traceback after 25 SEARCH FAILED lines is not a user error message."""
    pytest.importorskip("sentinelhub")
    import sentinelhub
    from oauthlib.oauth2.rfc6749.errors import InvalidClientError

    def reject(config=None):
        raise InvalidClientError(description="Invalid client credentials")

    monkeypatch.setattr(sentinelhub, "SentinelHubSession", reject)
    source = type("Source", (), {"config": object()})()

    with pytest.raises(ValueError, match="SH_CLIENT_ID"):
        gather_module._check_sentinel_credentials(source)


def test_a_transport_failure_is_not_relabelled_as_bad_credentials(monkeypatch):
    """Only the OAuth rejection is a credential error; a socket timeout is not."""
    pytest.importorskip("sentinelhub")
    import sentinelhub

    def die(config=None):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(sentinelhub, "SentinelHubSession", die)
    with pytest.raises(TimeoutError):
        gather_module._check_sentinel_credentials(type("S", (), {"config": object()})())


def test_gather_exits_two_when_sentinel_hub_rejects_the_keys(
    tmp_path, monkeypatch, capsys, no_backend_check
):
    pytest.importorskip("sentinelhub")
    import sentinelhub
    from oauthlib.oauth2.rfc6749.errors import InvalidClientError

    import cssic.imagery as imagery

    def reject(config=None):
        raise InvalidClientError(description="Invalid client credentials")

    monkeypatch.setattr(sentinelhub, "SentinelHubSession", reject)
    monkeypatch.setattr(
        imagery, "get_image_source", lambda *a, **kw: type("S", (), {"config": object()})()
    )
    collection(tmp_path)
    monkeypatch.chdir(tmp_path)

    assert main(["gather", "-s", "s", "--rgb"]) == 2
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


@pytest.mark.parametrize("source_name", ["stac", "sentinel"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start", "not-a-date"),
        ("start", None),
        ("start", "2018-07-01"),
        ("geometry", None),
        ("geometry", "empty"),
    ],
)
def test_gather_skips_invalid_saved_site_and_writes_the_next(
    tmp_path, monkeypatch, capsys, no_backend_check, source_name, field, value
):
    from PIL import Image
    from shapely.geometry import Polygon

    import cssic.imagery as imagery

    ws = collection(tmp_path)
    gdf = ws.load_collection()
    gdf.at[0, field] = Polygon() if value == "empty" else value
    ws.save_collection(gdf)

    class Source:
        def find_scenes(self, aoi, start, end):
            assert aoi.bounds[0] > 1  # only the second site's input is usable
            return [FakeScene(acquired=start)]

        def fetch(self, scene, aoi, band):
            return chip_array()

    monkeypatch.setattr(imagery, "get_image_source", lambda *args, **kwargs: Source())
    cfg = gather_config(
        build_parser().parse_args(["gather", "-s", source_name, "--rgb", "-n", "1"])
    )
    assert run_gather(cfg, Credentials(), ws) == 0
    out = capsys.readouterr().out
    assert "10-0: INVALID SITE:" in out
    assert "20-1: wrote 1 of 1 requested samples" in out
    assert list(ws.images_dir("10-0", "sentinel", "rgb").glob("*.png")) == []
    paths = list(ws.images_dir("20-1", "sentinel", "rgb").glob("*.png"))
    assert len(paths) == 1
    with Image.open(paths[0]) as image:
        assert image.size == (8, 8)


def test_gather_reads_and_writes_only_the_selected_workspace(
    tmp_path, monkeypatch, no_backend_check
):
    from pathlib import Path

    from PIL import Image

    import cssic.imagery as imagery

    ws = collection(tmp_path / "selected")
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)

    class Source:
        def find_scenes(self, aoi, start, end):
            return [FakeScene(acquired=start)]

        def fetch(self, scene, aoi, band):
            return chip_array()

    monkeypatch.setattr(imagery, "get_image_source", lambda *args, **kwargs: Source())
    assert main(["--workspace", "../selected", "gather", "-s", "stac", "--rgb", "-n", "1"]) == 0
    for chain_id in ("10-0", "20-1"):
        paths = list(ws.images_dir(chain_id, "sentinel", "rgb").glob("*.png"))
        assert len(paths) == 1
        with Image.open(paths[0]) as image:
            assert image.size == (8, 8)
            assert image.getpixel((0, 0)) == (0, 1, 2)
    assert list(caller.iterdir()) == []
    assert Path.cwd() == caller
