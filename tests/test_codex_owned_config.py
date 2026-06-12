from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

from headroom.cli.codex_owned_config import (
    build_owned_codex_config,
    link_shared_skills,
    write_codex_owned_config,
)


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


def _shared_store(tmp_path: Path) -> Path:
    shared = tmp_path / "dotcodex" / "skills"
    (shared / "glab").mkdir(parents=True)
    (shared / "glab" / "SKILL.md").write_text("# glab")
    return shared


def test_link_shared_skills_creates_symlink(tmp_path: Path):
    shared = _shared_store(tmp_path)
    owned = tmp_path / "owned"
    owned.mkdir()

    link = link_shared_skills(owned, shared_skills=shared)

    assert link == owned / "skills"
    assert link.is_symlink() and link.resolve() == shared.resolve()
    # skills visible through the link
    assert (link / "glab" / "SKILL.md").read_text() == "# glab"


def test_link_shared_skills_idempotent(tmp_path: Path):
    shared = _shared_store(tmp_path)
    owned = tmp_path / "owned"
    owned.mkdir()
    link_shared_skills(owned, shared_skills=shared)

    link = link_shared_skills(owned, shared_skills=shared)

    assert link is not None and link.is_symlink()
    assert link.resolve() == shared.resolve()


def test_link_shared_skills_replaces_empty_local_dir(tmp_path: Path):
    shared = _shared_store(tmp_path)
    owned = tmp_path / "owned"
    (owned / "skills").mkdir(parents=True)  # codex auto-creates this empty

    link = link_shared_skills(owned, shared_skills=shared)

    assert link is not None and link.is_symlink()
    assert link.resolve() == shared.resolve()


def test_link_shared_skills_replaces_dir_with_only_codex_system_boilerplate(tmp_path: Path):
    # Codex auto-creates skills/.system in every home; it must not count as a
    # local install (the shared store has its own copy).
    shared = _shared_store(tmp_path)
    owned = tmp_path / "owned"
    system = owned / "skills" / ".system" / "skill-creator"
    system.mkdir(parents=True)

    link = link_shared_skills(owned, shared_skills=shared)

    assert link is not None and link.is_symlink()
    assert link.resolve() == shared.resolve()


def test_link_shared_skills_preserves_local_installs(tmp_path: Path):
    shared = _shared_store(tmp_path)
    owned = tmp_path / "owned"
    local = owned / "skills" / "my-local-skill"
    local.mkdir(parents=True)

    link = link_shared_skills(owned, shared_skills=shared)

    assert link is None
    assert not (owned / "skills").is_symlink()
    assert local.is_dir()  # untouched


def test_link_shared_skills_no_store_no_dangling_link(tmp_path: Path):
    owned = tmp_path / "owned"
    owned.mkdir()

    link = link_shared_skills(owned, shared_skills=tmp_path / "absent")

    assert link is None
    assert not (owned / "skills").exists()
    assert not (owned / "skills").is_symlink()


def test_link_shared_skills_repoints_stale_symlink(tmp_path: Path):
    shared = _shared_store(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "skills").symlink_to(elsewhere)

    link = link_shared_skills(owned, shared_skills=shared)

    assert link is not None and link.is_symlink()
    assert link.resolve() == shared.resolve()
