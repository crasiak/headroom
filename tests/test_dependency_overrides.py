"""Patched Pillow/fastembed floors are real metadata, not uv-only overrides."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def _packages():
    with open(ROOT / "uv.lock", "rb") as f:
        return tomllib.load(f)["package"]


def test_the_lock_takes_the_fixed_pillow_on_supported_pythons():
    versions = [p["version"] for p in _packages() if p["name"] == "pillow"]
    assert versions and all(Version(v) >= Version("13.0.0") for v in versions), versions


def test_security_floors_are_declared_in_install_metadata():
    with open(ROOT / "pyproject.toml", "rb") as f:
        extras = tomllib.load(f)["project"]["optional-dependencies"]
    for extra, package, floor, vulnerable in (
        ("image", "pillow", "13.0.0", "12.2.0"),
        ("tray", "pillow", "13.0.0", "12.2.0"),
        ("relevance", "fastembed", "0.9.1", "0.9.0"),
    ):
        requirement = next(Requirement(r) for r in extras[extra] if Requirement(r).name == package)
        assert floor in requirement.specifier
        assert vulnerable not in requirement.specifier
    versions = [p["version"] for p in _packages() if p["name"] == "fastembed"]
    assert versions and all(Version(v) >= Version("0.9.1") for v in versions), versions


def test_the_image_build_installs_project_metadata_without_stale_override_key():
    dockerfile = (ROOT / "Dockerfile").read_text()
    install = re.search(r"uv pip install --system (.*)", dockerfile)
    assert install and '".[${HEADROOM_EXTRAS}]"' in install.group(1)
    assert "['override-dependencies']" not in dockerfile
