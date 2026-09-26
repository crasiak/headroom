"""Pinned model metadata catalog: packaging, validation and failure behavior (CRA-459)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from typing import Any

import pytest

from headroom.pricing import model_catalog

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "headroom" / "pricing" / "data"
PINNED_SOURCE_SHA256 = "f68d88c12610ea31ab355a1293fde55aeed6fa78a1f4b182c67be47d80b1d202"
PINNED_WHEEL_SHA256 = "d4064024151ff2877e542b56c6bb6a39e0c3e6651abf4639af9346a678586e52"


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "generate_model_catalog", ROOT / "scripts" / "generate_model_catalog.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_catalog(
    directory: Path,
    models: dict[str, Any],
    *,
    unresolvable: list[Any] | None = None,
    schema_version: int = model_catalog.SCHEMA_VERSION,
    version: str = "1.0.0",
    manifest_version: str | None = None,
    raw: bytes | None = None,
) -> None:
    """Write a catalog + manifest pair whose digest matches unless ``raw`` overrides."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = raw
    if payload is None:
        # Plain json.dumps, not the canonical encoder: fixtures include NaN.
        payload = json.dumps(
            {
                "schema_version": schema_version,
                "litellm_version": version,
                "models": models,
                "unresolvable": [] if unresolvable is None else unresolvable,
            }
        ).encode()
    (directory / model_catalog.CATALOG_FILE).write_bytes(payload)
    manifest = {
        "schema_version": model_catalog.SCHEMA_VERSION,
        "catalog": {
            "file": model_catalog.CATALOG_FILE,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        },
        "source": {"package": "litellm", "version": manifest_version or version},
    }
    (directory / model_catalog.MANIFEST_FILE).write_bytes(json.dumps(manifest).encode())


@pytest.fixture
def isolated_catalog(tmp_path, monkeypatch):
    """Point the loader at a private directory with no cached snapshot."""
    monkeypatch.setattr(model_catalog, "DATA_DIR", tmp_path)
    monkeypatch.setattr(model_catalog, "_snapshot", None)
    monkeypatch.setattr(model_catalog, "_attempted", False)
    return tmp_path


GOOD_MODELS = {
    "paid-model": {"input_cost_per_token": 3e-06, "output_cost_per_token": 1.5e-05},
    "free-model": {"input_cost_per_token": 0, "output_cost_per_token": 0.0},
    "unpriced-image-model": {"litellm_provider": "acme", "mode": "image_generation"},
}


def test_valid_catalog_loads_once_as_an_immutable_snapshot(isolated_catalog) -> None:
    _write_catalog(isolated_catalog, GOOD_MODELS, unresolvable=["free-model"])

    catalog = model_catalog.load_model_catalog()

    assert catalog is not None
    assert catalog is model_catalog.load_model_catalog()
    assert catalog.litellm_version == "1.0.0"
    assert catalog.public_models == frozenset(GOOD_MODELS)
    assert catalog.models["free-model"]["input_cost_per_token"] == 0
    assert "input_cost_per_token" not in catalog.models["unpriced-image-model"]
    assert catalog.is_resolvable("paid-model")
    assert not catalog.is_resolvable("free-model")
    assert not catalog.is_resolvable("absent-model")
    with pytest.raises(TypeError):
        catalog.models["paid-model"]["input_cost_per_token"] = 0.0  # type: ignore[index]
    with pytest.raises(TypeError):
        catalog.models["injected"] = {}  # type: ignore[index]


INVALID_CATALOGS = {
    "negative_rate": ({"fixture-model": {"input_cost_per_token": -1e-06}}, {}),
    "boolean_rate": ({"fixture-model": {"output_cost_per_token": True}}, {}),
    "string_rate": ({"fixture-model": {"input_cost_per_token": "3e-06"}}, {}),
    "null_rate": ({"fixture-model": {"cache_read_input_token_cost": None}}, {}),
    "infinite_rate": ({"fixture-model": {"input_cost_per_token": float("inf")}}, {}),
    "nan_rate": ({"fixture-model": {"input_cost_per_token": float("nan")}}, {}),
    "unknown_field": ({"fixture-model": {"input_cost_per_token_priority": 1e-06}}, {}),
    "entry_not_object": ({"fixture-model": [1, 2]}, {}),
    "reserved_key": ({"sample_spec": {"input_cost_per_token": 0.0}}, {}),
    "empty_key": ({"": {"input_cost_per_token": 0.0}}, {}),
    "boolean_limit": ({"fixture-model": {"max_tokens": False}}, {}),
    "non_string_provider": ({"fixture-model": {"litellm_provider": 7}}, {}),
    "unknown_unresolvable": ({"fixture-model": {}}, {"unresolvable": ["other-model"]}),
    "unsorted_unresolvable": (
        {"alpha-model": {}, "beta-model": {}},
        {"unresolvable": ["beta-model", "alpha-model"]},
    ),
    "unsupported_schema": (
        {"fixture-model": {}},
        {"schema_version": model_catalog.SCHEMA_VERSION + 1},
    ),
    "manifest_version_mismatch": ({"fixture-model": {}}, {"manifest_version": "9.9.9"}),
    "no_models": ({}, {}),
}


