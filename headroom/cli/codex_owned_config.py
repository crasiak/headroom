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
import tomllib
from pathlib import Path

from headroom.cli.wrap import _strip_codex_headroom_blocks


def build_owned_codex_config(seed_text: str, port: int) -> str:
    cleaned = _strip_codex_headroom_blocks(seed_text)
    proxy_v1 = f"http://127.0.0.1:{port}/v1"

    doc = tomllib.loads(cleaned)
    provider = doc.get("model_provider")
    providers = doc.get("model_providers") or {}

    if isinstance(provider, str) and provider in providers and "base_url" in providers[provider]:
        # Rewrite the active provider's base_url line in place (text-level so we
        # preserve comments/formatting/other keys). The base_url lives inside the
        # [model_providers.<provider>] table.
        return _rewrite_provider_base_url(cleaned, provider, proxy_v1)

    # Default-OpenAI seed: inject the top-level override at the front (bare keys
    # must precede any [section]).
    override = f'openai_base_url = "{proxy_v1}"\n'
    body = cleaned.strip()
    return f"{override}{body}\n" if body else override


def _rewrite_provider_base_url(text: str, provider: str, proxy_v1: str) -> str:
    """Replace base_url within the [model_providers.<provider>] table only."""
    lines = text.splitlines(keepends=True)
    in_table = False
    header = f"[model_providers.{provider}]"
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_table = stripped == header
        if in_table and re.match(r"\s*base_url\s*=", line):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f'{indent}base_url = "{proxy_v1}"\n')
            continue
        out.append(line)
    return "".join(out)


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
