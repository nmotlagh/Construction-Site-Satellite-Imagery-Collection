"""Optional backends are checked before any network or credential work."""

from __future__ import annotations

import pytest

import cssic.deps as deps_module


def test_require_backend_names_the_module_and_the_extra(monkeypatch):
    monkeypatch.setattr(
        deps_module, "BACKEND_REQUIREMENTS", {"stac": ("stac", ("no_such_module_xyz",))}
    )
    with pytest.raises(ValueError) as exc:
        deps_module.require_backend("stac")

    message = str(exc.value)
    assert "no_such_module_xyz" in message
    assert "uv sync --extra stac" in message


def test_require_backend_is_quiet_when_everything_is_installed(monkeypatch):
    monkeypatch.setattr(deps_module, "BACKEND_REQUIREMENTS", {"stac": ("stac", ("json",))})
    assert deps_module.require_backend("stac") is None


def test_an_unknown_source_has_no_requirements():
    assert deps_module.require_backend("nothing-like-this") is None


def test_the_real_backend_requirements_name_real_extras():
    extras = {extra for extra, _modules in deps_module.BACKEND_REQUIREMENTS.values()}
    assert extras == {"stac", "sentinelhub", "planet"}


def test_the_install_hint_names_a_command_that_can_actually_run(monkeypatch):
    """The distribution is not on PyPI, so ``pip install 'cssic[stac]'`` is a dead end."""
    monkeypatch.setattr(
        deps_module, "BACKEND_REQUIREMENTS", {"stac": ("stac", ("no_such_module_xyz",))}
    )
    with pytest.raises(ValueError) as exc:
        deps_module.require_backend("stac")

    message = str(exc.value)
    assert "uv sync --extra stac" in message
    assert "pip install -e '.[stac]'" in message
    assert "cssic[stac]" not in message


def test_a_missing_pyosmium_reports_a_real_install_command(monkeypatch):
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    with pytest.raises(ValueError) as exc:
        deps_module.require_history_backend("osmium")
    assert "uv sync --extra osmium" in str(exc.value)


def test_require_history_backend_lets_ohsome_through():
    assert deps_module.require_history_backend("ohsome") is None
