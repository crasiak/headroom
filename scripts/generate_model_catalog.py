#!/usr/bin/env python3
"""Generate Headroom's pinned model catalog from the LiteLLM wheel in ``uv.lock``.

    python scripts/generate_model_catalog.py --wheel PATH/litellm-<v>-<tags>.whl
    python scripts/generate_model_catalog.py --wheel PATH --check   # exit 1 on drift

Reads LiteLLM's bundled ``model_prices_and_context_window_backup.json`` and its
license straight out of the wheel, whose SHA-256 must match an entry in
``uv.lock``. Keeps the fields ``headroom.pricing.model_catalog`` declares,
excludes reserved documentation keys, and fails on malformed source rather than
dropping a model.

It also records which keys the pinned SDK's ``cost_per_token`` probe rejects
(``ModelCatalog.unresolvable``). That step imports the installed LiteLLM in a
child process with its local cost map forced and non-loopback sockets blocked,
and refuses to run unless the installed version and bundled map are identical
to the wheel's. Output is deterministic: no timestamps or machine paths.

Refreshing prices is a reviewed dependency change: bump LiteLLM in ``uv.lock``,
rerun this, review the catalog diff, and rebuild the runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from headroom.pricing import model_catalog  # noqa: E402

GENERATOR_VERSION = 1
SOURCE_MEMBER = "litellm/model_prices_and_context_window_backup.json"
LICENSE_FILE = "LITELLM_LICENSE"


class CatalogSourceError(ValueError):
    """The LiteLLM source cannot be turned into a catalog without losing data."""


def extract_models(source: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep declared fields of every non-reserved entry, validating each value."""
    models: dict[str, dict[str, Any]] = {}
    for name in sorted(source):
        if name in model_catalog.RESERVED_KEYS:
            continue
        entry = source[name]
        if not name or not isinstance(entry, dict):
            raise CatalogSourceError(f"{name!r}: entry is not an object")
        if "aliases" in entry:
            # LiteLLM expands these into extra top-level ids at load time. The
            # pinned source has none; supporting them is a reviewed change.
            raise CatalogSourceError(f"{name!r}: 'aliases' is not supported")
        kept = {
            field: value for field, value in entry.items() if model_catalog.is_catalog_field(field)
        }
        for field, value in kept.items():
            if not model_catalog.is_valid_value(field, value):
                raise CatalogSourceError(f"{name!r}.{field}: invalid value {value!r}")
        models[name] = kept
    return models


def build_catalog(
    models: Mapping[str, Mapping[str, Any]], *, unresolvable: Iterable[str], version: str
) -> bytes:
    return model_catalog.encode_catalog(
        {
            "schema_version": model_catalog.SCHEMA_VERSION,
            "litellm_version": version,
            "models": models,
            "unresolvable": sorted(set(unresolvable)),
        }
    )


def _locked_wheels(lock_path: Path) -> dict[str, str]:
    """``{sha256: wheel filename}`` for the LiteLLM wheels pinned in ``uv.lock``."""
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    return {
        wheel["hash"].removeprefix("sha256:"): wheel["url"].rsplit("/", 1)[-1]
        for package in lock.get("package", [])
        if package.get("name") == "litellm"
        for wheel in package.get("wheels", [])
    }


_PROBE = """
import contextlib, hashlib, importlib.metadata, io, json, socket, sys
_connect = socket.socket.connect
def _loopback_only(self, address):
    if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
        raise OSError("network blocked")
    return _connect(self, address)
socket.socket.connect = _loopback_only
version, member_sha256 = sys.argv[1], sys.argv[2]
distribution = importlib.metadata.distribution("litellm")
bundled = distribution.locate_file("litellm/model_prices_and_context_window_backup.json")
if distribution.version != version:
    sys.exit(f"installed litellm {distribution.version} != wheel {version}")
if hashlib.sha256(bundled.read_bytes()).hexdigest() != member_sha256:
    sys.exit("installed litellm cost map differs from the wheel's")
import litellm
rejected = []
for name in json.load(sys.stdin):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            litellm.cost_per_token(model=name, prompt_tokens=1, completion_tokens=0)
        except Exception:
            rejected.append(name)
print(json.dumps(rejected))
"""


