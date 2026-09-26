"""Regression: free (0-priced) models must not be billed the fallback rate.

`_estimate_compression_savings_usd` / `_estimate_input_cost_usd` read
`input_cost_per_token` from the model catalog (litellm's cost map at the time)
and used `if not input_cost_per_token: raise`,
which treats a legitimate `0.0` (a free / local / vendored-at-0 model) as "price
unavailable" and falls back to DEFAULT_FALLBACK_INPUT_COST_PER_TOKEN ($3/M) —
fabricating savings/cost for a model that costs nothing. A missing key (unknown
model) must still fall back.
"""

from __future__ import annotations

from headroom.proxy import savings_tracker as st
from headroom.proxy.savings_tracker import (
    DEFAULT_FALLBACK_INPUT_COST_PER_TOKEN,
    DEFAULT_FALLBACK_OUTPUT_COST_PER_TOKEN,
    _estimate_compression_savings_usd,
    _estimate_input_cost_usd,
    _estimate_output_savings_usd,
)
from tests._model_catalog import fake_catalog


def test_compression_savings_zero_for_free_model(monkeypatch):
    monkeypatch.setattr(
        st,
        "load_model_catalog",
        lambda: fake_catalog({"free-model": {"input_cost_per_token": 0.0}}),
    )
    assert _estimate_compression_savings_usd("free-model", 1_000_000) == 0.0


def test_compression_savings_falls_back_for_unknown_model(monkeypatch):
    # Model absent from the catalog → input_cost_per_token is None → fall back.
    monkeypatch.setattr(st, "load_model_catalog", lambda: fake_catalog({}))
    got = _estimate_compression_savings_usd("unknown-model", 1_000_000)
    assert got == 1_000_000 * DEFAULT_FALLBACK_INPUT_COST_PER_TOKEN


def test_compression_savings_uses_real_price_for_paid_model(monkeypatch):
    price = 3.0 / 1_000_000
    monkeypatch.setattr(
        st,
        "load_model_catalog",
        lambda: fake_catalog({"paid-model": {"input_cost_per_token": price}}),
    )
    assert _estimate_compression_savings_usd("paid-model", 1_000_000) == 1_000_000 * price


def test_input_cost_zero_for_free_model(monkeypatch):
    monkeypatch.setattr(
        st,
        "load_model_catalog",
        lambda: fake_catalog({"free-model": {"input_cost_per_token": 0.0}}),
    )
    assert _estimate_input_cost_usd("free-model", 500_000) == 0.0


def test_output_savings_zero_for_free_model(monkeypatch):
    # output_cost_per_token == 0.0 (free model) must yield $0, not the fallback.
    monkeypatch.setattr(
        st,
        "load_model_catalog",
        lambda: fake_catalog({"free-model": {"output_cost_per_token": 0.0}}),
    )
    assert _estimate_output_savings_usd("free-model", 1_000_000) == 0.0


def test_output_savings_falls_back_for_unknown_model(monkeypatch):
    # Model absent from the catalog → output_cost_per_token is None → fall back.
    monkeypatch.setattr(st, "load_model_catalog", lambda: fake_catalog({}))
    got = _estimate_output_savings_usd("unknown-model", 1_000_000)
    assert got == 1_000_000 * DEFAULT_FALLBACK_OUTPUT_COST_PER_TOKEN


def test_output_savings_uses_real_price_for_paid_model(monkeypatch):
    price = 15.0 / 1_000_000
    monkeypatch.setattr(
        st,
        "load_model_catalog",
        lambda: fake_catalog({"paid-model": {"output_cost_per_token": price}}),
    )
    assert _estimate_output_savings_usd("paid-model", 1_000_000) == 1_000_000 * price
