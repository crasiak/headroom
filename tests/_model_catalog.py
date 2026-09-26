"""Build in-memory model catalogs for tests that swap the pinned one out."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from headroom.pricing.model_catalog import ModelCatalog


def fake_catalog(
    models: Mapping[str, Mapping[str, Any]], *, unresolvable: Iterable[str] = ()
) -> ModelCatalog:
    """A catalog holding exactly ``models``; ``unresolvable`` mimics SDK-probe rejects."""
    return ModelCatalog(
        models=MappingProxyType({name: MappingProxyType(dict(v)) for name, v in models.items()}),
        public_models=frozenset(models),
        unresolvable=frozenset(unresolvable),
        litellm_version="test",
    )
