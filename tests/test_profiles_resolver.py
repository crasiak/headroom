from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest
import tomllib

from headroom.cli.profiles import (
    ResolvedProfile,
    _profile_port,
    _strip_v1,
    resolve_profile,
    select_profile_name,
)


def test_strip_v1():
    assert _strip_v1("https://h/v1") == "https://h"
    assert _strip_v1("https://h/v1/") == "https://h"
    assert _strip_v1("https://h") == "https://h"
    assert _strip_v1(None) is None


def test_profile_port_default_pins_8787():
    assert _profile_port("personal", default_profile="personal") == 8787


def test_profile_port_deterministic_and_stable():
    p1 = _profile_port("company", default_profile="personal")
    p2 = _profile_port("company", default_profile="personal")
    assert p1 == p2 == 8788 + (zlib.crc32(b"company") % 1000)
    assert p1 != 8787


def test_profile_port_default_pinning_is_relative():
    # whichever profile is default pins to 8787; others are crc32-derived
    assert _profile_port("company", default_profile="company") == 8787
    assert _profile_port("personal", default_profile="company") == 8788 + (zlib.crc32(b"personal") % 1000)


def test_profile_explicit_port_wins(tmp_path):
    _write_profiles(tmp_path, """
[profiles]
default = "personal"
[profiles.company]
port = 9123
codex_seed = "{seed}"
""".replace("{seed}", str(_codex_seed(tmp_path))))
    rp = resolve_profile("company", profiles_path=tmp_path / "profiles.toml")
    assert rp.port == 9123


def test_resolve_company_reads_both_seeds(tmp_path):
    claude_seed = tmp_path / "settings.corelight.json"
    claude_seed.write_text(json.dumps({"env": {
        "ANTHROPIC_BEDROCK_BASE_URL": "https://ai.taileb6e.ts.net/bedrock",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "x",
    }}))
    seed_dir = _codex_seed(tmp_path)
    _write_profiles(tmp_path, f"""
[profiles]
default = "personal"
[profiles.company]
claude_seed = "{claude_seed}"
codex_seed = "{seed_dir}"
""")
    rp = resolve_profile("company", profiles_path=tmp_path / "profiles.toml")
    assert rp.name == "company"
    assert rp.bedrock_base_url == "https://ai.taileb6e.ts.net/bedrock"
    assert rp.claude_env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert rp.claude_env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "x"
    assert rp.openai_upstream == "https://ai.taileb6e.ts.net"   # /v1 stripped
    assert rp.codex_seed_dir == str(seed_dir)
    assert rp.port == 8788 + (zlib.crc32(b"company") % 1000)


def test_resolve_personal_no_claude_seed(tmp_path):
    seed_dir = _codex_seed(tmp_path, provider=None)  # pristine default-openai config
    _write_profiles(tmp_path, f"""
[profiles]
default = "personal"
[profiles.personal]
codex_seed = "{seed_dir}"
""")
    rp = resolve_profile("personal", profiles_path=tmp_path / "profiles.toml")
    assert rp.bedrock_base_url is None
    assert rp.claude_env == {}
    assert rp.openai_upstream is None         # no custom provider -> default OpenAI
    assert rp.codex_seed_dir == str(seed_dir)
    assert rp.port == 8787                     # it is the default profile


def test_missing_seed_file_raises(tmp_path):
    _write_profiles(tmp_path, """
[profiles]
default = "company"
[profiles.company]
codex_seed = "/no/such/dir"
""")
    with pytest.raises(FileNotFoundError) as exc:
        resolve_profile("company", profiles_path=tmp_path / "profiles.toml")
    assert "/no/such/dir" in str(exc.value)


def test_unknown_profile_lists_available(tmp_path):
    _write_profiles(tmp_path, """
[profiles]
default = "personal"
[profiles.personal]
[profiles.company]
""")
    with pytest.raises(KeyError) as exc:
        resolve_profile("nope", profiles_path=tmp_path / "profiles.toml")
    assert "personal" in str(exc.value) and "company" in str(exc.value)


def test_select_profile_precedence(monkeypatch):
    # flag > env > config default
    assert select_profile_name(flag="company", env={"HEADROOM_PROFILE": "x"},
                                config_default="personal") == "company"
    assert select_profile_name(flag=None, env={"HEADROOM_PROFILE": "envp"},
                                config_default="personal") == "envp"
    assert select_profile_name(flag=None, env={}, config_default="personal") == "personal"
    assert select_profile_name(flag=None, env={}, config_default=None) == "personal"


def test_already_wrapped_codex_seed_returns_none(tmp_path):
    d = tmp_path / "cx"; d.mkdir()
    (d / "config.toml").write_text(
        'model_provider="headroom"\n[model_providers.headroom]\nbase_url="http://127.0.0.1:8787/v1"\n')
    _write_profiles(tmp_path, f'[profiles]\ndefault="personal"\n[profiles.personal]\ncodex_seed="{d}"\n')
    rp = resolve_profile("personal", profiles_path=tmp_path / "profiles.toml")
    assert rp.openai_upstream is None


def test_codex_seed_provider_not_in_table_falls_back(tmp_path):
    d = tmp_path / "cx"; d.mkdir()
    (d / "config.toml").write_text('model_provider="ghost"\nopenai_base_url="https://h/v1"\n')
    _write_profiles(tmp_path, f'[profiles]\ndefault="personal"\n[profiles.personal]\ncodex_seed="{d}"\n')
    rp = resolve_profile("personal", profiles_path=tmp_path / "profiles.toml")
    assert rp.openai_upstream == "https://h"


def test_profile_with_no_seeds_resolves_all_none(tmp_path):
    _write_profiles(tmp_path, '[profiles]\ndefault="personal"\n[profiles.personal]\n')
    rp = resolve_profile("personal", profiles_path=tmp_path / "profiles.toml")
    assert rp.bedrock_base_url is None and rp.openai_upstream is None and rp.codex_seed_dir is None


def test_strip_v1_only_strips_one_suffix():
    assert _strip_v1("https://h/v1/v1") == "https://h/v1"


# --- helpers ---
def _write_profiles(tmp_path: Path, body: str) -> None:
    (tmp_path / "profiles.toml").write_text(body)


def test_ensure_starter_profiles_writes_template(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))
    from headroom.cli.profiles import default_profiles_path, ensure_starter_profiles
    path = ensure_starter_profiles()
    assert path == default_profiles_path()
    assert path.exists()
    body = path.read_text()
    assert "[profiles]" in body and 'default = "personal"' in body
    assert "[profiles.personal]" in body and "[profiles.company]" in body
    # template must stay valid TOML
    doc = tomllib.loads(body)
    assert doc["profiles"]["default"] == "personal"
    assert isinstance(doc["profiles"]["personal"], dict)
    assert isinstance(doc["profiles"]["company"], dict)
    # idempotent: second call does not overwrite
    path.write_text(body + "\n# edited\n")
    ensure_starter_profiles()
    assert "# edited" in path.read_text()


def _codex_seed(tmp_path: Path, provider: str | None = "corelight") -> Path:
    d = tmp_path / "codexseed"
    d.mkdir(exist_ok=True)
    if provider:
        (d / "config.toml").write_text(
            f'model = "gpt-5.5"\n'
            f'model_provider = "{provider}"\n'
            f'[model_providers.{provider}]\n'
            f'base_url = "https://ai.taileb6e.ts.net/v1"\n'
            f'wire_api = "responses"\n'
        )
    else:
        (d / "config.toml").write_text('model = "gpt-5.5"\n')
    return d
