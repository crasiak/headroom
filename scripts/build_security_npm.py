#!/usr/bin/env python3
"""Build CRA-1534's npm fork from verified published code and a reviewed clean lock.

Never modifies an installed npm. --prepare creates disposable source for lock
maintenance; normal builds require the committed lock's exact digest, disable
lifecycle scripts, and package only freshly installed dependencies.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import tarfile
import tempfile
import urllib.request
from copy import deepcopy
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
RECIPE_DIR = ROOT / "e2e" / "wrap" / "tools" / "npm-security"
PROVENANCE = "npm-security-provenance.json"
MAX_ARCHIVE_BYTES = 24 * 1024 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_integrity(data: bytes, expected: str) -> None:
    actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if not hmac.compare_digest(actual, expected):
        raise ValueError("upstream archive integrity mismatch")


def _archive_parts(member: tarfile.TarInfo) -> tuple[str, ...]:
    parts = PurePosixPath(member.name).parts
    if (
        not parts
        or parts[0] != "package"
        or ".." in parts
        or "\\" in member.name
        or ":" in member.name
    ):
        raise ValueError(f"unsafe archive path: {member.name}")
    if not (member.isfile() or member.isdir()):
        raise ValueError(f"archive link or special entry rejected: {member.name}")
    return parts


def extract_source(archive: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("source destination must not already exist")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 20000 or sum(m.size for m in members) > MAX_SOURCE_BYTES:
            raise ValueError("upstream archive exceeds source budget")
        validated = [(member, _archive_parts(member)) for member in members]
        destination.mkdir(parents=True)
        for member, parts in validated:
            # Rebuild the whole dependency payload, never patch hidden old copies.
            if "node_modules" in parts or parts[-1] in ("package-lock.json", "npm-shrinkwrap.json"):
                continue
            target = destination.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                stream = tar.extractfile(member)
                if stream is None:
                    raise ValueError(f"missing archive data: {member.name}")
                with target.open("xb") as output:
                    output.write(stream.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)


def patch_manifest(upstream: dict, recipe: dict) -> dict:
    if upstream.get("name") != "npm" or upstream.get("version") != recipe["upstream_version"]:
        raise ValueError("unexpected upstream package identity")
    result = deepcopy(upstream)
    result["version"] = recipe["version"]
    for name, version in recipe["security_versions"].items():
        if name in result["dependencies"]:
            result["dependencies"][name] = version
    result["overrides"] = dict(recipe["security_versions"])
    # Published docs are already built. Do not execute upstream development hooks
    # or resolve unpublished workspaces/dev packages while building a CLI payload.
    for key in ("scripts", "workspaces", "devDependencies", "tap", "templateOSS"):
        result.pop(key, None)
    result["files"].append(PROVENANCE)
    return result


def _version(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"unexpected security dependency version: {value}")
    return tuple(int(part) for part in value.split("."))


def verify_packed_artifact(archive: Path, recipe: dict) -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {name: [] for name in recipe["security_versions"]}
    with tarfile.open(archive, "r:gz") as tar:
        root = json.load(tar.extractfile("package/package.json"))
        if root.get("name") != "npm" or root.get("version") != recipe["version"]:
            raise ValueError("packed artifact has incorrect fork identity")
        for member in tar:
            _archive_parts(member)
            if not member.isfile() or not member.name.endswith("/package.json"):
                continue
            package = json.load(tar.extractfile(member))
            name = package.get("name")
            if name not in versions:
                continue
            version = package["version"]
            if _version(version) < _version(recipe["security_versions"][name]):
                raise ValueError(f"packed artifact contains vulnerable {name}: {member.name}")
            versions[name].append(version)
    for name, found in versions.items():
        if not found:
            raise ValueError(f"packed artifact is missing {name}")
        versions[name] = sorted(found)
    return versions


def verify_packed_lock(archive: Path, lock_path: Path) -> None:
    """Bind every shipped package, not just four known fixes, to the audited lock."""
    lock = json.loads(lock_path.read_text())
    expected = {name: value["version"] for name, value in lock["packages"].items() if name}
    actual: dict[str, str] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            _archive_parts(member)
            if not member.isfile() or not member.name.endswith("/package.json"):
                continue
            relative = member.name.removeprefix("package/").removesuffix("/package.json")
            if not relative.startswith("node_modules/"):
                continue
            tail = relative.rsplit("node_modules/", 1)[-1]
            if len(tail.split("/")) != (2 if tail.startswith("@") else 1):
                continue  # e.g. glob/dist/commonjs/package.json is not another module
            package = json.load(tar.extractfile(member))
            if relative in actual or package.get("name") != tail:
                raise ValueError(f"invalid packed dependency inventory: {relative}")
            actual[relative] = package["version"]
    if actual != expected:
        raise ValueError("packed dependency inventory differs from the audited lock")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def verify_toolchain(archive: Path, node: Path, npm_cli: Path, recipe: dict) -> str:
    expected_digest = recipe["node_distributions"].get(archive.name)
    if expected_digest is None or digest(archive) != expected_digest:
        raise ValueError("untrusted toolchain distribution digest")
    prefix = archive.name.removesuffix(".tar.gz")
    root = node.parent.parent
    if root.name != prefix or npm_cli != root / "lib/node_modules/npm/bin/npm-cli.js":
        raise ValueError("toolchain paths do not match the verified distribution")
    expected_paths: set[Path] = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != prefix or ".." in parts or "\\" in member.name:
                raise ValueError("unsafe toolchain archive path")
            relative = Path(*parts[1:])
            if relative == Path("."):
                continue
            expected_paths.add(relative)
            expected_paths.update(p for p in relative.parents if p != Path("."))
            actual = root / relative
            if member.issym():
                if not actual.is_symlink() or os.readlink(actual) != member.linkname:
                    raise ValueError(f"toolchain link mismatch: {relative}")
                if not actual.resolve().is_relative_to(root):
                    raise ValueError(f"toolchain link escapes distribution: {relative}")
            elif member.isdir():
                if actual.is_symlink() or not actual.is_dir():
                    raise ValueError(f"toolchain directory mismatch: {relative}")
            elif member.isfile():
                source = tar.extractfile(member)
                if source is None or actual.is_symlink() or not actual.is_file():
                    raise ValueError(f"toolchain file missing: {relative}")
                if digest(actual) != hashlib.file_digest(source, "sha256").hexdigest():
                    raise ValueError(f"toolchain content mismatch: {relative}")
                if actual.stat().st_mode & 0o111 != member.mode & 0o111:
                    raise ValueError(f"toolchain executable mode mismatch: {relative}")
            else:
                raise ValueError("toolchain archive contains a special entry")
    if {p.relative_to(root) for p in root.rglob("*")} != expected_paths:
        raise ValueError("toolchain contains unexpected or missing paths")
    return expected_digest


def fresh_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise ValueError(f"workspace/output must be fresh: {path}") from error


def prepare(work: Path, recipe: dict, archive: Path | None) -> Path:
    fresh_directory(work)
    if archive is None:
        archive = work / "upstream.tgz"
        with urllib.request.urlopen(recipe["upstream_url"], timeout=60) as response:
            data = response.read(MAX_ARCHIVE_BYTES + 1)
        if len(data) > MAX_ARCHIVE_BYTES:
            raise ValueError("upstream download exceeds archive budget")
        verify_integrity(data, recipe["upstream_integrity"])
        with archive.open("xb") as output:
            output.write(data)
    else:
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("upstream archive exceeds download budget")
        verify_integrity(archive.read_bytes(), recipe["upstream_integrity"])
    package = work / "package"
    extract_source(archive, package)
    manifest = json.loads((package / "package.json").read_text())
    write_json(package / "package.json", patch_manifest(manifest, recipe))
    return package


def isolated_env(work: Path, node: Path) -> dict[str, str]:
    if work.is_symlink() or not work.is_dir():
        raise ValueError("environment requires an owned workspace directory")
    home = Path(tempfile.mkdtemp(prefix="env-", dir=work))
    user_config = home / "user.npmrc"
    global_config = home / "global.npmrc"
    # npm rejects loading the same physical file twice (including /dev/null).
    for config in (user_config, global_config):
        config.write_text("", encoding="utf-8")
    # No native npmrc, credentials, NODE_OPTIONS, live HOME, or inherited npm flags.
    return {
        "PATH": os.pathsep.join((str(node.parent), os.defpath, "/usr/sbin", "/sbin")),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "NPM_CONFIG_USERCONFIG": str(user_config),
        "NPM_CONFIG_GLOBALCONFIG": str(global_config),
        "NPM_CONFIG_CACHE": str(home / "cache"),
        "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        # Hoisting this cross-major override closure loops replacing root nodes.
        # A reviewed nested lock avoids that resolver path without hiding copies.
        "NPM_CONFIG_INSTALL_STRATEGY": "nested",
    }


def check_lock(lock_path: Path, manifest: dict, recipe: dict) -> None:
    if digest(lock_path) != recipe.get("lock_sha256"):
        raise ValueError("committed security lock digest mismatch")
    lock = json.loads(lock_path.read_text())
    root = lock["packages"][""]
    if (
        root.get("version") != recipe["version"]
        or root.get("dependencies") != manifest["dependencies"]
    ):
        raise ValueError("security lock root differs from patched manifest")
    for name, package in lock["packages"].items():
        if not name:
            continue
        if not package.get("resolved", "").startswith("https://registry.npmjs.org/"):
            raise ValueError(f"non-public-registry dependency in security lock: {name}")
        if not package.get("integrity", "").startswith("sha512-"):
            raise ValueError(f"missing SHA512 dependency integrity: {name}")


def build(
    work: Path,
    output: Path,
    recipe_path: Path,
    node: Path,
    npm_cli: Path,
    node_distribution: Path,
    archive: Path | None,
) -> dict:
    recipe = json.loads(recipe_path.read_text())
    toolchain_digest = verify_toolchain(node_distribution, node, npm_cli, recipe)
    if output == work or output.is_relative_to(work):
        raise ValueError("artifact output must be outside the build workspace")
    package = prepare(work, recipe, archive)
    env = isolated_env(work, node)
    node_version = subprocess.check_output(
        [node, "--version"], env=env, text=True, timeout=30
    ).strip()
    npm_version = subprocess.check_output(
        [node, npm_cli, "--version"], env=env, text=True, timeout=30
    ).strip()
    if node_version != recipe["node"] or npm_version != recipe["bootstrap_npm"]:
        raise ValueError("build toolchain differs from the reviewed recipe")
    lock = recipe_path.with_name("package-lock.json")
    manifest = json.loads((package / "package.json").read_text())
    if manifest != json.loads(recipe_path.with_name("package.json").read_text()):
        raise ValueError("patched manifest differs from reviewed source")
    if recipe["install_strategy"] != env["NPM_CONFIG_INSTALL_STRATEGY"]:
        raise ValueError("install strategy differs from reviewed lock recipe")
    check_lock(lock, manifest, recipe)
    (package / "package-lock.json").write_bytes(lock.read_bytes())
    provenance = {
        key: recipe[key]
        for key in (
            "upstream_version",
            "upstream_revision",
            "upstream_url",
            "upstream_integrity",
            "version",
            "security_versions",
            "node",
            "bootstrap_npm",
            "node_distributions",
            "install_strategy",
            "lock_sha256",
        )
    }
    provenance["recipe_sha256"] = digest(recipe_path)
    provenance["builder_sha256"] = digest(Path(__file__))
    write_json(package / PROVENANCE, provenance)
    subprocess.run(
        [node, npm_cli, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        cwd=package,
        env=env,
        check=True,
        timeout=300,
    )
    fresh_directory(output)
    filename = f"npm-{recipe['version']}.tgz"
    packed = output / filename
    if packed.exists():
        raise ValueError("refusing to overwrite a retained artifact")
    pack_result = subprocess.run(
        [node, npm_cli, "pack", "--ignore-scripts", "--json", "--pack-destination", output],
        cwd=package,
        env=env,
        check=True,
        timeout=300,
        stdout=subprocess.PIPE,
        text=True,
    )
    (work / "pack.json").write_text(pack_result.stdout, encoding="utf-8")
    versions = verify_packed_artifact(packed, recipe)
    verify_packed_lock(packed, lock)
    result = {
        "filename": filename,
        "version": recipe["version"],
        "sha256": digest(packed),
        "integrity": "sha512-"
        + base64.b64encode(hashlib.sha512(packed.read_bytes()).digest()).decode("ascii"),
        "recipe_sha256": digest(recipe_path),
        "builder_sha256": digest(Path(__file__)),
        "lock_sha256": recipe["lock_sha256"],
        "upstream_revision": recipe["upstream_revision"],
        "node": node_version,
        "bootstrap_npm": npm_version,
        "node_distribution_sha256": toolchain_digest,
        "packed_security_versions": versions,
    }
    write_json(output / "artifact.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, default=RECIPE_DIR / "recipe.json")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path)
    parser.add_argument(
        "--prepare", action="store_true", help="prepare disposable source for lock maintenance only"
    )
    parser.add_argument("--node", type=Path)
    parser.add_argument("--npm-cli", type=Path)
    parser.add_argument(
        "--node-distribution",
        type=Path,
        help="reviewed Node archive; verifies the entire bootstrap tree",
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.work_dir.is_symlink() or (args.output_dir and args.output_dir.is_symlink()):
        parser.error("workspace/output cannot be a symlink")
    work = args.work_dir.resolve()
    if args.prepare:
        recipe = json.loads(args.recipe.read_text())
        print(prepare(work, recipe, args.source_archive))
    else:
        if any(
            value is None
            for value in (args.node, args.npm_cli, args.node_distribution, args.output_dir)
        ):
            parser.error("build requires --node, --npm-cli, --node-distribution and --output-dir")
        print(
            json.dumps(
                build(
                    work,
                    args.output_dir.resolve(),
                    args.recipe.resolve(),
                    args.node.resolve(),
                    args.npm_cli.resolve(),
                    args.node_distribution.resolve(),
                    args.source_archive,
                ),
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
