"""Build a headroom-owned Codex config.toml from a user seed config.

The owned config is a copy of the seed with the active provider's ``base_url``
rewritten to the local proxy, so Codex traffic flows through Headroom while the
user's seed file is never modified. Two seed shapes are handled:

* custom ``model_provider`` (e.g. corelight) -> rewrite that provider's
  ``base_url`` in place, preserving ``wire_api`` and other keys;
* default OpenAI seed (no custom provider) -> inject a top-level
  ``openai_base_url`` override (parity with ``_inject_codex_provider_config``),
  which is what catches ChatGPT-subscription traffic.

Prior in-place Headroom blocks in the seed are stripped first so a seed that was
itself wrapped does not leave a stale local-proxy URL behind.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import tomllib

from headroom.cli.wrap import _strip_codex_headroom_blocks


def build_owned_codex_config(seed_text: str, port: int) -> str:
    cleaned = _strip_codex_headroom_blocks(seed_text)
    proxy_v1 = f"http://127.0.0.1:{port}/v1"

    doc = tomllib.loads(cleaned)
    provider = doc.get("model_provider")
    providers = doc.get("model_providers") or {}

    if isinstance(provider, str) and provider in providers:
        # Rewrite the active provider's base_url line in place (text-level so we
        # preserve comments/formatting/other keys). The base_url lives inside the
        # [model_providers.<provider>] table.
        if "base_url" not in providers[provider]:
            raise ValueError(f"codex seed provider '{provider}' has no base_url to rewrite")
        result = _rewrite_provider_base_url(cleaned, provider, proxy_v1)
        _assert_routes_to_proxy(result, provider, proxy_v1)
        return result

    # Default-OpenAI seed: replace/inject the top-level override so subscription
    # traffic routes through the proxy.
    result = _set_top_level_openai_base_url(cleaned, proxy_v1)
    _assert_routes_to_proxy(result, None, proxy_v1)
    return result


def _rewrite_provider_base_url(text: str, provider: str, proxy_v1: str) -> str:
    """Replace base_url within the [model_providers.<provider>] table only."""
    lines = text.splitlines(keepends=True)
    in_table = False
    header = f"[model_providers.{provider}]"
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            header_part = stripped.split("#", 1)[0].rstrip()
            in_table = header_part == header
        if in_table and re.match(r"\s*base_url\s*=", line):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f'{indent}base_url = "{proxy_v1}"\n')
            continue
        out.append(line)
    return "".join(out)


def _set_top_level_openai_base_url(text: str, proxy_v1: str) -> str:
    """Replace a top-level openai_base_url (before any [section]); else prepend."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    replaced = False
    in_table = False
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            in_table = True
        if not in_table and re.match(r"\s*openai_base_url\s*=", line):
            out.append(f'openai_base_url = "{proxy_v1}"\n')
            replaced = True
            continue
        out.append(line)
    body = "".join(out)
    if replaced:
        return body
    body = body.strip()
    return (
        f'openai_base_url = "{proxy_v1}"\n{body}\n' if body else f'openai_base_url = "{proxy_v1}"\n'
    )


def _assert_routes_to_proxy(text: str, provider: str | None, proxy_v1: str) -> None:
    """Guarantee the built config actually routes to the proxy; raise loudly
    if a rewrite silently missed (e.g. inline-table provider syntax)."""
    doc = tomllib.loads(text)
    if provider is not None:
        actual = (doc.get("model_providers") or {}).get(provider, {}).get("base_url")
    else:
        actual = doc.get("openai_base_url")
    if actual != proxy_v1:
        raise RuntimeError(
            "codex owned-config did not route to the proxy "
            f"(expected {proxy_v1!r}, got {actual!r}). The seed config uses an "
            "unsupported shape (e.g. inline-table providers); edit the seed to "
            "use a standard [model_providers.<name>] table."
        )


def write_codex_owned_config(seed_dir: Path, owned_dir: Path, port: int) -> Path:
    """Write the owned config.toml under owned_dir; carry auth.json from the seed.

    Returns the path to the written config.toml. The seed dir is never modified.
    """
    owned_dir.mkdir(parents=True, exist_ok=True)
    seed_cfg = seed_dir / "config.toml"
    owned_cfg = owned_dir / "config.toml"
    owned_cfg.write_text(build_owned_codex_config(seed_cfg.read_text(), port))

    # Carry auth.json so an existing login persists into the owned CODEX_HOME.
    seed_auth = seed_dir / "auth.json"
    owned_auth = owned_dir / "auth.json"
    if seed_auth.exists():
        if owned_auth.exists() or owned_auth.is_symlink():
            owned_auth.unlink()
        shutil.copy2(seed_auth, owned_auth)
    return owned_cfg


# Entries under the default codex home (~/.codex) that every profile home
# shares via symlink, so installs/edits from any profile land in one place.
# Each is (name, kind); kind is "dir" or "file". config.toml and auth.json are
# deliberately NOT shared — they carry the per-profile backend and credentials.
_SHARED_CODEX_ENTRIES: tuple[tuple[str, str], ...] = (
    ("skills", "dir"),
    ("prompts", "dir"),
    ("AGENTS.md", "file"),
)

# Children of a shared dir that codex auto-creates in every home and that
# therefore don't count as a "local install" when judging emptiness.
_CODEX_DIR_BOILERPLATE: dict[str, set[str]] = {"skills": {".system"}}


def link_shared_codex_entries(
    owned_dir: Path, shared_root: Path | None = None
) -> dict[str, Path | None]:
    """Symlink each shareable entry in ``owned_dir`` at the user's default
    codex home (``~/.codex/<name>``), so skills/prompts/AGENTS.md edited or
    installed from any profile land in one shared place. Never writes into the
    shared store itself. Returns ``{name: Path | None}`` — None means not linked
    (no shared source, or a local copy is present and was left untouched).
    """
    if shared_root is None:
        shared_root = Path.home() / ".codex"
    return {
        name: _link_shared_entry(
            owned_dir / name,
            shared_root / name,
            kind,
            ignore=_CODEX_DIR_BOILERPLATE.get(name, frozenset()),
        )
        for name, kind in _SHARED_CODEX_ENTRIES
    }


def _link_shared_entry(
    link: Path, target: Path, kind: str, ignore: frozenset[str] | set[str] = frozenset()
) -> Path | None:
    """Point ``link`` at ``target`` (a shared file or dir). No-op returning None
    when the target is absent or ``link`` already holds local content."""
    if kind == "dir":
        if not target.is_dir():
            return None
    elif not target.is_file():
        return None

    # is_symlink() before exists()/is_dir(): a symlink to a dir is both.
    if link.is_symlink():
        if link.resolve() != target.resolve():
            link.unlink()
            link.symlink_to(target)
        return link
    if link.exists():
        if link.is_dir():
            if any(p.name not in ignore for p in link.iterdir()):
                return None  # local content -> profile stays independent
            shutil.rmtree(link)  # only boilerplate; replace with the link
        else:  # real file
            if link.stat().st_size > 0:
                return None  # local content -> leave it
            link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    return link


def link_shared_skills(owned_dir: Path, shared_skills: Path | None = None) -> Path | None:
    """Back-compat shim: link only ``owned_dir/skills`` at the shared skill
    store (default ``~/.codex/skills``). See ``link_shared_codex_entries``."""
    if shared_skills is None:
        shared_skills = Path.home() / ".codex" / "skills"
    return _link_shared_entry(owned_dir / "skills", shared_skills, "dir", ignore={".system"})
