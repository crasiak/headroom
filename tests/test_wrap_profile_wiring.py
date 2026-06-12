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


def test_launch_tool_accepts_bedrock_api_url():
    """_launch_tool must accept bedrock_api_url (it forwards it to _ensure_proxy).

    Before the fix this raised NameError because the parameter was missing
    from the signature while the body referenced it.
    """
    import os
    import signal

    import pytest

    from headroom.cli.wrap import _launch_tool

    saved_int = signal.getsignal(signal.SIGINT)
    saved_term = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.raises(SystemExit) as excinfo:
            _launch_tool(
                binary="/usr/bin/true",
                args=(),
                env=os.environ.copy(),
                port=8999,
                no_proxy=True,  # _ensure_proxy only probes the port; starts nothing
                tool_label="X",
                env_vars_display=[],
                bedrock_api_url="https://ap/bedrock",
            )
        assert excinfo.value.code == 0
    finally:
        signal.signal(signal.SIGINT, saved_int)
        signal.signal(signal.SIGTERM, saved_term)


def _company_codex_workspace(tmp_path):
    """Build a workspace dir with a profiles.toml + codex seed (company profile)."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seed_dir = tmp_path / "cx"
    seed_dir.mkdir()
    (seed_dir / "config.toml").write_text(
        'model="gpt-5.5"\nmodel_provider="corelight"\n'
        '[model_providers.corelight]\nbase_url="https://ap/v1"\nwire_api="responses"\n')
    (workspace / "profiles.toml").write_text(
        f'[profiles]\ndefault="personal"\n[profiles.company]\ncodex_seed="{seed_dir}"\n')
    return workspace


def test_codex_profile_mode_leaves_user_codex_untouched(tmp_path, monkeypatch):
    """In profile mode the user's ~/.codex must stay byte-identical."""
    import zlib

    from click.testing import CliRunner

    from headroom.cli.wrap import codex

    fake_home = tmp_path / "home"
    user_codex = fake_home / ".codex"
    user_codex.mkdir(parents=True)
    sentinel = 'model = "user-owned-do-not-touch"\n'
    (user_codex / "config.toml").write_text(sentinel)

    workspace = _company_codex_workspace(tmp_path)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))
    monkeypatch.delenv("HEADROOM_PROFILE", raising=False)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(codex, ["--profile", "company", "--prepare-only"])
    assert result.exit_code == 0, result.output

    # ~/.codex byte-identical: same single file, same content.
    assert [p.name for p in user_codex.iterdir()] == ["config.toml"]
    assert (user_codex / "config.toml").read_text() == sentinel

    # Owned config materialized with the company proxy port.
    owned_config = workspace / "codex" / "company" / "config.toml"
    assert owned_config.exists()
    company_port = 8788 + (zlib.crc32(b"company") % 1000)
    assert f'base_url = "http://127.0.0.1:{company_port}/v1"' in owned_config.read_text()


def test_resolve_codex_profile_port_override(tmp_path, monkeypatch):
    """--port must win: the owned config must point at the override, not crc32."""
    import zlib

    from headroom.cli.wrap import _resolve_codex_profile

    seed_dir = tmp_path / "cx"
    seed_dir.mkdir()
    (seed_dir / "config.toml").write_text(
        'model="gpt-5.5"\nmodel_provider="corelight"\n'
        '[model_providers.corelight]\nbase_url="https://ap/v1"\nwire_api="responses"\n')
    (tmp_path / "profiles.toml").write_text(
        f'[profiles]\ndefault="personal"\n[profiles.company]\ncodex_seed="{seed_dir}"\n')
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))

    rp, owned_home = _resolve_codex_profile(flag="company", port_override=9999)
    assert rp.port == 9999
    written = (owned_home / "config.toml").read_text()
    assert 'base_url = "http://127.0.0.1:9999/v1"' in written
    crc32_port = 8788 + (zlib.crc32(b"company") % 1000)
    assert f"127.0.0.1:{crc32_port}" not in written


def test_codex_profile_mode_port_flag_wins(tmp_path, monkeypatch):
    """CLI: codex --profile company --port 9999 writes 9999 into the owned config."""
    import zlib

    from click.testing import CliRunner

    from headroom.cli.wrap import codex

    fake_home = tmp_path / "home"
    user_codex = fake_home / ".codex"
    user_codex.mkdir(parents=True)
    sentinel = 'model = "user-owned-do-not-touch"\n'
    (user_codex / "config.toml").write_text(sentinel)

    workspace = _company_codex_workspace(tmp_path)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))
    monkeypatch.delenv("HEADROOM_PROFILE", raising=False)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            codex, ["--profile", "company", "--prepare-only", "--port", "9999"]
        )
    assert result.exit_code == 0, result.output

    owned_config = workspace / "codex" / "company" / "config.toml"
    assert owned_config.exists()
    written = owned_config.read_text()
    assert 'base_url = "http://127.0.0.1:9999/v1"' in written
    crc32_port = 8788 + (zlib.crc32(b"company") % 1000)
    assert f"127.0.0.1:{crc32_port}" not in written

    # ~/.codex untouched.
    assert (user_codex / "config.toml").read_text() == sentinel


def test_codex_profile_mode_memory_disabled(tmp_path, monkeypatch):
    """Profile mode is compression-only (v1): _launch_tool must get memory=False
    even when --memory was passed, the skip must be echoed, and ~/.codex must
    stay untouched (no memory MCP registration)."""
    from click.testing import CliRunner

    from headroom.cli import wrap as wrap_mod
    from headroom.cli.wrap import codex

    fake_home = tmp_path / "home"
    user_codex = fake_home / ".codex"
    user_codex.mkdir(parents=True)
    sentinel = 'model = "user-owned-do-not-touch"\n'
    (user_codex / "config.toml").write_text(sentinel)

    workspace = _company_codex_workspace(tmp_path)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))
    monkeypatch.delenv("HEADROOM_PROFILE", raising=False)

    calls = {}

    def fake_launch_tool(**kwargs):
        calls.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(wrap_mod, "_launch_tool", fake_launch_tool)
    monkeypatch.setattr(wrap_mod.shutil, "which", lambda name: "/usr/bin/true")

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(codex, ["--profile", "company", "--memory"])
    assert result.exit_code == 0, result.output
    assert "skipping rtk/MCP/Serena/memory" in result.output  # the profile-mode skip echo
    assert calls, "_launch_tool was not called"
    assert calls["memory"] is False
    # No memory MCP write to the user's ~/.codex.
    assert (user_codex / "config.toml").read_text() == sentinel


def test_codex_legacy_prepare_only_uses_default_port(tmp_path, monkeypatch):
    """Legacy path (no profiles file) must write port 8787, never 'None'."""
    from click.testing import CliRunner

    from headroom.cli.wrap import codex

    fake_home = tmp_path / "home"
    # Pre-create ~/.codex so CodexRegistrar.detect() lets headroom MCP setup
    # run — that's where the literal 'http://127.0.0.1:None' corruption bit.
    (fake_home / ".codex").mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()  # empty: no profiles.toml -> legacy path
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))
    monkeypatch.delenv("HEADROOM_PROFILE", raising=False)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            codex, ["--prepare-only", "--no-rtk", "--no-serena"]
        )
    assert result.exit_code == 0, result.output

    cfg = (fake_home / ".codex" / "config.toml").read_text()
    assert "8787" in cfg
    assert "None" not in cfg
