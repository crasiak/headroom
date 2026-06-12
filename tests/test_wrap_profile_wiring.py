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


def test_health_payload_exposes_bedrock_api_url():
    proxy = HeadroomProxy(
        ProxyConfig(bedrock_base_url="https://ap/bedrock", cache_enabled=False,
                    rate_limit_enabled=False)
    )
    from headroom.proxy import server as srv
    payload = srv._build_health_config(proxy.config)   # module-level health helper
    assert payload["bedrock_api_url"] == "https://ap/bedrock"
    assert payload["openai_api_url"] == proxy.config.openai_api_url
