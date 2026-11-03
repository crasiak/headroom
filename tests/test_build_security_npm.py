"""The security bundle builder must reject corrupt inputs and hidden old copies."""

import base64
import hashlib
import importlib.util
import io
import json
import tarfile
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_security_npm.py"
spec = importlib.util.spec_from_file_location("build_security_npm", SCRIPT)
assert spec is not None and spec.loader is not None
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

RECIPE = {
    "upstream_version": "12.2.1",
    "version": "12.2.1-crasiak.1",
    "security_versions": {
        "glob": "14.0.3",
        "minimatch": "11.0.4",
        "ip-address": "10.7.5",
        "undici": "6.29.1",
    },
}
UPSTREAM = {
    "name": "npm",
    "version": "12.2.1",
    "license": "Artistic-2.0",
    "bin": {"npm": "bin/npm-cli.js", "npx": "bin/npx-cli.js"},
    "dependencies": {"glob": "^13.0.6", "minimatch": "^10.2.5", "chalk": "^5.6.2"},
    "bundleDependencies": ["glob", "minimatch", "chalk"],
    "files": ["bin/", "lib/"],
    "scripts": {"prepack": "unsafe-network-script"},
    "workspaces": ["docs"],
    "devDependencies": {"tap": "^21.0.0"},
}


def archive(path: Path, files: dict[str, bytes], symlink: str | None = None) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
        if symlink is not None:
            member = tarfile.TarInfo("package/lib/link")
            member.type = tarfile.SYMTYPE
            member.linkname = symlink
            tar.addfile(member)
    return path


def test_artifact_reference_is_bound_to_current_reviewed_build_inputs() -> None:
    directory = SCRIPT.parents[1] / "e2e/wrap/tools/npm-security"
    reference = json.loads((directory / "artifact.json").read_text())
    assert reference["builder_sha256"] == builder.digest(SCRIPT)
    assert reference["recipe_sha256"] == builder.digest(directory / "recipe.json")
    assert reference["lock_sha256"] == builder.digest(directory / "package-lock.json")


def test_corrupt_upstream_archive_is_rejected_before_extraction() -> None:
    with pytest.raises(ValueError, match="integrity"):
        builder.verify_integrity(b"tampered", "sha512-AAAA")


@pytest.mark.parametrize("name", ["package/../escape", "/tmp/escape", "other/root"])
def test_archive_cannot_write_outside_its_package(tmp_path: Path, name: str) -> None:
    source = archive(tmp_path / "source.tgz", {name: b"bad"})
    with pytest.raises(ValueError, match="path"):
        builder.extract_source(source, tmp_path / "out")
    assert not (tmp_path / "escape").exists()


def test_archive_links_are_rejected(tmp_path: Path) -> None:
    source = archive(tmp_path / "source.tgz", {}, symlink="../../escape")
    with pytest.raises(ValueError, match="link"):
        builder.extract_source(source, tmp_path / "out")


def test_old_bundled_payload_is_not_an_input_to_clean_install(tmp_path: Path) -> None:
    source = archive(
        tmp_path / "source.tgz",
        {"package/LICENSE": b"license", "package/node_modules/undici/old.js": b"old"},
    )
    builder.extract_source(source, tmp_path / "out")
    assert (tmp_path / "out" / "LICENSE").read_bytes() == b"license"
    assert not (tmp_path / "out" / "node_modules").exists()


