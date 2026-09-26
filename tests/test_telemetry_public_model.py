"""Telemetry model ids come only from the pinned public catalog (CRA-459).

Model ids are public SKUs; custom deployment names can carry an organisation's
name. The beacon may therefore emit a model id only when the immutable public
set contains it, and never the private prefix a client wrapped around it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from headroom.pricing import model_catalog
from headroom.telemetry import session


@pytest.mark.parametrize(
    ("client_name", "emitted"),
    [
        ("claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("gpt-4o", "gpt-4o"),
        ("minimax/MiniMax-M3", "minimax/MiniMax-M3"),
        # Exact, then last slash component, then last dot component.
        ("acme-private-gateway/claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("tenant.acme.gpt-4o", "gpt-4o"),
        (
            "bedrock/anthropic.claude-sonnet-4-5-20250929-v1:0",
            "anthropic.claude-sonnet-4-5-20250929-v1:0",
        ),
        # Unknown, fine-tuned and self-hosted names never reach the wire.
        ("ft:gpt-4o:acme-corp:internal-bot:abc123", None),
        ("acme-internal-llama", None),
        ("azure/acme-private-deployment", None),
        ("", None),
        # Reserved keys in LiteLLM's file are not model ids.
        ("sample_spec", None),
        ("fallback_generalizations", None),
        # Headroom's pricing-only registration does not widen the public set.
        ("MiniMax-M3", None),
        # Lookalikes are not normalized into a public id.
        ("claude-sonnet-4-5​", None),
        ("clаude-sonnet-4-5", None),
        ("CLAUDE-SONNET-4-5", None),
    ],
)
def test_public_model_emits_only_a_public_candidate(client_name, emitted) -> None:
    assert session._public_model(client_name) == emitted


def test_pricing_alias_map_does_not_widen_telemetry(monkeypatch) -> None:
    monkeypatch.setenv("HEADROOM_MODEL_ALIAS_MAP", json.dumps({"acme-opus": "claude-opus-4-6"}))

    assert session._public_model("acme-opus") is None


def test_unavailable_catalog_emits_no_model(monkeypatch) -> None:
    monkeypatch.setattr(model_catalog, "load_model_catalog", lambda: None)

    assert session._public_model("claude-sonnet-4-5") is None


def _outcome(model: str) -> Any:
    return SimpleNamespace(
        provider="anthropic",
        model=model,
        original_tokens=100,
        optimized_tokens=60,
        output_tokens=10,
        tokens_saved=40,
        cache_read_tokens=0,
        cache_write_tokens=0,
        uncached_input_tokens=0,
        overhead_ms=1.0,
        total_latency_ms=5.0,
        status_code=200,
        transforms_applied=(),
        tags={},
    )


def test_session_payload_carries_only_public_model_ids() -> None:
    emitted: list[dict[str, Any]] = []
    aggregator = session.SessionAggregator(emitted.append, idle_s=60.0)
    for index, name in enumerate(
        [
            "claude-sonnet-4-5",
            "acme-private-gateway/claude-sonnet-4-5",
            "ft:gpt-4o:acme-corp:internal-bot:abc123",
            "acme-internal-llama",
            "sample_spec",
            "tenant.acme.gpt-4o",
        ]
    ):
        aggregator.record(_outcome(name), now=1000.0 + index)
    aggregator.flush_all()

    assert emitted[-1]["models"] == ["claude-sonnet-4-5", "gpt-4o"]
    wire = json.dumps(emitted)
    for private in ("acme", "ft:", "sample_spec", "internal-llama"):
        assert private not in wire, private