@pytest.mark.parametrize("case", sorted(INVALID_CATALOGS))
def test_invalid_catalog_is_unavailable_with_one_bounded_diagnostic(
    isolated_catalog, caplog, case
) -> None:
    models, overrides = INVALID_CATALOGS[case]
    _write_catalog(isolated_catalog, models, **overrides)
    caplog.set_level(logging.DEBUG, logger="headroom.pricing.model_catalog")

    assert model_catalog.load_model_catalog() is None
    assert model_catalog.load_model_catalog() is None

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1, messages
    assert str(isolated_catalog) not in messages[0]
    for name in models:
        if name:
            assert name not in messages[0]


@pytest.mark.parametrize(
    "damage",
    ["missing_catalog", "missing_manifest", "corrupt_json", "digest_mismatch", "not_utf8"],
)
def test_damaged_resources_are_unavailable(isolated_catalog, caplog, damage) -> None:
    _write_catalog(isolated_catalog, GOOD_MODELS)
    catalog_path = isolated_catalog / model_catalog.CATALOG_FILE
    if damage == "missing_catalog":
        catalog_path.unlink()
    elif damage == "missing_manifest":
        (isolated_catalog / model_catalog.MANIFEST_FILE).unlink()
    elif damage == "corrupt_json":
        _write_catalog(isolated_catalog, {}, raw=b'{"schema_version": 1, "models": {')
    elif damage == "digest_mismatch":
        catalog_path.write_bytes(catalog_path.read_bytes().replace(b"3e-06", b"4e-06"))
    else:
        _write_catalog(isolated_catalog, {}, raw=b"\xff\xfe")
    caplog.set_level(logging.DEBUG, logger="headroom.pricing.model_catalog")

    assert model_catalog.load_model_catalog() is None
    assert len(caplog.records) == 1
    assert str(isolated_catalog) not in caplog.records[0].getMessage()


def test_catalog_size_bound_is_enforced_at_the_boundary(isolated_catalog, monkeypatch) -> None:
    _write_catalog(isolated_catalog, GOOD_MODELS)
    size = (isolated_catalog / model_catalog.CATALOG_FILE).stat().st_size

    monkeypatch.setattr(model_catalog, "MAX_CATALOG_BYTES", size)
    assert model_catalog.load_model_catalog() is not None

    monkeypatch.setattr(model_catalog, "_snapshot", None)
    monkeypatch.setattr(model_catalog, "_attempted", False)
    monkeypatch.setattr(model_catalog, "MAX_CATALOG_BYTES", size - 1)
    assert model_catalog.load_model_catalog() is None


def test_pinned_catalog_fits_its_size_bound_with_room_for_refresh() -> None:
    size = (DATA_DIR / model_catalog.CATALOG_FILE).stat().st_size
    assert size * 3 < model_catalog.MAX_CATALOG_BYTES


@pytest.mark.parametrize("valid", [True, False])
def test_concurrent_first_use_sees_one_complete_state(isolated_catalog, monkeypatch, valid) -> None:
    _write_catalog(
        isolated_catalog, GOOD_MODELS if valid else {"fixture-model": {"max_tokens": -1}}
    )
    real_parse = model_catalog.parse_catalog
    calls = 0
    entered = threading.Event()
    proceed = threading.Event()

    def slow_parse(catalog_bytes: bytes, manifest_bytes: bytes) -> Any:
        nonlocal calls
        calls += 1
        entered.set()
        proceed.wait(5)
        return real_parse(catalog_bytes, manifest_bytes)

    monkeypatch.setattr(model_catalog, "parse_catalog", slow_parse)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(model_catalog.load_model_catalog()))
        for _ in range(16)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(5)
    proceed.set()
    for thread in threads:
        thread.join(5)

    assert calls == 1
    assert len(results) == 16
    assert len({id(result) for result in results}) == 1
    if valid:
        assert results[0].public_models == frozenset(GOOD_MODELS)
    else:
        assert results[0] is None


