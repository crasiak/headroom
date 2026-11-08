"""CRA-1534: shipping locks must enforce the approved security/support floors."""

import json
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


@pytest.mark.parametrize(
    ("name", "vulnerable", "fixed"),
    [("filelock", "4.2.0", "4.2.1"), ("litellm", "1.101.0", "1.101.3")],
)
def test_shipping_metadata_enforces_floors_even_when_installed_without_uv(
    name: str, vulnerable: str, fixed: str
) -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    requirements = [Requirement(value) for value in data["project"]["dependencies"]]
    matching = [requirement for requirement in requirements if requirement.name == name]
    assert matching, f"pip metadata lacks the security floor for {name}"
    for requirement in matching:
        assert vulnerable not in requirement.specifier
        assert fixed in requirement.specifier


@pytest.mark.parametrize(("name", "floor"), [("filelock", "4.2.1"), ("litellm", "1.101.3")])
def test_frozen_lock_contains_only_patched_target_versions(name: str, floor: str) -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    assert "3.10" not in SpecifierSet(lock["requires-python"])
    assert "3.11" in SpecifierSet(lock["requires-python"])
    versions = [Version(pkg["version"]) for pkg in lock["package"] if pkg["name"] == name]
    assert versions
    assert all(version >= Version(floor) for version in versions)


def test_wrap_consumer_keeps_cli_pins_and_scopes_complete_npm_rebuild() -> None:
    directory = ROOT / "e2e/wrap/tools"
    manifest = json.loads((directory / "package.json").read_text())
    recipe = json.loads((directory / "npm-security/recipe.json").read_text())
    artifact = json.loads((directory / "npm-security/artifact.json").read_text())
    assert manifest["dependencies"]["@openai/codex"] == "0.153.4"
    assert manifest["dependencies"]["openclaw"] == "2026.10.7"
    url = manifest["dependencies"]["npm"]
    assert url.startswith("https://github.com/crasiak/headroom/releases/download/")
    assert url.endswith("/" + artifact["filename"])
    assert manifest["overrides"]["npm"] == {".": "$npm", **recipe["security_versions"]}
    assert manifest["overrides"]["minimatch"] == recipe["security_versions"]["minimatch"]
    assert manifest["overrides"]["undici"] == "8.12.3"
    assert manifest["overrides"]["zod"] == "4.6.7"


def test_wrap_lock_enumerates_every_packed_npm_dependency_and_binds_integrity() -> None:
    directory = ROOT / "e2e/wrap/tools"
    manifest = json.loads((directory / "package.json").read_text())
    consumer = json.loads((directory / "package-lock.json").read_text())["packages"]
    artifact = json.loads((directory / "npm-security/artifact.json").read_text())
    audited = json.loads((directory / "npm-security/package-lock.json").read_text())["packages"]
    installed = consumer["node_modules/npm"]
    assert installed["version"] == artifact["version"]
    assert installed["resolved"] == manifest["dependencies"]["npm"]
    assert installed["integrity"] == artifact["integrity"]
    expected = {name: package["version"] for name, package in audited.items() if name}
    actual = {
        name.removeprefix("node_modules/npm/"): package["version"]
        for name, package in consumer.items()
        if name.startswith("node_modules/npm/")
    }
    assert actual == expected


def test_security_workflow_audits_every_maintained_npm_lock() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/security.yml").read_text())
    audited = set(workflow["jobs"]["npm-audit"]["strategy"]["matrix"]["directory"])
    maintained = {
        path.parent.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("package-lock.json")
        if "node_modules" not in path.parts and ".venv" not in path.parts
    }
    assert maintained <= audited, f"maintained locks missing from audit: {maintained - audited}"
