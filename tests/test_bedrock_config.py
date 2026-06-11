from __future__ import annotations

from headroom.proxy.models import ProxyConfig


def test_bedrock_config_defaults():
    cfg = ProxyConfig()
    assert cfg.bedrock_base_url is None
    assert cfg.bedrock_compression == "aggressive"


def test_bedrock_config_set():
    cfg = ProxyConfig(
        bedrock_base_url="https://ai.taileb6e.ts.net/bedrock",
        bedrock_compression="lossless",
    )
    assert cfg.bedrock_base_url == "https://ai.taileb6e.ts.net/bedrock"
    assert cfg.bedrock_compression == "lossless"
