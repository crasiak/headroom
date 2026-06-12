from __future__ import annotations

import json

from headroom.proxy.server import HeadroomProxy, ProxyConfig


def test_resolve_claude_profile_company(tmp_path, monkeypatch):
    claude_seed = tmp_path / "s.json"
    claude_seed.write_text(json.dumps({"env": {
        "ANTHROPIC_BEDROCK_BASE_URL": "https://ap/bedrock",
        "CLAUDE_CODE_USE_BEDROCK": "1",
    }}))
    seed_dir = tmp_path / "cx"; seed_dir.mkdir()
    (seed_dir / "config.toml").write_text(
        'model_provider="corelight"\n[model_providers.corelight]\nbase_url="https://ap/v1"\n')
    (tmp_path / "profiles.toml").write_text(
        f'[profiles]\ndefault="personal"\n[profiles.company]\n'
        f'claude_seed="{claude_seed}"\ncodex_seed="{seed_dir}"\n')
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))

    from headroom.cli.wrap import _resolve_claude_profile
    rp = _resolve_claude_profile(flag="company", bedrock_base_url=None)
    assert rp.bedrock_base_url == "https://ap/bedrock"
    assert rp.openai_upstream == "https://ap"
    import zlib
    assert rp.port == 8788 + (zlib.crc32(b"company") % 1000)


def test_resolve_codex_profile_company(tmp_path, monkeypatch):
    from pathlib import Path
    from headroom.cli.wrap import _resolve_codex_profile

    seed_dir = tmp_path / "cx"; seed_dir.mkdir()
    (seed_dir / "config.toml").write_text(
        'model="gpt-5.5"\nmodel_provider="corelight"\n'
        '[model_providers.corelight]\nbase_url="https://ap/v1"\nwire_api="responses"\n')
    (tmp_path / "profiles.toml").write_text(
        f'[profiles]\ndefault="personal"\n[profiles.company]\ncodex_seed="{seed_dir}"\n')
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))

    rp, owned_home = _resolve_codex_profile(flag="company")
    assert rp.openai_upstream == "https://ap"
    assert owned_home == Path(tmp_path) / "codex" / "company"
    written = owned_home / "config.toml"
    assert written.exists()
    assert f'base_url = "http://127.0.0.1:{rp.port}/v1"' in written.read_text()
    # seed untouched
    assert 'https://ap/v1' in (seed_dir / "config.toml").read_text()


def test_health_payload_exposes_bedrock_api_url():
    proxy = HeadroomProxy(
        ProxyConfig(bedrock_base_url="https://ap/bedrock", cache_enabled=False,
                    rate_limit_enabled=False)
    )
    from headroom.proxy import server as srv
    payload = srv._build_health_config(proxy.config)   # module-level health helper
    assert payload["bedrock_api_url"] == "https://ap/bedrock"
    assert payload["openai_api_url"] == proxy.config.openai_api_url
