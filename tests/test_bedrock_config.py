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


from click.testing import CliRunner


def test_env_wires_bedrock_base_url(monkeypatch):
    from headroom.proxy.server import _proxy_config_from_env

    # Clear the multi-worker JSON config env so the env-var path is taken.
    monkeypatch.delenv("HEADROOM_PROXY_CONFIG_JSON", raising=False)
    monkeypatch.setenv("BEDROCK_TARGET_API_URL", "https://aperture.test/bedrock")
    cfg = _proxy_config_from_env()
    assert cfg.bedrock_base_url == "https://aperture.test/bedrock"


def test_click_proxy_accepts_bedrock_flags():
    from headroom.cli.proxy import proxy as proxy_cmd

    runner = CliRunner()
    result = runner.invoke(proxy_cmd, ["--help"])
    assert result.exit_code == 0
    assert "--bedrock-base-url" in result.output
    assert "--bedrock-compression" in result.output
