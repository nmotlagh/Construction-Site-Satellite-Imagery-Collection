"""Shared fixtures for the command tests."""

from __future__ import annotations

import pytest

import cssic.deps as deps_module


@pytest.fixture
def missing_stac_backend(monkeypatch):
    """Make the ``stac`` backend require a module that cannot be imported."""
    monkeypatch.setattr(
        deps_module, "BACKEND_REQUIREMENTS", {"stac": ("stac", ("no_such_module_xyz",))}
    )


@pytest.fixture
def no_backend_check(monkeypatch):
    """Disable the optional-dependency preflight entirely."""
    monkeypatch.setattr(deps_module, "BACKEND_REQUIREMENTS", {})
