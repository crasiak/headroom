from __future__ import annotations

from headroom.cli.wrap import _apply_bedrock_child_env, _detect_bedrock_aperture


def test_autodetect_returns_url_when_corelight_env_present():
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://ai.taileb6e.ts.net/bedrock",
    }
    assert _detect_bedrock_aperture(env, flag_override=None) == "https://ai.taileb6e.ts.net/bedrock"


def test_autodetect_absent_when_use_bedrock_unset():
    env = {"ANTHROPIC_BEDROCK_BASE_URL": "https://ai.taileb6e.ts.net/bedrock"}
    assert _detect_bedrock_aperture(env, flag_override=None) is None


def test_autodetect_absent_when_base_url_unset():
    env = {"CLAUDE_CODE_USE_BEDROCK": "1"}
    assert _detect_bedrock_aperture(env, flag_override=None) is None


def test_flag_override_forces_and_wins():
    env = {}
    assert _detect_bedrock_aperture(env, flag_override="https://forced.example/bedrock") == (
        "https://forced.example/bedrock"
    )


def test_flag_override_beats_env():
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://ai.taileb6e.ts.net/bedrock",
    }
    assert _detect_bedrock_aperture(env, flag_override="https://forced.example/bedrock") == (
        "https://forced.example/bedrock"
    )


def test_falsy_use_bedrock_not_detected():
    env = {"CLAUDE_CODE_USE_BEDROCK": "0",
           "ANTHROPIC_BEDROCK_BASE_URL": "https://ai.taileb6e.ts.net/bedrock"}
    assert _detect_bedrock_aperture(env, flag_override=None) is None


def test_apply_bedrock_child_env_rewrites_when_engaged():
    env = {}
    local = _apply_bedrock_child_env(env, "https://ai.taileb6e.ts.net/bedrock", 8788)
    assert local == "http://127.0.0.1:8788"
    assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://127.0.0.1:8788"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"


def test_apply_bedrock_child_env_noop_when_disengaged():
    env = {"FOO": "bar"}
    assert _apply_bedrock_child_env(env, None, 8788) is None
    assert "ANTHROPIC_BEDROCK_BASE_URL" not in env


def test_apply_bedrock_child_env_preserves_existing_flags():
    env = {"CLAUDE_CODE_SKIP_BEDROCK_AUTH": "0"}
    _apply_bedrock_child_env(env, "https://x/bedrock", 9000)
    assert env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "0"  # setdefault preserves operator value
    assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://127.0.0.1:9000"  # force-set


def test_start_proxy_forwards_bedrock_flag(monkeypatch):
    """_start_proxy passes --bedrock-base-url and sets BEDROCK_TARGET_API_URL."""
    import headroom.cli.wrap as wrap

    captured = {}

    class _FakeProc:
        returncode = None

        def poll(self):
            return None

        def kill(self):
            pass

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr(wrap.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wrap, "_check_proxy", lambda port: True)
    monkeypatch.setattr(wrap.time, "sleep", lambda *_: None)

    wrap._start_proxy(
        9999, agent_type="claude", bedrock_api_url="https://aperture.test/bedrock"
    )
    assert "--bedrock-base-url" in captured["cmd"]
    i = captured["cmd"].index("--bedrock-base-url")
    assert captured["cmd"][i + 1] == "https://aperture.test/bedrock"
    assert captured["env"]["BEDROCK_TARGET_API_URL"] == "https://aperture.test/bedrock"
