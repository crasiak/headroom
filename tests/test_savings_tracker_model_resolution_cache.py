"""Regression: `_resolve_catalog_model`'s cache must be bounded (PR #2860 review).

A plain unbounded dict cache keyed by a client-controlled model string is a
memory-retention path on a request-facing proxy: a caller can grow it without
limit by sending a new model name on every request. The fix uses a bounded
`functools.lru_cache`. These tests pin the three properties that actually
matter, independent of the pricing behavior covered elsewhere:

- repeated resolution of the same unresolvable model only resolves once
- the cache never grows past its bound, no matter how many distinct model
  names get resolved
- an evicted name is transparently resolved again (never silently wrong or
  stuck) rather than growing the cache further
"""

from __future__ import annotations

from collections.abc import Callable

from headroom.pricing.model_catalog import ModelCatalog
from headroom.proxy import savings_tracker as st
from tests._model_catalog import fake_catalog


def _counting_empty_catalog(calls: list[int]) -> Callable[[], ModelCatalog]:
    """An empty catalog that counts resolutions: every name is unknown.

    Each uncached `_resolve_catalog_model` call reads the catalog once, so the
    count is the number of resolutions the cache did not absorb.
    """
    catalog = fake_catalog({})

    def load() -> ModelCatalog:
        calls.append(1)
        return catalog

    return load


def test_resolve_catalog_model_resolves_unknown_model_once(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(st, "load_model_catalog", _counting_empty_catalog(calls))

    for _ in range(5):
        resolved = st._resolve_catalog_model("widget-local-model")
        assert resolved == "widget-local-model"

    assert len(calls) == 1


def test_resolve_catalog_model_cache_is_bounded(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(st, "load_model_catalog", _counting_empty_catalog(calls))

    extra_beyond_bound = 50
    for i in range(st._MODEL_RESOLUTION_CACHE_MAXSIZE + extra_beyond_bound):
        st._resolve_catalog_model(f"widget-local-model-{i}")

    info = st._resolve_catalog_model.cache_info()
    assert info.maxsize == st._MODEL_RESOLUTION_CACHE_MAXSIZE
    # However many distinct names were resolved, the cache itself never
    # grows past its bound -- this is the actual memory-retention fix.
    assert info.currsize == st._MODEL_RESOLUTION_CACHE_MAXSIZE


def test_resolve_catalog_model_evicted_name_is_resolved_again(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(st, "load_model_catalog", _counting_empty_catalog(calls))

    st._resolve_catalog_model("seed-model")
    assert len(calls) == 1

    # Push exactly `maxsize` new distinct names through without ever touching
    # "seed-model" again -- LRU eviction must push it out to make room.
    for i in range(st._MODEL_RESOLUTION_CACHE_MAXSIZE):
        st._resolve_catalog_model(f"filler-model-{i}")
    assert len(calls) == 1 + st._MODEL_RESOLUTION_CACHE_MAXSIZE

    # An evicted name is not a correctness bug (it just resolves again) --
    # the assertion that matters is that it *does* resolve again rather than
    # silently reusing a slot it no longer legitimately owns.
    st._resolve_catalog_model("seed-model")
    assert len(calls) == 2 + st._MODEL_RESOLUTION_CACHE_MAXSIZE