def probe_unresolvable(names: Iterable[str], *, version: str, member_sha256: str) -> set[str]:
    """Names the pinned SDK's resolvability probe rejects, measured offline."""
    env = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    with tempfile.TemporaryDirectory() as scratch:
        result = subprocess.run(
            [sys.executable, "-c", _PROBE, version, member_sha256],
            input=json.dumps(sorted(names)),
            capture_output=True,
            text=True,
            cwd=scratch,
            env=env,
            timeout=600,
        )
    if result.returncode != 0:
        raise CatalogSourceError(f"SDK probe failed: {result.stderr.strip()[-500:]}")
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def generate(wheel_path: Path, lock_path: Path) -> dict[str, bytes]:
    """Return ``{filename: bytes}`` for the catalog, manifest and license."""
    wheel_bytes = wheel_path.read_bytes()
    wheel_sha256 = hashlib.sha256(wheel_bytes).hexdigest()
    wheel_name = _locked_wheels(lock_path).get(wheel_sha256)
    if wheel_name is None:
        raise CatalogSourceError("wheel SHA-256 is not a LiteLLM wheel pinned in uv.lock")

    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
        dist_info = next(name.split("/", 1)[0] for name in names if ".dist-info/" in name)
        version = dist_info.removeprefix("litellm-").removesuffix(".dist-info")
        source_bytes = wheel.read(SOURCE_MEMBER)
        license_member = f"{dist_info}/licenses/LICENSE"
        license_bytes = wheel.read(license_member)
    if b"MIT License" not in license_bytes:
        raise CatalogSourceError("LiteLLM license text changed; review before shipping data")

    source = json.loads(source_bytes)
    if not isinstance(source, dict):
        raise CatalogSourceError("source is not an object")
    models = extract_models(source)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    unresolvable = probe_unresolvable(models, version=version, member_sha256=source_sha256)
    catalog = build_catalog(models, unresolvable=unresolvable, version=version)
    manifest = model_catalog.encode_catalog(
        {
            "schema_version": model_catalog.SCHEMA_VERSION,
            "catalog": {
                "file": model_catalog.CATALOG_FILE,
                "sha256": hashlib.sha256(catalog).hexdigest(),
                "bytes": len(catalog),
                "model_count": len(models),
                "unresolvable_count": len(unresolvable),
            },
            "source": {
                "package": "litellm",
                "version": version,
                "wheel": wheel_name,
                "wheel_sha256": wheel_sha256,
                "member": SOURCE_MEMBER,
                "member_sha256": source_sha256,
                "member_bytes": len(source_bytes),
                "excluded_keys": sorted(model_catalog.RESERVED_KEYS & set(source)),
                # The map lives outside LiteLLM's separately licensed enterprise/.
                "license": "MIT",
                "license_member": license_member,
                "license_file": LICENSE_FILE,
                "license_sha256": hashlib.sha256(license_bytes).hexdigest(),
            },
            "generator": {
                "script": "scripts/generate_model_catalog.py",
                "version": GENERATOR_VERSION,
            },
        }
    )
    # Refuse to emit anything the runtime loader would reject.
    model_catalog.parse_catalog(catalog, manifest)
    return {
        model_catalog.CATALOG_FILE: catalog,
        model_catalog.MANIFEST_FILE: manifest,
        LICENSE_FILE: license_bytes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wheel", type=Path, required=True, help="LiteLLM wheel pinned in uv.lock")
    parser.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    parser.add_argument("--check", action="store_true", help="exit 1 if packaged data differs")
    args = parser.parse_args(argv)

    try:
        outputs = generate(args.wheel, args.lock)
    except CatalogSourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    drift = [
        name
        for name, data in outputs.items()
        if not (model_catalog.DATA_DIR / name).is_file()
        or (model_catalog.DATA_DIR / name).read_bytes() != data
    ]
    if args.check:
        for name in drift:
            print(f"drift: {name}", file=sys.stderr)
        return 1 if drift else 0
    model_catalog.DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in outputs.items():
        (model_catalog.DATA_DIR / name).write_bytes(data)
    print(f"wrote {len(outputs)} files ({len(drift)} changed) to {model_catalog.DATA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
