from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import actual_clerk


def test_the_package_version_matches_the_project_metadata():
    project = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert actual_clerk._installed_version() == project["project"]["version"]


def test_the_container_may_stamp_its_own_version(monkeypatch):
    monkeypatch.setenv("ACTUAL_CLERK_VERSION", "9.9.9")
    reloaded = importlib.reload(actual_clerk)
    try:
        assert reloaded.__version__ == "9.9.9"
    finally:
        monkeypatch.delenv("ACTUAL_CLERK_VERSION")
        importlib.reload(actual_clerk)
