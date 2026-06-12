from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from headroom.cli.codex_owned_config import build_owned_codex_config, write_codex_owned_config


def _parse(text: str) -> dict:
    return tomllib.loads(text)


def test_company_seed_rewrites_provider_base_url_preserving_wire_api():
    seed = (
        'model = "gpt-5.5"\n'
        'model_provider = "corelight"\n'
        '[model_providers.corelight]\n'
        'name = "Corelight Tailscale Gateway"\n'
        'base_url = "https://ai.taileb6e.ts.net/v1"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = false\n'
    )
    out = build_owned_codex_config(seed, port=8830)
    doc = _parse(out)
    assert doc["model_provider"] == "corelight"
    prov = doc["model_providers"]["corelight"]
    assert prov["base_url"] == "http://127.0.0.1:8830/v1"   # rewritten
    assert prov["wire_api"] == "responses"                   # preserved
    assert prov["requires_openai_auth"] is False             # preserved
    assert doc["model"] == "gpt-5.5"                          # preserved


def test_personal_default_openai_seed_injects_override():
    seed = 'model = "gpt-5.5"\n'
    out = build_owned_codex_config(seed, port=8787)
    doc = _parse(out)
    # default-openai seed gets the proxy override so subscription traffic routes
    assert doc["openai_base_url"] == "http://127.0.0.1:8787/v1"
    assert doc["model"] == "gpt-5.5"


def test_already_wrapped_seed_is_neutralized_then_rewritten():
    seed = (
        'model_provider = "headroom"\n'
        'openai_base_url = "http://127.0.0.1:9999/v1"\n'
        'model = "gpt-5.5"\n'
        'model_provider = "corelight"\n'
        '[model_providers.corelight]\n'
        'base_url = "https://ai.taileb6e.ts.net/v1"\n'
        'wire_api = "responses"\n'
    )
    out = build_owned_codex_config(seed, port=8830)
    # no stale 9999 reference survives
    assert "9999" not in out
    doc = _parse(out)
    assert doc["model_providers"]["corelight"]["base_url"] == "http://127.0.0.1:8830/v1"


def test_write_codex_owned_config_does_not_touch_seed(tmp_path: Path):
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    seed_cfg = seed_dir / "config.toml"
    seed_text = (
        'model = "gpt-5.5"\n'
        'model_provider = "corelight"\n'
        '[model_providers.corelight]\n'
        'base_url = "https://ai.taileb6e.ts.net/v1"\n'
        'wire_api = "responses"\n'
    )
    seed_cfg.write_text(seed_text)
    auth = seed_dir / "auth.json"
    auth.write_text('{"token": "x"}')

    owned_dir = tmp_path / "owned"
    written = write_codex_owned_config(seed_dir, owned_dir, port=8830)

    assert written == owned_dir / "config.toml"
    assert "8830" in written.read_text()
    assert seed_cfg.read_text() == seed_text                 # seed byte-identical
    # auth.json carried over (symlink or copy) so login persists
    assert (owned_dir / "auth.json").exists()
    assert (owned_dir / "auth.json").read_text() == '{"token": "x"}'


def test_trailing_comment_header_still_rewrites():
    seed = (
        'model_provider = "corelight"\n'
        '[model_providers.corelight] # prod gateway\n'
        'base_url = "https://ap/v1"\n'
        'wire_api = "responses"\n'
    )
    out = build_owned_codex_config(seed, port=8830)
    assert tomllib.loads(out)["model_providers"]["corelight"]["base_url"] == "http://127.0.0.1:8830/v1"


def test_existing_openai_base_url_is_replaced_not_duplicated():
    seed = 'openai_base_url = "https://myproxy.example.com/v1"\nmodel = "gpt-5.5"\n'
    out = build_owned_codex_config(seed, port=8787)
    assert out.count("openai_base_url") == 1
    assert tomllib.loads(out)["openai_base_url"] == "http://127.0.0.1:8787/v1"


def test_custom_provider_without_base_url_raises():
    seed = 'model_provider = "corelight"\n[model_providers.corelight]\nwire_api = "responses"\n'
    with pytest.raises(ValueError):
        build_owned_codex_config(seed, port=8830)


def test_inline_table_provider_fails_loudly():
    seed = (
        'model_provider = "corelight"\n'
        '[model_providers]\n'
        'corelight = { base_url = "https://ap/v1", wire_api = "responses" }\n'
    )
    with pytest.raises((RuntimeError, ValueError)):
        build_owned_codex_config(seed, port=8830)
