from __future__ import annotations

from headroom.proxy.server import HeadroomProxy, ProxyConfig


def test_health_payload_exposes_bedrock_api_url():
    proxy = HeadroomProxy(
        ProxyConfig(bedrock_base_url="https://ap/bedrock", cache_enabled=False,
                    rate_limit_enabled=False)
    )
    from headroom.proxy import server as srv
    payload = srv._build_health_config(proxy.config)   # module-level health helper
    assert payload["bedrock_api_url"] == "https://ap/bedrock"
    assert payload["openai_api_url"] == proxy.config.openai_api_url