def test_packaged_catalog_matches_manifest_and_pinned_source() -> None:
    manifest = json.loads((DATA_DIR / model_catalog.MANIFEST_FILE).read_bytes())
    catalog_bytes = (DATA_DIR / model_catalog.CATALOG_FILE).read_bytes()
    license_bytes = (DATA_DIR / manifest["source"]["license_file"]).read_bytes()

    assert manifest["catalog"]["sha256"] == hashlib.sha256(catalog_bytes).hexdigest()
    assert manifest["catalog"]["bytes"] == len(catalog_bytes)
    assert manifest["catalog"]["model_count"] == 3816
    assert manifest["source"]["package"] == "litellm"
    assert manifest["source"]["version"] == "1.101.0"
    assert manifest["source"]["wheel_sha256"] == PINNED_WHEEL_SHA256
    assert manifest["source"]["member"] == "litellm/model_prices_and_context_window_backup.json"
    assert manifest["source"]["member_sha256"] == PINNED_SOURCE_SHA256
    assert manifest["source"]["member_bytes"] == 2_311_609
    assert manifest["source"]["excluded_keys"] == ["fallback_generalizations", "sample_spec"]
    assert manifest["source"]["license"] == "MIT"
    assert manifest["source"]["license_sha256"] == hashlib.sha256(license_bytes).hexdigest()
    assert b"MIT" in license_bytes and b"Berri AI" in license_bytes
    # Nothing machine-local or time-dependent enters the deterministic payload.
    for text in (catalog_bytes.decode(), json.dumps(manifest)):
        assert "/Users/" not in text and "/nix/store" not in text and "/tmp" not in text

    catalog = model_catalog.parse_catalog(
        catalog_bytes, (DATA_DIR / model_catalog.MANIFEST_FILE).read_bytes()
    )
    assert len(catalog.models) == 3816
    assert "sample_spec" not in catalog.models
    assert "fallback_generalizations" not in catalog.models
    # Headroom's runtime registrations are not upstream model ids.
    assert "MiniMax-M3" not in catalog.models
    assert catalog.models["minimax/MiniMax-M3"]["input_cost_per_token"] == 3e-07
    assert dict(catalog.models["claude-sonnet-4-5"]) == {
        "cache_creation_input_token_cost": 3.75e-06,
        "cache_creation_input_token_cost_above_1hr": 6e-06,
        "cache_creation_input_token_cost_above_1hr_above_200k_tokens": 1.2e-05,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-06,
        "cache_read_input_token_cost": 3e-07,
        "cache_read_input_token_cost_above_200k_tokens": 6e-07,
        "input_cost_per_token": 3e-06,
        "input_cost_per_token_above_200k_tokens": 6e-06,
        "litellm_provider": "anthropic",
        "max_input_tokens": 200000,
        "max_output_tokens": 64000,
        "max_tokens": 64000,
        "mode": "chat",
        "output_cost_per_token": 1.5e-05,
        "output_cost_per_token_above_200k_tokens": 2.25e-05,
    }
    # Absent rates stay absent: gpt-4o publishes no cache-write price.
    assert "cache_creation_input_token_cost" not in catalog.models["gpt-4o"]
    # The pinned SDK's cost probe rejects these bare ids, so pricing takes the
    # provider-prefixed twin (see test_model_catalog_sdk_parity.py).
    assert {"deepseek-chat", "deepseek-reasoner"} <= catalog.unresolvable
    assert "deepseek/deepseek-chat" not in catalog.unresolvable