def test_build_environment_cannot_read_live_config_or_double_load_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODE_OPTIONS", "--require=/live/injection.js")
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://private.invalid/")
    env = builder.isolated_env(tmp_path, Path("/tools/bin/node"))
    assert "NODE_OPTIONS" not in env
    assert env["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org/"
    assert env["NPM_CONFIG_USERCONFIG"] != env["NPM_CONFIG_GLOBALCONFIG"]
    assert Path(env["NPM_CONFIG_USERCONFIG"]).read_text() == ""
    assert Path(env["NPM_CONFIG_GLOBALCONFIG"]).read_text() == ""


def test_existing_workspace_is_rejected_before_source_writes(tmp_path: Path) -> None:
    work = tmp_path / "existing"
    work.mkdir()
    marker = work / "retained"
    marker.write_text("keep")
    source = archive(
        tmp_path / "source.tgz", {"package/package.json": json.dumps(UPSTREAM).encode()}
    )
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(source.read_bytes()).digest()).decode()
    with pytest.raises(ValueError, match="workspace"):
        builder.prepare(work, {**RECIPE, "upstream_integrity": integrity}, source)
    assert marker.read_text() == "keep"
    assert not (work / "package").exists()


def test_preexisting_config_and_cache_symlinks_cannot_redirect_writes(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "user.npmrc").write_text("keep-user")
    (protected / "global.npmrc").write_text("keep-global")
    work = tmp_path / "work"
    work.mkdir()
    (work / "home").symlink_to(protected, target_is_directory=True)
    (work / "cache").symlink_to(protected, target_is_directory=True)
    env = builder.isolated_env(work, Path("/tools/bin/node"))
    assert (protected / "user.npmrc").read_text() == "keep-user"
    assert (protected / "global.npmrc").read_text() == "keep-global"
    assert Path(env["NPM_CONFIG_CACHE"]).parent == Path(env["HOME"])
    assert Path(env["HOME"]) != work / "home"


def toolchain_fixture(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    files = {
        "node-test/bin/node": b"trusted-node",
        "node-test/lib/node_modules/npm/bin/npm-cli.js": b"trusted-cli",
        "node-test/lib/node_modules/npm/node_modules/dep/index.js": b"trusted-dependency",
    }
    source = archive(tmp_path / "node-test.tar.gz", files)
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    recipe = {"node_distributions": {source.name: builder.digest(source)}}
    root = tmp_path / "node-test"
    return source, root / "bin/node", root / "lib/node_modules/npm/bin/npm-cli.js", recipe


def test_bootstrap_dependency_tampering_is_rejected_not_just_version_labels(tmp_path: Path) -> None:
    source, node, cli, recipe = toolchain_fixture(tmp_path)
    dependency = cli.parent.parent / "node_modules/dep/index.js"
    dependency.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="toolchain"):
        builder.verify_toolchain(source, node, cli, recipe)


def test_extra_bootstrap_file_cannot_shadow_a_verified_module(tmp_path: Path) -> None:
    source, node, cli, recipe = toolchain_fixture(tmp_path)
    (cli.parent / "unexpected.js").write_text("injection")
    with pytest.raises(ValueError, match="toolchain"):
        builder.verify_toolchain(source, node, cli, recipe)


def test_complete_bootstrap_distribution_is_bound_to_trusted_archive(tmp_path: Path) -> None:
    source, node, cli, recipe = toolchain_fixture(tmp_path)
    assert builder.verify_toolchain(source, node, cli, recipe) == builder.digest(source)


def test_wrong_upstream_identity_is_rejected() -> None:
    wrong = {**UPSTREAM, "version": "12.2.0"}
    with pytest.raises(ValueError, match="identity"):
        builder.patch_manifest(wrong, RECIPE)


def test_patched_manifest_preserves_cli_license_and_untouched_dependencies() -> None:
    before = deepcopy(UPSTREAM)
    patched = builder.patch_manifest(UPSTREAM, RECIPE)
    assert UPSTREAM == before
    assert patched["name"] == "npm"
    assert patched["version"] == RECIPE["version"]
    assert patched["license"] == UPSTREAM["license"]
    assert patched["bin"] == UPSTREAM["bin"]
    assert patched["dependencies"]["chalk"] == UPSTREAM["dependencies"]["chalk"]
    assert patched["dependencies"]["glob"] == "14.0.3"
    assert patched["dependencies"]["minimatch"] == "11.0.4"
    assert patched["overrides"] == RECIPE["security_versions"]
    assert "scripts" not in patched
    assert "workspaces" not in patched
    assert "devDependencies" not in patched
    assert "npm-security-provenance.json" in patched["files"]


