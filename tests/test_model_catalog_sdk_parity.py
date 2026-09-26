"""Offline differential check: pinned catalog vs the LiteLLM SDK it came from (CRA-459).

Runs only when the installed LiteLLM is the catalog's pinned version. The SDK
side forces LiteLLM's local cost map and blocks non-loopback sockets, so both
sides read the same bundled data. The reference savings resolver below is the
SDK-probing implementation the catalog replaced, kept here as the oracle.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from headroom.pricing import model_catalog

ROOT = Path(__file__).resolve().parents[1]

# Characterized differences, each with the SDK-path value and the catalog value
# at 1000 tokens. Every other name must match exactly.
KNOWN_DIVERGENCES = {
    # The SDK probe accepts "claude-opus-4" / "anthropic/claude-opus-4@20250514",
    # which are absent from its own map, so the SDK path fell back to the
    # blended estimate. The catalog prices the exact Vertex entry.
    "claude-opus-4@20250514": {
        "compression": (0.003, 0.015),
        "output": (0.015, 0.075),
        "cache": (0.0, 0.0135),
        "segmented": (0.003, 0.0144),
    },
    "claude-sonnet-4@20250514": {"cache": (0.0, 0.0027), "segmented": (0.003, 0.00288)},
    # Reserved documentation entry; the SDK map priced it at $0.
    "sample_spec": {
        "compression": (0.0, 0.003),
        "output": (0.0, 0.015),
        "segmented": (0.0, 0.003),
    },
}

_SCRIPT = textwrap.dedent(
    """
    import contextlib, io, json, os, socket, sys
    _connect = socket.socket.connect
    def _loopback_only(self, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise OSError("network blocked")
        return _connect(self, address)
    socket.socket.connect = _loopback_only

    import litellm
    sdk_keys = set(litellm.model_cost)  # before Headroom registers anything
    from headroom.pricing import model_catalog
    from headroom.pricing.litellm_pricing import resolve_litellm_model
    from headroom.proxy import savings_tracker as st
    from headroom.telemetry import session

    catalog = model_catalog.load_model_catalog()
    F_IN, F_OUT = 3.0 / 1_000_000, 15.0 / 1_000_000

    def probe(name):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                litellm.cost_per_token(model=name, prompt_tokens=1, completion_tokens=0)
                return True
            except Exception:
                return False

    def sdk_resolve(model):
        try:
            resolved = resolve_litellm_model(model)
            info = litellm.model_cost.get(resolved)
            if info and info.get("input_cost_per_token") is not None:
                return resolved
        except Exception:
            pass
        if probe(model):
            return model
        for pattern, prefix in (("claude-", "anthropic/"), ("gpt-", "openai/"), ("o1-", "openai/"),
                                ("o3-", "openai/"), ("o4-", "openai/"), ("gemini-", "google/")):
            if model.startswith(pattern):
                return prefix + model if probe(prefix + model) else model
        return model

    def sdk_savings(model, n=1000):
        info = litellm.model_cost.get(sdk_resolve(model), {})
        rate_in, rate_out = info.get("input_cost_per_token"), info.get("output_cost_per_token")
        row = {
            "compression": n * F_IN if rate_in is None else n * float(rate_in),
            "output": n * F_OUT if rate_out is None else n * float(rate_out),
        }
        try:
            if not rate_in:
                row["cache"] = 0.0
            else:
                delta = float(rate_in) - float(info.get("cache_read_input_token_cost", rate_in))
                row["cache"] = n * delta if delta > 0 else 0.0
        except Exception:
            row["cache"] = 0.0
        try:
            if rate_in is None:
                raise RuntimeError
            row["segmented"] = (
                100 * float(info.get("cache_read_input_token_cost", rate_in))
                + 200 * float(info.get("cache_creation_input_token_cost", rate_in))
                + 700 * float(rate_in)
            )
        except Exception:
            row["segmented"] = n * F_IN
        return row

    def catalog_savings(model, n=1000):
        return {
            "compression": st._estimate_compression_savings_usd(model, n),
            "output": st._estimate_output_savings_usd(model, n),
            "cache": st._estimate_cache_savings_usd(model, n),
            "segmented": st._estimate_input_cost_usd(
                model, n, cache_read_tokens=100, cache_write_tokens=200, uncached_input_tokens=700
            ),
        }

    def sdk_public_model(model):
        for candidate in (model, model.rsplit("/", 1)[-1], model.rsplit(".", 1)[-1]):
            if model and candidate in sdk_keys:
                return candidate
        return None

    names = set(json.loads(sys.argv[1]))
    for key in catalog.models:
        names.update({key, key.rsplit("/", 1)[-1], key.rsplit(".", 1)[-1]})
        if key.startswith("claude-"):
            names.update({key + "@20251001", "gateway/team/" + key})
        if key.startswith(("anthropic.", "us.anthropic.", "meta.", "amazon.")):
            names.add("bedrock/" + key)

    savings, telemetry = {}, {}
    for name in sorted(names):
        expected, actual = sdk_savings(name), catalog_savings(name)
        if expected != actual:
            savings[name] = {k: (expected[k], actual[k]) for k in expected if expected[k] != actual[k]}
        if sdk_public_model(name) != session._public_model(name):
            telemetry[name] = [sdk_public_model(name), session._public_model(name)]
    print(json.dumps({
        "names": len(names),
        "savings": savings,
        "telemetry": telemetry,
        "unresolvable_mismatch": sorted(k for k in catalog.models if probe(k) == (k in catalog.unresolvable)),
        "public_mismatch": sorted((sdk_keys - {"sample_spec"}) ^ set(catalog.public_models)),
    }))
    """
)

FIXTURES = [
    "claude-sonnet-4-5",
    "claude-opus-4-6",
    "claude-haiku-4-5@20251001",
    "claude-3-5-sonnet-20241022",
    "claude-3-sonnet-20240229",
    "gpt-4o",
    "gpt-5.4",
    "o4-mini",
    "MiniMax-M3",
    "deepseek-chat",
    "deepseek-v4-flash",
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-opus",
    "team-fast",
    "broken",
    "sample_spec",
    "acme-internal-llama",
    "ft:gpt-4o:acme-corp:internal-bot:abc123",
]


def _pinned_sdk_installed() -> bool:
    try:
        installed = importlib.metadata.version("litellm")
    except importlib.metadata.PackageNotFoundError:
        return False
    catalog = model_catalog.load_model_catalog()
    return catalog is not None and installed == catalog.litellm_version


@pytest.mark.skipif(not _pinned_sdk_installed(), reason="needs the catalog's pinned LiteLLM")
@pytest.mark.parametrize(
    "alias_map",
    [
        None,
        {
            "claude-opus": "bedrock/anthropic.claude-opus-4-1-20250805-v1:0",
            "team-fast": "vertex_ai/gemini-2.5-flash",
            "gpt-4o": "claude-haiku-4-5",
            "broken": "no-such-model",
        },
    ],
)
def test_catalog_matches_pinned_sdk_offline(alias_map) -> None:
    env = {**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True", "HEADROOM_BEACON": "off"}
    env.pop("HEADROOM_MODEL_ALIAS_MAP", None)
    if alias_map is not None:
        env["HEADROOM_MODEL_ALIAS_MAP"] = json.dumps(alias_map)
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, json.dumps(FIXTURES)],
        capture_output=True,
        text=True,
        check=True,
        timeout=600,
        cwd=ROOT,
        env=env,
    )
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert report["names"] > 6000
    assert report["public_mismatch"] == []
    assert report["unresolvable_mismatch"] == []
    assert report["telemetry"] == {"sample_spec": ["sample_spec", None]}
    assert set(report["savings"]) == set(KNOWN_DIVERGENCES)
    for name, fields in KNOWN_DIVERGENCES.items():
        assert set(report["savings"][name]) == set(fields), name
        for field, (sdk_value, catalog_value) in fields.items():
            assert report["savings"][name][field] == pytest.approx([sdk_value, catalog_value])
