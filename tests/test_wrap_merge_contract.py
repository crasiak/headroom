"""Regression contracts for profile/upstream launch composition; no live tools."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from headroom.cli import wrap as wrap_mod
from tests.test_cli.test_wrap_claude_vertex_proxy_env import _invoke_wrap_claude
from tests.test_wrap_profile_wiring import _company_codex_workspace


@pytest.fixture(autouse=True)
def private_launch_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in ("HEADROOM_PROFILE", "HEADROOM_PORT", "HEADROOM_BACKEND", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(wrap_mod, "_claude_managed_settings_env", lambda path: {})


def _claude_profile(tmp_path, monkeypatch, seed_env):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seed = tmp_path / "seed.json"
    seed.write_text(json.dumps({"env": seed_env}))
    (workspace / "profiles.toml").write_text(
        f'[profiles]\ndefault="personal"\n[profiles.personal]\nclaude_seed="{seed}"\nport=9111\n'
    )
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))


@pytest.mark.parametrize("seed_bedrock", [False, True])
def test_claude_backend_uses_profile_adjusted_shell(tmp_path, monkeypatch, seed_bedrock):
    seed = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-west-2"} if seed_bedrock else {}
    _claude_profile(tmp_path, monkeypatch, seed)
    captured, _ = _invoke_wrap_claude(
        CliRunner(),
        monkeypatch,
        env={} if seed_bedrock else {"CLAUDE_CODE_USE_BEDROCK": "1"},
        extra_args=("--profile", "personal"),
    )
    assert captured["ensure_kwargs"].get("backend") == ("bedrock" if seed_bedrock else None)
    assert captured["child_env"].get("CLAUDE_CODE_USE_BEDROCK") == ("0" if seed_bedrock else None)


def test_claude_explicit_port_environment_wins_over_profile(tmp_path, monkeypatch):
    _claude_profile(tmp_path, monkeypatch, {})
    captured, _ = _invoke_wrap_claude(
        CliRunner(),
        monkeypatch,
        env={"HEADROOM_PORT": "9123"},
        extra_args=("--profile", "personal"),
    )
    assert captured["ensure_args"][0] == 9123
    assert captured["child_env"]["HEADROOM_PORT"] == "9123"


def test_codex_explicit_port_environment_wins_over_profile(tmp_path, monkeypatch):
    workspace = _company_codex_workspace(tmp_path)
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))
    result = CliRunner().invoke(
        wrap_mod.codex,
        ["--profile", "company", "--prepare-only"],
        env={"HEADROOM_PORT": "9123"},
    )
    assert result.exit_code == 0, result.output
    assert (
        'base_url = "http://127.0.0.1:9123/v1"'
        in (workspace / "codex/company/config.toml").read_text()
    )


def test_legacy_default_port_still_uses_upstream_discovery(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))
    monkeypatch.setattr(wrap_mod, "reconcile_default_port", lambda port: 9177, raising=False)
    captured = {}
    monkeypatch.setattr(wrap_mod, "_run_codex_wrap", lambda **kw: captured.update(kw))
    result = CliRunner().invoke(wrap_mod.codex, ["--prepare-only"])
    assert result.exit_code == 0, result.output
    assert captured["port"] == 9177


def test_codex_owned_routing_rebinds_after_listener_fallback(tmp_path, monkeypatch):
    workspace = _company_codex_workspace(tmp_path)
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(workspace))
    monkeypatch.setattr(wrap_mod.shutil, "which", lambda name: "/usr/bin/true")
    seed = tmp_path / "cx/config.toml"
    original = seed.read_bytes()

    def launch(**kw):
        callback = kw.get("configure_launch")
        assert callable(callback), "owned config must follow the actual listener"
        callback(9155, kw["args"], kw["env"], kw["env_vars_display"])

    monkeypatch.setattr(wrap_mod, "_launch_tool", launch)
    result = CliRunner().invoke(wrap_mod.codex, ["--profile", "company", "--port", "9111"])
    assert result.exit_code == 0, result.output
    assert (
        'base_url = "http://127.0.0.1:9155/v1"'
        in (workspace / "codex/company/config.toml").read_text()
    )
    assert seed.read_bytes() == original
    assert not (Path.home() / ".codex/config.toml").exists()
