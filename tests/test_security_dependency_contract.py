"""CRA-1534: shipping locks must enforce the approved security/support floors."""

from pathlib import Path

import pytest
import tomllib
import yaml
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def test_python_310_is_rejected_before_resolving_modern_filelock() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    supported = SpecifierSet(project["requires-python"])
    assert "3.10" not in supported
    assert "3.11" in supported
    assert "Programming Language :: Python :: 3.10" not in project["classifiers"]


@pytest.mark.parametrize(
    ("name", "vulnerable", "fixed"),
    [("filelock", "4.2.0", "4.2.1"), ("litellm", "1.101.0", "1.101.3")],
)
def test_security_constraints_exclude_unpatched_versions(
    name: str, vulnerable: str, fixed: str
) -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    constraints = {
        req.name: req
        for value in data["tool"]["uv"]["constraint-dependencies"]
        if (req := Requirement(value))
    }
    assert name in constraints, f"missing persistent security floor for {name}"
    assert vulnerable not in constraints[name].specifier
    assert fixed in constraints[name].specifier


@pytest.mark.parametrize(("name", "floor"), [("filelock", "4.2.1"), ("litellm", "1.101.3")])
def test_frozen_lock_contains_only_patched_target_versions(name: str, floor: str) -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    assert "3.10" not in SpecifierSet(lock["requires-python"])
    assert "3.11" in SpecifierSet(lock["requires-python"])
    versions = [Version(pkg["version"]) for pkg in lock["package"] if pkg["name"] == name]
    assert versions
    assert all(version >= Version(floor) for version in versions)


def test_security_workflow_audits_every_maintained_npm_lock() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/security.yml").read_text())
    audited = set(workflow["jobs"]["npm-audit"]["strategy"]["matrix"]["directory"])
    maintained = {
        path.parent.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("package-lock.json")
        if "node_modules" not in path.parts and ".venv" not in path.parts
    }
    assert maintained <= audited, f"maintained locks missing from audit: {maintained - audited}"