def test_packaged_catalog_loads_in_a_fresh_process_without_litellm() -> None:
    script = textwrap.dedent(
        """
        import json, sys
        import headroom.pricing
        from headroom.pricing import litellm_model_resolution, model_catalog
        catalog = model_catalog.load_model_catalog()
        print(json.dumps({
            "models": len(catalog.models),
            "litellm": "litellm" in sys.modules,
            "pricing": "headroom.pricing.litellm_pricing" in sys.modules,
            "file": model_catalog.__file__,
        }))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        cwd=ROOT,
    )
    observed = json.loads(result.stdout)
    assert observed["file"].startswith(str(ROOT))
    assert observed == {**observed, "models": 3816, "litellm": False, "pricing": False}


# --- generator ---------------------------------------------------------------


def test_generator_keeps_declared_fields_exactly_and_drops_reserved_keys() -> None:
    generator = _load_generator()
    source = {
        "sample_spec": {"input_cost_per_token": 0.0, "max_tokens": "description"},
        "fallback_generalizations": {"rules": []},
        "b-model": {
            "input_cost_per_token": 0,
            "output_cost_per_token": 1.5e-05,
            "output_cost_per_token_priority": 3e-05,
            "supports_vision": True,
            "max_tokens": 2000000.0,
            "litellm_provider": "acme",
        },
        "a-model": {"mode": "image_generation", "output_cost_per_image": 0.04},
    }

    models = generator.extract_models(source)

    assert list(models) == ["a-model", "b-model"]
    assert models["a-model"] == {"mode": "image_generation"}
    assert models["b-model"] == {
        "input_cost_per_token": 0,
        "output_cost_per_token": 1.5e-05,
        "max_tokens": 2000000.0,
        "litellm_provider": "acme",
    }
    assert isinstance(models["b-model"]["input_cost_per_token"], int)


@pytest.mark.parametrize(
    "entry",
    [
        {"input_cost_per_token": -1.0},
        {"input_cost_per_token": True},
        {"input_cost_per_token": "0.1"},
        {"input_cost_per_token": None},
        {"input_cost_per_token": float("nan")},
        {"input_cost_per_token": float("inf")},
        {"max_tokens": -5},
        {"litellm_provider": None},
        {"aliases": ["another-name"]},
        ["not", "an", "object"],
    ],
)
def test_generator_rejects_malformed_source_rather_than_dropping_models(entry) -> None:
    generator = _load_generator()
    with pytest.raises(generator.CatalogSourceError):
        generator.extract_models({"good": {"input_cost_per_token": 1e-06}, "bad": entry})


def test_generator_output_is_deterministic_and_independent_of_key_order() -> None:
    generator = _load_generator()
    forward = {"x": {"input_cost_per_token": 1e-06, "mode": "chat"}, "é-model": {}}
    backward = {"é-model": {}, "x": {"mode": "chat", "input_cost_per_token": 1e-06}}

    first = generator.build_catalog(forward, unresolvable={"x"}, version="1.0.0")
    second = generator.build_catalog(backward, unresolvable={"x"}, version="1.0.0")

    assert first == second
    assert first.endswith(b"\n")
    assert "é-model".encode() in first
    parsed = json.loads(first)
    assert parsed["unresolvable"] == ["x"]
    assert parsed["schema_version"] == model_catalog.SCHEMA_VERSION


CONTEXT_MODELS = {
    "claude-opus-4-1": {"max_input_tokens": 200_000, "max_tokens": 32_000},
    "deepseek-v4-flash": {"max_input_tokens": 1_000_000.0},
    "output-only": {"max_tokens": 8_192},
    "acme/listed-without-limits": {"mode": "chat"},
    "listed-without-limits": {"max_input_tokens": 4_096},
}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("claude-opus-4-1", 200_000),
        # get_model_info matches keys case-insensitively.
        ("DeepSeek-V4-Flash", 1_000_000),
        # One leading provider segment is dropped, as the SDK's split model does.
        ("anthropic/claude-opus-4-1", 200_000),
        ("ANTHROPIC/Claude-Opus-4-1", 200_000),
        ("output-only", 8_192),
        # A listed name without limits is final; the stripped name is not tried.
        ("acme/listed-without-limits", None),
        ("gateway/team/claude-opus-4-1", None),
        ("acme-internal-llama", None),
        ("", None),
    ],
)
def test_context_window_reads_limits_as_get_model_info_keys_them(name, expected) -> None:
    from tests._model_catalog import fake_catalog

    window = fake_catalog(CONTEXT_MODELS).context_window(name)

    assert window == expected
    assert window is None or type(window) is int


def test_pinned_catalog_context_window_for_ids_outside_headroom_tables() -> None:
    catalog = model_catalog.load_model_catalog()
    assert catalog is not None

    assert catalog.context_window("claude-opus-4-1") == 200_000
    assert catalog.context_window("claude-3-7-sonnet-20250219") == 200_000
    assert catalog.context_window("DeepSeek-V4-Flash") == 1_000_000
    assert catalog.context_window("zai/glm-4.6") == 200_000
    assert catalog.context_window("glm-4.6") is None