def test_modified_security_lock_is_rejected(tmp_path: Path) -> None:
    lock = tmp_path / "package-lock.json"
    lock.write_text("{}")
    with pytest.raises(ValueError, match="digest"):
        builder.check_lock(lock, builder.patch_manifest(UPSTREAM, RECIPE), RECIPE)


def test_private_registry_or_unhashed_dependency_is_rejected(tmp_path: Path) -> None:
    manifest = builder.patch_manifest(UPSTREAM, RECIPE)
    data = {
        "packages": {
            "": {"version": RECIPE["version"], "dependencies": manifest["dependencies"]},
            "node_modules/example": {
                "resolved": "https://private.invalid/x.tgz",
                "integrity": "sha512-AAAA",
            },
        }
    }
    lock = tmp_path / "package-lock.json"
    builder.write_json(lock, data)
    recipe = {**RECIPE, "lock_sha256": builder.digest(lock)}
    with pytest.raises(ValueError, match="non-public-registry"):
        builder.check_lock(lock, manifest, recipe)
    data["packages"]["node_modules/example"] = {
        "resolved": "https://registry.npmjs.org/example/-/example.tgz"
    }
    builder.write_json(lock, data)
    recipe["lock_sha256"] = builder.digest(lock)
    with pytest.raises(ValueError, match="SHA512"):
        builder.check_lock(lock, manifest, recipe)


def packed_files() -> dict[str, bytes]:
    root = {"name": "npm", "version": RECIPE["version"]}
    files = {"package/package.json": json.dumps(root).encode()}
    for name, version in RECIPE["security_versions"].items():
        files[f"package/node_modules/{name}/package.json"] = json.dumps(
            {"name": name, "version": version}
        ).encode()
    return files


def test_packed_dependency_inventory_must_match_the_entire_audited_lock(tmp_path: Path) -> None:
    files = packed_files()
    lock = tmp_path / "package-lock.json"
    packages = {"": {"version": RECIPE["version"]}}
    for name, version in RECIPE["security_versions"].items():
        packages[f"node_modules/{name}"] = {"version": version}
    builder.write_json(lock, {"packages": packages})
    source = archive(tmp_path / "valid.tgz", files)
    builder.verify_packed_lock(source, lock)
    files["package/node_modules/unreviewed/package.json"] = (
        b'{"name":"unreviewed","version":"1.0.0"}'
    )
    source = archive(tmp_path / "extra.tgz", files)
    with pytest.raises(ValueError, match="inventory"):
        builder.verify_packed_lock(source, lock)


def test_nested_vulnerable_copy_is_rejected_even_if_direct_copy_is_fixed(tmp_path: Path) -> None:
    files = packed_files()
    files["package/node_modules/parent/node_modules/undici/package.json"] = json.dumps(
        {"name": "undici", "version": "6.29.0"}
    ).encode()
    source = archive(tmp_path / "packed.tgz", files)
    with pytest.raises(ValueError, match="undici"):
        builder.verify_packed_artifact(source, RECIPE)


def test_missing_required_security_package_is_rejected(tmp_path: Path) -> None:
    files = packed_files()
    del files["package/node_modules/glob/package.json"]
    source = archive(tmp_path / "packed.tgz", files)
    with pytest.raises(ValueError, match="glob"):
        builder.verify_packed_artifact(source, RECIPE)


def test_packed_artifact_keeps_honest_fork_identity_and_reports_all_copies(tmp_path: Path) -> None:
    source = archive(tmp_path / "packed.tgz", packed_files())
    versions = builder.verify_packed_artifact(source, RECIPE)
    assert versions == {name: [version] for name, version in RECIPE["security_versions"].items()}
