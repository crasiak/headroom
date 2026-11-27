"""Response accounting uses immutable metadata, never the SDK registry."""

import builtins

import pytest

from headroom.pricing import counterfactual as cf
from headroom.pricing.model_catalog import ModelCatalog


@pytest.fixture
def snapshot_only(monkeypatch):
    snapshot = ModelCatalog(
        models={
            "anthropic/claude-catalog-test": {
                "input_cost_per_token": 3e-6,
                "cache_read_input_token_cost": 0.3e-6,
                "cache_creation_input_token_cost": 3.75e-6,
                "cache_creation_input_token_cost_above_1hr": 6e-6,
                "input_cost_per_token_above_100k_tokens": 6e-6,
                "cache_read_input_token_cost_above_100k_tokens": 0.6e-6,
                "cache_creation_input_token_cost_above_100k_tokens": 7.5e-6,
            },
            "free-catalog-test": {"input_cost_per_token": 0.0},
        },
        public_models=frozenset(),
        unresolvable=frozenset(),
        litellm_version="test",
    )
    monkeypatch.setattr(cf, "load_model_catalog", lambda: snapshot, raising=False)
    original_import = builtins.__import__
    attempts = []

    def guarded_import(name, *args, **kwargs):
        if name == "litellm" or name.startswith("litellm."):
            attempts.append(name)
            raise AssertionError("SDK import on response accounting path")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    cf.resolve_rates.cache_clear()
    cf.long_context_threshold.cache_clear()
    yield attempts
    cf.resolve_rates.cache_clear()
    cf.long_context_threshold.cache_clear()
    assert attempts == []


def test_snapshot_rates_preserve_cache_mix_and_long_context(snapshot_only):
    model = "claude-catalog-test"
    assert cf.long_context_threshold(model) == 100_000
    rates = cf.resolve_rates(model)
    assert rates is not None
    assert rates.price(
        cf.TokenSplit(read=20, write_5m=30, write_1h=10, uncached=40)
    ) == pytest.approx(20 * 0.3e-6 + 30 * 3.75e-6 + 10 * 6e-6 + 40 * 3e-6)
    long_rates = cf.resolve_rates(model, long_context=True)
    assert long_rates is not None
    assert long_rates.uncached == 6e-6
    assert long_rates.write_1h == 12e-6
    assert long_rates.basis == cf.BASIS_CATALOG_TTL_RATIO
    assert rates.price(cf.TokenSplit(read=0, write_5m=0, write_1h=0, uncached=0)) == 0


@pytest.mark.parametrize("prefix", ["", "deepseek/"])
@pytest.mark.parametrize(
    ("model", "input_per_million", "output_per_million", "read_per_million"),
    [
        ("deepseek-flash", 0.15, 0.60, 0.003),
        ("deepseek-v4-flash", 0.15, 0.60, 0.003),
        ("deepseek-v4-flash-vision-exp", 0.15, 0.60, 0.003),
        ("deepseek-v4-pro", 0.66, 1.98, 0.022),
    ],
)
def test_trusted_vendor_prices_never_widen_public_membership(
    snapshot_only,
    prefix,
    model,
    input_per_million,
    output_per_million,
    read_per_million,
):
    catalog = cf.load_model_catalog()
    name = prefix + model
    assert name not in catalog.models
    assert name not in catalog.public_models
    row = catalog.pricing_models[name]
    assert row["output_cost_per_token"] == pytest.approx(output_per_million / 1e6)
    rates = cf.resolve_rates(name)
    assert rates.uncached == pytest.approx(input_per_million / 1e6)
    assert rates.read == pytest.approx(read_per_million / 1e6)
    with pytest.raises(TypeError):
        row["input_cost_per_token"] = 123
    with pytest.raises(TypeError):
        catalog.pricing_models[name] = {}
    assert cf.resolve_rates("other-vendor/deepseek-flash") is None
    assert cf.resolve_rates("deepseek-flash:free") is None


def test_vendor_overlay_preserves_raw_write_buckets_and_unrelated_skus():
    raw = {
        "deepseek-v4-pro": {"input_cost_per_token": 99.0, "cache_creation_input_token_cost": 0.0},
        "other/deepseek-flash": {"input_cost_per_token": 9.0},
        "deepseek-flash:free": {"input_cost_per_token": 0.0},
    }
    catalog = ModelCatalog(
        models=raw, public_models=frozenset(raw), unresolvable=frozenset(), litellm_version="test"
    )
    assert catalog.models == raw
    assert catalog.models["deepseek-v4-pro"]["input_cost_per_token"] == 99.0
    assert catalog.pricing_models["deepseek-v4-pro"]["cache_creation_input_token_cost"] == 0.0
    assert "cache_creation_input_token_cost" not in catalog.pricing_models["deepseek-flash"]
    assert catalog.pricing_models["other/deepseek-flash"]["input_cost_per_token"] == 9.0
    assert catalog.pricing_models["deepseek-flash:free"]["input_cost_per_token"] == 0.0
    assert catalog.public_models == frozenset(raw)


def test_packaged_cache_minima_survive_snapshot_extraction():
    assert cf.catalog_min_cacheable_tokens("claude-sonnet-4-5") == 1024
    assert cf.catalog_min_cacheable_tokens("claude-haiku-4-5") == 4096
    assert cf.catalog_min_cacheable_tokens("claude-opus-4-6") == 4096


def test_snapshot_free_and_unknown_are_distinct(snapshot_only):
    rates = cf.resolve_rates("free-catalog-test")
    assert rates is not None
    assert rates.price(cf.TokenSplit(read=100, write_5m=100, write_1h=100, uncached=100)) == 0
    assert cf.resolve_rates("not-in-snapshot") is None
