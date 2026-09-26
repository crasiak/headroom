"""Savings estimates priced from the pinned catalog (CRA-459).

Expected dollars below are written out from the pinned LiteLLM 1.101.0 rates
(e.g. claude-sonnet-4-5: $3 in / $15 out / $0.30 cache read / $3.75 cache
write per 1M), not computed with the resolver under test.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from headroom.pricing import model_catalog
from headroom.proxy import savings_tracker as st
from tests._model_catalog import fake_catalog

ROOT = Path(__file__).resolve().parents[1]
M = 1_000_000


def _use(monkeypatch, catalog: model_catalog.ModelCatalog | None) -> None:
    monkeypatch.setattr(st, "load_model_catalog", lambda: catalog)


@pytest.mark.parametrize(
    ("model", "compression", "output", "cache", "segmented"),
    [
        # 100k read + 200k write + 700k uncached.
        ("claude-sonnet-4-5", 3.00, 15.00, 2.70, 0.03 + 0.75 + 2.10),
        ("claude-haiku-4-5@20251001", 1.00, 5.00, 0.90, 0.01 + 0.25 + 0.70),
        # Retired Sonnet aliases price at the Sonnet tier.
        ("claude-3-5-sonnet-20241022", 3.00, 15.00, 2.70, 0.03 + 0.75 + 2.10),
        # No published cache-write price: writes bill at the input rate.
        ("gpt-4o", 2.50, 10.00, 1.25, 0.125 + 0.50 + 1.75),
        # Bare MiniMax id resolves to LiteLLM's prefixed entry.
        ("MiniMax-M3", 0.30, 1.20, 0.24, 0.006 + 0.06 + 0.21),
        # The pinned SDK rejects bare "deepseek-chat"; pricing takes the
        # prefixed twin, whose published cache-write rate is 0.
        ("deepseek-chat", 0.28, 0.42, 0.252, 0.0028 + 0.0 + 0.196),
        # A legitimately free model is free, not the fallback estimate.
        ("cloudflare/@cf/google/gemma-2b-it-lora", 0.0, 0.0, 0.0, 0.0),
        # Unknown names keep each caller's existing estimate.
        ("acme-internal-llama", 3.00, 15.00, 0.0, 3.00),
        # A reserved documentation key is not a model (the SDK map priced it $0).
        ("sample_spec", 3.00, 15.00, 0.0, 3.00),
    ],
)
def test_packaged_catalog_prices_each_savings_layer(model, compression, output, cache, segmented):
    assert st._estimate_compression_savings_usd(model, M) == pytest.approx(compression)
    assert st._estimate_output_savings_usd(model, M) == pytest.approx(output)
    assert st._estimate_cache_savings_usd(model, M) == pytest.approx(cache)
    assert st._estimate_input_cost_usd(
        model,
        M,
        cache_read_tokens=100_000,
        cache_write_tokens=200_000,
        uncached_input_tokens=700_000,
    ) == pytest.approx(segmented)


def test_vertex_versioned_id_uses_its_published_vertex_rate() -> None:
    # Known divergence from the SDK path: its cost probe accepted a name absent
    # from its own map, so it fell back to the $3/$15 estimate. The catalog
    # prices the exact Vertex entry ($15 / $75 / $1.50 read per 1M).
    model = "claude-opus-4@20250514"
    assert st._estimate_compression_savings_usd(model, M) == pytest.approx(15.00)
    assert st._estimate_output_savings_usd(model, M) == pytest.approx(75.00)
    assert st._estimate_cache_savings_usd(model, M) == pytest.approx(13.50)


def test_segmented_input_never_adds_total_input_on_top() -> None:
    assert st._estimate_input_cost_usd(
        "claude-sonnet-4-5", 5 * M, cache_read_tokens=M
    ) == pytest.approx(0.30)
    assert st._estimate_input_cost_usd("claude-sonnet-4-5", M) == pytest.approx(3.00)


def test_request_savings_breakdown_uses_the_catalog() -> None:
    assert st.estimate_request_savings_usd(
        "claude-sonnet-4-5",
        compression_tokens_saved=M,
        tool_schema_tokens_saved=M // 2,
        output_tokens_saved=M // 10,
        cache_read_tokens=M,
    ) == pytest.approx(
        {"compression": 3.00, "tool_schema": 1.50, "output_shaping": 1.50, "provider_cache": 2.70}
    )


@pytest.mark.parametrize(
    ("alias_map", "model", "compression"),
    [
        # A gateway alias reduces to a priced key, stripping a bedrock/ prefix.
        (
            {"claude-opus": "bedrock/anthropic.claude-opus-4-1-20250805-v1:0"},
            "claude-opus",
            15.00,
        ),
        ({"team-fast": "vertex_ai/claude-haiku-4-5"}, "team-fast", 1.00),
        # The static map takes precedence over a direct catalog hit.
        ({"gpt-4o": "claude-haiku-4-5"}, "gpt-4o", 1.00),
        # An unpriced target falls through to ordinary resolution.
        ({"gpt-4o": "no-such-model"}, "gpt-4o", 2.50),
        ({"broken": "no-such-model"}, "broken", 3.00),
    ],
)
def test_static_alias_map_precedence(monkeypatch, alias_map, model, compression) -> None:
    monkeypatch.setenv("HEADROOM_MODEL_ALIAS_MAP", json.dumps(alias_map))
    st._resolve_catalog_model.cache_clear()

    assert st._estimate_compression_savings_usd(model, M) == pytest.approx(compression)


def test_unavailable_catalog_uses_each_callers_existing_estimate(monkeypatch) -> None:
    _use(monkeypatch, None)

    assert st._estimate_compression_savings_usd("claude-sonnet-4-5", M) == pytest.approx(3.00)
    assert st._estimate_output_savings_usd("claude-sonnet-4-5", M) == pytest.approx(15.00)
    assert st._estimate_cache_savings_usd("claude-sonnet-4-5", M) == pytest.approx(3.00)
    assert st._estimate_input_cost_usd(
        "claude-sonnet-4-5", 5 * M, cache_read_tokens=M, cache_write_tokens=M
    ) == pytest.approx(6.00)


def test_free_rates_differ_from_missing_rates(monkeypatch) -> None:
    _use(
        monkeypatch,
        fake_catalog(
            {
                "free": {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0},
                "no-output-rate": {"input_cost_per_token": 1e-06},
                "free-read": {"input_cost_per_token": 2e-06, "cache_read_input_token_cost": 0},
            }
        ),
    )

    assert st._estimate_compression_savings_usd("free", M) == 0.0
    assert st._estimate_output_savings_usd("free", M) == 0.0
    assert st._estimate_cache_savings_usd("free", M) == 0.0
    assert st._estimate_input_cost_usd("free", M) == 0.0
    assert st._estimate_output_savings_usd("no-output-rate", M) == pytest.approx(15.00)
    assert st._estimate_cache_savings_usd("no-output-rate", M) == 0.0
    assert st._estimate_cache_savings_usd("free-read", M) == pytest.approx(2.00)
    assert st._estimate_input_cost_usd("free-read", M, cache_read_tokens=M) == 0.0


def test_unresolvable_key_defers_to_the_next_candidate(monkeypatch) -> None:
    _use(
        monkeypatch,
        fake_catalog(
            {
                "gpt-x": {"input_cost_per_token": 9e-06},
                "openai/gpt-x": {"input_cost_per_token": 1e-06},
            },
            unresolvable={"gpt-x"},
        ),
    )

    assert st._estimate_compression_savings_usd("gpt-x", M) == pytest.approx(1.00)


@pytest.mark.parametrize("tokens", [0, -5])
def test_nonpositive_tokens_return_zero_before_metadata_access(monkeypatch, tokens) -> None:
    def forbidden() -> None:
        raise AssertionError("metadata accessed for a zero-token estimate")

    monkeypatch.setattr(st, "load_model_catalog", forbidden)

    assert st._estimate_compression_savings_usd("claude-sonnet-4-5", tokens) == 0.0
    assert st._estimate_output_savings_usd("claude-sonnet-4-5", tokens) == 0.0
    assert st._estimate_cache_savings_usd("claude-sonnet-4-5", tokens) == 0.0
    assert st._estimate_input_cost_usd("claude-sonnet-4-5", tokens) == 0.0


def test_client_name_resolution_cache_is_bounded() -> None:
    st._resolve_catalog_model.cache_clear()
    for index in range(st._MODEL_RESOLUTION_CACHE_MAXSIZE + 50):
        st._estimate_compression_savings_usd(f"client-model-{index}", 10)

    info = st._resolve_catalog_model.cache_info()
    assert info.maxsize == st._MODEL_RESOLUTION_CACHE_MAXSIZE == 256
    assert info.currsize == 256


def test_nonzero_savings_in_a_fresh_process_do_not_import_litellm() -> None:
    script = textwrap.dedent(
        """
        import json, sys
        from headroom.proxy.savings_tracker import (
            _estimate_input_cost_usd, estimate_request_savings_usd,
        )
        savings = estimate_request_savings_usd(
            "claude-sonnet-4-5", compression_tokens_saved=1000, output_tokens_saved=10,
            cache_read_tokens=500,
        )
        spend = _estimate_input_cost_usd("claude-sonnet-4-5", 1000, cache_read_tokens=500)
        print(json.dumps({
            "savings": savings, "spend": spend,
            "litellm": "litellm" in sys.modules,
            "pricing": "headroom.pricing.litellm_pricing" in sys.modules,
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
    assert observed["savings"]["compression"] == pytest.approx(0.003)
    assert observed["spend"] == pytest.approx(0.00015)
    assert observed["litellm"] is False
    assert observed["pricing"] is False
