# Profile-Based Wraps (`--profile`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `headroom wrap claude --profile <name>` / `headroom wrap codex --profile <name>` so the user can route each agent at a named backend (default `personal` = Claude Max + ChatGPT; `company` = corelight Bedrock aperture for Claude + aperture `/v1` Responses for Codex), with profiles defined in `~/.headroom/profiles.toml` and seeded from the user's existing config files.

**Architecture:** A pure resolver (`headroom/cli/profiles.py`) reads `profiles.toml` + the per-profile seed files and produces a `ResolvedProfile` (bedrock URL + claude env block, openai upstream + codex seed dir, deterministic port). The `claude`/`codex` wrap commands gain `--profile`, resolve it, start one profile-complete proxy (both `--bedrock-base-url` and `--openai-api-url`) on the profile's port, and configure the child: Claude via the existing `_apply_bedrock_child_env` machinery (now seed-driven), Codex via a headroom-owned `CODEX_HOME` (`~/.headroom/codex/<profile>/`) whose `config.toml` is the seed with its provider `base_url` rewritten to the proxy. The user's `~/.codex`, `~/.codex-corelight`, and `~/.claude/settings*.json` are never mutated.

**Tech Stack:** Python 3.11 (`tomllib` for read-only parse), Click CLI, pytest. Source of truth: `docs/superpowers/specs/2026-06-12-profile-wraps-personal-company-design.md`.

**Conventions:** Use `.venv/bin/python -m pytest` from the worktree root. Conventional commits ending with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Do NOT push. `git add` only the named files — NEVER add the untracked `.serena/`, `AGENTS.md`, or `docs/superpowers/specs/.clearance-rendered-preview-*/`.

**Verified facts (read from live code 2026-06-12 — do not re-derive):**
- `OPENAI_API_URL` instance attr is set from `--openai-api-url` (`server.py:325`); Responses handler forwards to it (`openai.py:2976` else-branch) and compresses (`is_chatgpt_auth` False for no-auth aperture traffic).
- `_apply_bedrock_child_env(env, bedrock_upstream, port)` and `_detect_bedrock_aperture(env, flag_override)` exist in `wrap.py` (1480/1464).
- `_ensure_proxy(port, no_proxy, *, ..., openai_api_url=None, anthropic_api_url=None, bedrock_api_url=None, copilot_api_token=None)` reuses a healthy proxy by port and restarts on an `openai_api_url` mismatch (`wrap.py:1831-1837`); it does NOT check bedrock (the gap — Task 2).
- `/health` config payload (`server.py:1681-1694`) exposes `openai_api_url` but not `bedrock_api_url`; the ProxyConfig field is `bedrock_base_url`.
- Codex config handling is raw-text + markers (`_inject_codex_provider_config` at `wrap.py:887`, `_strip_codex_headroom_blocks(content, *, remove_mcp=False)` at `wrap.py:770`, `_codex_config_paths()` hardcodes `~/.codex`). No `CODEX_HOME` is set anywhere today. No `tomllib`/`tomli` imported in `wrap.py`.
- `workspace_dir()` (`headroom/paths.py:115`) = `~/.headroom` (override `$HEADROOM_WORKSPACE_DIR`).
- The `codex` command launches via `_launch_tool(...)` which internally calls `_ensure_proxy` + `subprocess.run`; the `claude` command calls `_ensure_proxy` directly then `subprocess.run([claude_bin, *claude_args], env=env)`.

---

## File Structure

**Create:**
- `headroom/cli/profiles.py` — `ResolvedProfile`, `load_profiles`, `resolve_profile`, `_profile_port`, `_strip_v1`, `select_profile_name`. Pure, no side effects beyond reading files.
- `headroom/cli/codex_owned_config.py` — `build_owned_codex_config(seed_text, port)` (pure string→string) + `write_codex_owned_config(seed_dir, owned_dir, port)` (filesystem orchestration). Kept separate from `profiles.py` (different responsibility: TOML rewriting vs profile resolution).
- `tests/test_profiles_resolver.py`, `tests/test_codex_owned_config.py`, `tests/test_wrap_profile_wiring.py`, `tests/test_profile_smoke.py`.

**Modify:**
- `headroom/proxy/server.py` — add `bedrock_api_url` to the `/health` config payload.
- `headroom/cli/wrap.py` — add `bedrock_api_url` mismatch check in `_ensure_proxy`; add `--profile` to `claude` and `codex` commands; thread resolved profile through.
- `docs/getting-started-subscriptions-and-bedrock.md` — add a `--profile` section.

---

## Task 1: Profile resolver — pure core

**Files:**
- Create: `headroom/cli/profiles.py`
- Test: `tests/test_profiles_resolver.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_profiles_resolver.py
from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest

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


# --- helpers ---
def _write_profiles(tmp_path: Path, body: str) -> None:
    (tmp_path / "profiles.toml").write_text(body)


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_profiles_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'headroom.cli.profiles'`.

- [ ] **Step 3: Implement `headroom/cli/profiles.py`**

```python
"""Profile resolution for `headroom wrap --profile <name>`.

A profile is a named backend defined in ``~/.headroom/profiles.toml`` and
seeded from the user's existing agent config files. The resolver reads the
profile + its seed files and returns a ``ResolvedProfile`` the wrap commands
use to start a profile-complete proxy and configure the child agent. Pure
except for reading those files; never writes anything.
"""

from __future__ import annotations

import json
import tomllib
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from headroom.paths import workspace_dir

DEFAULT_PROFILE = "personal"


def profiles_path() -> Path:
    """Location of the user's profiles file (``~/.headroom/profiles.toml``)."""
    return workspace_dir() / "profiles.toml"


@dataclass(frozen=True)
class ResolvedProfile:
    name: str
    port: int
    bedrock_base_url: str | None = None
    claude_env: dict[str, str] = field(default_factory=dict)
    openai_upstream: str | None = None
    codex_seed_dir: str | None = None


def _strip_v1(url: str | None) -> str | None:
    if not isinstance(url, str):
        return None
    normalized = url.strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3]
    return normalized or None


def _profile_port(name: str, *, default_profile: str) -> int:
    if name == default_profile:
        return 8787
    return 8788 + (zlib.crc32(name.encode()) % 1000)


def _expand(p: str) -> Path:
    return Path(p).expanduser()


def load_profiles(profiles_path_: Path) -> dict:
    if not profiles_path_.exists():
        raise FileNotFoundError(f"No headroom profiles file at {profiles_path_}")
    with open(profiles_path_, "rb") as f:
        return tomllib.load(f)


def select_profile_name(*, flag: str | None, env: dict[str, str], config_default: str | None) -> str:
    return flag or env.get("HEADROOM_PROFILE") or config_default or DEFAULT_PROFILE


def _read_claude_seed(path: Path) -> tuple[str | None, dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"claude_seed not found: {path}")
    data = json.loads(path.read_text())
    env = {str(k): str(v) for k, v in (data.get("env") or {}).items()}
    return env.get("ANTHROPIC_BEDROCK_BASE_URL"), env


def _read_codex_seed(dir_path: Path) -> str | None:
    """Return the active provider's upstream (``base_url`` minus /v1), or None
    when the seed uses the default OpenAI provider or already points at a local
    proxy (a prior in-place wrap)."""
    cfg = dir_path / "config.toml"
    if not dir_path.exists() or not cfg.exists():
        raise FileNotFoundError(f"codex_seed config not found: {cfg}")
    with open(cfg, "rb") as f:
        data = tomllib.load(f)
    provider = data.get("model_provider")
    providers = data.get("model_providers") or {}
    base_url = None
    if isinstance(provider, str) and provider in providers:
        base_url = providers[provider].get("base_url")
    if base_url is None:
        base_url = data.get("openai_base_url")
    upstream = _strip_v1(base_url)
    if upstream and ("127.0.0.1" in upstream or "localhost" in upstream):
        return None  # seed already wrapped to a local proxy -> treat as default
    return upstream


def resolve_profile(name: str, *, profiles_path: Path | None = None) -> ResolvedProfile:
    path = profiles_path or globals()["profiles_path"]() if profiles_path is None else profiles_path
    doc = load_profiles(path)
    table = doc.get("profiles") or {}
    default_profile = table.get("default") or DEFAULT_PROFILE
    profile = table.get(name)
    if not isinstance(profile, dict):
        available = sorted(k for k in table if k != "default")
        raise KeyError(f"Unknown profile '{name}'. Available: {available}")

    port = int(profile["port"]) if "port" in profile else _profile_port(name, default_profile=default_profile)

    bedrock_base_url: str | None = None
    claude_env: dict[str, str] = {}
    if profile.get("claude_seed"):
        bedrock_base_url, claude_env = _read_claude_seed(_expand(profile["claude_seed"]))

    openai_upstream: str | None = None
    codex_seed_dir: str | None = None
    if profile.get("codex_seed"):
        seed_dir = _expand(profile["codex_seed"])
        openai_upstream = _read_codex_seed(seed_dir)
        codex_seed_dir = str(seed_dir)

    return ResolvedProfile(
        name=name,
        port=port,
        bedrock_base_url=bedrock_base_url,
        claude_env=claude_env,
        openai_upstream=openai_upstream,
        codex_seed_dir=codex_seed_dir,
    )
```

Note: the `path = ...` line is awkward; replace it with the clean form:

```python
    path = profiles_path if profiles_path is not None else globals()["profiles_path"]()
```
Wait — `profiles_path` is both a parameter name and a module function, which shadows. RENAME the module function to `default_profiles_path()` to avoid the collision, and update `profiles_path()` references. Final resolver signature: `resolve_profile(name, *, profiles_path: Path | None = None)`, body: `path = profiles_path if profiles_path is not None else default_profiles_path()`.

- [ ] **Step 4: Apply the rename fix and run tests**

Rename the module-level `profiles_path()` function to `default_profiles_path()`; in `resolve_profile`, use `path = profiles_path if profiles_path is not None else default_profiles_path()`.
Run: `.venv/bin/python -m pytest tests/test_profiles_resolver.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add headroom/cli/profiles.py tests/test_profiles_resolver.py
git commit -m "feat(cli): profile resolver for --profile wraps

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Close the Bedrock-upstream health gap

**Files:**
- Modify: `headroom/proxy/server.py:1690` (health config payload)
- Modify: `headroom/cli/wrap.py:1837` (`_ensure_proxy` mismatch check)
- Test: `tests/test_wrap_profile_wiring.py` (health payload assertion)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wrap_profile_wiring.py
from __future__ import annotations

from headroom.proxy.server import HeadroomProxy, ProxyConfig


def test_health_payload_exposes_bedrock_api_url():
    proxy = HeadroomProxy(
        ProxyConfig(bedrock_base_url="https://ap/bedrock", cache_enabled=False,
                    rate_limit_enabled=False)
    )
    # _health_payload is a closure in create_app; assert via the public builder.
    # Build the app and read /health through the TestClient WITHOUT entering the
    # lifespan (which needs the Rust core): call the config builder directly.
    from headroom.proxy import server as srv
    payload = srv._build_health_config(proxy.config)   # helper added in Step 3
    assert payload["bedrock_api_url"] == "https://ap/bedrock"
    assert payload["openai_api_url"] == proxy.config.openai_api_url
```

Rationale for a `_build_health_config` helper: the existing payload is built inside a closure in `create_app` and is hard to unit-test without booting the app (which `sys.exit(78)`s when the Rust core is absent). Extracting the config dict into a tiny module-level pure function makes it testable and is a clean refactor.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_wrap_profile_wiring.py::test_health_payload_exposes_bedrock_api_url -v`
Expected: FAIL — `AttributeError: module 'headroom.proxy.server' has no attribute '_build_health_config'`.

- [ ] **Step 3: Extract + extend the health config payload**

In `headroom/proxy/server.py`, add a module-level helper (near the other helpers, before `create_app`):

```python
def _build_health_config(config: "ProxyConfig") -> dict[str, Any]:
    """Config block surfaced on /health (used by wrap's running-proxy checks)."""
    return {
        "backend": config.backend,
        "optimize": config.optimize,
        "cache": config.cache_enabled,
        "rate_limit": config.rate_limit_enabled,
        "memory": config.memory_enabled,
        "learn": config.traffic_learning_enabled,
        "code_graph": config.code_graph_watcher,
        "anthropic_api_url": config.anthropic_api_url,
        "openai_api_url": config.openai_api_url,
        "bedrock_api_url": config.bedrock_base_url,
        "gemini_api_url": config.gemini_api_url,
        "cloudcode_api_url": config.cloudcode_api_url,
        "pid": os.getpid(),
    }
```

Then in `_health_payload` (line ~1681), replace the inline `payload["config"] = {...}` block with:

```python
        if include_config:
            payload["config"] = _build_health_config(config)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_wrap_profile_wiring.py::test_health_payload_exposes_bedrock_api_url -v`
Expected: PASS.

- [ ] **Step 5: Add the `_ensure_proxy` bedrock mismatch check**

In `headroom/cli/wrap.py`, immediately after the existing `openai_api_url` mismatch block (ends at line 1837 with `missing.append("openai-api-url")`), add:

```python
                if bedrock_api_url:
                    running_bedrock_url = _normalize_proxy_api_url(
                        running_config.get("bedrock_api_url")
                    )
                    requested_bedrock_url = _normalize_proxy_api_url(bedrock_api_url)
                    if running_bedrock_url != requested_bedrock_url:
                        missing.append("bedrock-api-url")
```

(`_normalize_proxy_api_url` already strips trailing `/` and `/v1`, suitable for bedrock URLs.)

- [ ] **Step 6: Verify nothing regressed**

Run: `.venv/bin/python -m pytest tests/test_wrap_profile_wiring.py tests/test_bedrock_config.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add headroom/proxy/server.py headroom/cli/wrap.py tests/test_wrap_profile_wiring.py
git commit -m "fix(proxy): expose bedrock_api_url on /health + check it in _ensure_proxy

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Codex owned-config writer

**Files:**
- Create: `headroom/cli/codex_owned_config.py`
- Test: `tests/test_codex_owned_config.py`

Builds the headroom-owned Codex `config.toml` from a seed, rewriting the active provider's `base_url` to the local proxy and stripping any prior in-place headroom blocks. Handles both seed shapes: a custom `model_provider` (company — rewrite its `base_url`, preserve `wire_api`) and a default-OpenAI seed (personal — inject the proxy override block, matching `_inject_codex_provider_config`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_codex_owned_config.py
from __future__ import annotations

import tomllib
from pathlib import Path

from headroom.cli.codex_owned_config import build_owned_codex_config, write_codex_owned_config


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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_codex_owned_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'headroom.cli.codex_owned_config'`.

- [ ] **Step 3: Implement `headroom/cli/codex_owned_config.py`**

```python
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
```

Note the import `from headroom.cli.wrap import _strip_codex_headroom_blocks` — confirm no circular import at runtime (wrap.py does not import codex_owned_config at module load). If a cycle appears, move the import inside `build_owned_codex_config`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_codex_owned_config.py -v`
Expected: PASS (all four).

- [ ] **Step 5: Commit**

```bash
git add headroom/cli/codex_owned_config.py tests/test_codex_owned_config.py
git commit -m "feat(cli): headroom-owned Codex config writer for profiles

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire `--profile` into `wrap claude`

**Files:**
- Modify: `headroom/cli/wrap.py` — `claude` command (option + signature + body ~2297-2487)
- Test: `tests/test_wrap_profile_wiring.py` (add)

- [ ] **Step 1: Write the failing test** (exercises the resolution+wiring via a pure helper)

To keep the heavy `claude` command testable, extract the profile→bedrock decision into a small pure helper and test that:

```python
# append to tests/test_wrap_profile_wiring.py
import json
from headroom.cli.wrap import _resolve_claude_profile


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

    rp = _resolve_claude_profile(flag="company", bedrock_base_url=None)
    assert rp.bedrock_base_url == "https://ap/bedrock"
    assert rp.openai_upstream == "https://ap"
    assert rp.port == _expected_company_port()


def _expected_company_port():
    import zlib
    return 8788 + (zlib.crc32(b"company") % 1000)
```

(`HEADROOM_WORKSPACE_DIR` redirects `workspace_dir()` so `profiles.toml` is read from tmp_path — verified that `paths.workspace_dir()` honors that env var.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_wrap_profile_wiring.py::test_resolve_claude_profile_company -v`
Expected: FAIL — `ImportError: cannot import name '_resolve_claude_profile'`.

- [ ] **Step 3: Add the resolver helper + wire the command**

In `headroom/cli/wrap.py`, add near the other helpers:

```python
def _resolve_claude_profile(*, flag: str | None, bedrock_base_url: str | None):
    """Resolve the profile for `wrap claude`. A --bedrock-base-url flag still
    forces Bedrock mode without a profile (manual override)."""
    from headroom.cli.profiles import (
        ResolvedProfile, default_profiles_path, load_profiles,
        resolve_profile, select_profile_name,
    )
    path = default_profiles_path()
    if not path.exists():
        # No profiles configured: preserve legacy behavior via the flag/ambient
        # detect, on the default port.
        bedrock = _detect_bedrock_aperture(dict(os.environ), bedrock_base_url)
        return ResolvedProfile(name="(none)", port=8787, bedrock_base_url=bedrock)
    doc = load_profiles(path)
    default_profile = (doc.get("profiles") or {}).get("default")
    name = select_profile_name(flag=flag, env=dict(os.environ), config_default=default_profile)
    rp = resolve_profile(name, profiles_path=path)
    if bedrock_base_url:  # explicit flag overrides the profile's bedrock url
        rp = ResolvedProfile(
            name=rp.name, port=rp.port, bedrock_base_url=bedrock_base_url,
            claude_env=rp.claude_env, openai_upstream=rp.openai_upstream,
            codex_seed_dir=rp.codex_seed_dir,
        )
    return rp
```

Add the `--profile` Click option to the `claude` command (after `--bedrock-base-url`, before `--verbose`):

```python
@click.option(
    "--profile",
    "profile",
    default=None,
    help="Named backend profile from ~/.headroom/profiles.toml (e.g. personal, company). "
         "Default resolves from the profiles file; HEADROOM_PROFILE env overrides.",
)
```

Add `profile: str | None,` to the `def claude(` signature (next to `bedrock_base_url`). Also change `--port` default to `None` so the profile port can apply: `@click.option("--port", "-p", default=None, type=int, ...)` and `port: int | None,` in the signature.

Replace the bedrock-detection + `_ensure_proxy` region (lines 2417-2437) with profile-driven resolution:

```python
        resolved = _resolve_claude_profile(flag=profile, bedrock_base_url=bedrock_base_url)
        effective_port = port if port is not None else resolved.port
        bedrock_upstream = resolved.bedrock_base_url

        foundry_upstream = None
        if os.environ.get("CLAUDE_CODE_USE_FOUNDRY"):
            foundry_upstream = os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL")
        if foundry_upstream and bedrock_upstream:
            click.echo(
                "  Warning: both Foundry and Bedrock modes detected; Claude Code uses "
                "Foundry first, so Bedrock-aperture compression will be inert."
            )

        proxy_holder[0] = _ensure_proxy(
            effective_port,
            no_proxy,
            learn=learn,
            memory=memory,
            agent_type="claude",
            code_graph=code_graph,
            anthropic_api_url=foundry_upstream,
            bedrock_api_url=bedrock_upstream,
            openai_api_url=resolved.openai_upstream,   # profile-complete proxy
        )
```

Then update every later use of `port` in the `claude` body to `effective_port` (the `_claude_proxy_base_url(port)` call, the `_apply_bedrock_child_env(env, bedrock_upstream, port)` call, MCP/rtk setup that takes `port`). And overlay the seed env onto the child before applying bedrock:

```python
        env = os.environ.copy()
        env.update(resolved.claude_env)              # model overrides etc. from claude_seed
        proxy_url = _claude_proxy_base_url(effective_port)
        if foundry_upstream:
            env["ANTHROPIC_FOUNDRY_BASE_URL"] = proxy_url
        else:
            env["ANTHROPIC_BASE_URL"] = proxy_url
        local_bedrock = _apply_bedrock_child_env(env, bedrock_upstream, effective_port)
        if local_bedrock:
            click.echo(f"  Bedrock aperture [{resolved.name}]: {bedrock_upstream} (via {local_bedrock})")
```

(Find the existing `proxy_url = _claude_proxy_base_url(port)` assignment earlier in the body and replace its `port` with `effective_port`; ensure only ONE `proxy_url` assignment remains.)

- [ ] **Step 4: Run the test + import sanity**

Run: `.venv/bin/python -m pytest tests/test_wrap_profile_wiring.py -v`
Expected: PASS.
Run: `.venv/bin/python -c "from click.testing import CliRunner; from headroom.cli.wrap import claude; r=CliRunner().invoke(claude,['--help']); print(r.exit_code, '--profile' in r.output)"`
Expected: `0 True`.

- [ ] **Step 5: Commit**

```bash
git add headroom/cli/wrap.py tests/test_wrap_profile_wiring.py
git commit -m "feat(cli): wrap claude --profile (seed-driven, profile-complete proxy)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Wire `--profile` into `wrap codex`

**Files:**
- Modify: `headroom/cli/wrap.py` — `codex` command (option + signature + body ~2865-3030)
- Test: `tests/test_wrap_profile_wiring.py` (add)

- [ ] **Step 1: Write the failing test** (pure helper for the codex profile decision)

```python
# append to tests/test_wrap_profile_wiring.py
from pathlib import Path
from headroom.cli.wrap import _resolve_codex_profile


def test_resolve_codex_profile_company(tmp_path, monkeypatch):
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_wrap_profile_wiring.py::test_resolve_codex_profile_company -v`
Expected: FAIL — `ImportError: cannot import name '_resolve_codex_profile'`.

- [ ] **Step 3: Add the helper + wire the command**

In `headroom/cli/wrap.py`, add:

```python
def _resolve_codex_profile(*, flag: str | None):
    """Resolve the profile for `wrap codex` and materialize the owned CODEX_HOME.
    Returns (ResolvedProfile, owned_codex_home: Path)."""
    from headroom.cli.codex_owned_config import write_codex_owned_config
    from headroom.cli.profiles import (
        default_profiles_path, load_profiles, resolve_profile, select_profile_name,
    )
    from headroom.paths import workspace_dir

    path = default_profiles_path()
    if not path.exists():
        return None, None  # legacy in-place behavior (caller falls back)
    doc = load_profiles(path)
    default_profile = (doc.get("profiles") or {}).get("default")
    name = select_profile_name(flag=flag, env=dict(os.environ), config_default=default_profile)
    rp = resolve_profile(name, profiles_path=path)
    owned_home = workspace_dir() / "codex" / name
    if rp.codex_seed_dir:
        write_codex_owned_config(Path(rp.codex_seed_dir), owned_home, rp.port)
    return rp, owned_home
```

Add the `--profile` Click option to the `codex` command (after `--region`, before `--memory`) — same decorator as in Task 4 — and `profile: str | None,` to the `def codex(` signature. Change `--port` default to `None`, `port: int | None,` in the signature.

In the `codex` body, before the launch, resolve the profile and branch:

```python
    resolved, owned_home = _resolve_codex_profile(flag=profile)
    if resolved is not None and owned_home is not None:
        effective_port = port if port is not None else resolved.port
        env, env_vars_display = _build_codex_launch_env(effective_port, os.environ)
        env["CODEX_HOME"] = str(owned_home)          # headroom-owned config
        _launch_tool(
            binary=codex_bin,
            args=codex_args,
            env=env,
            port=effective_port,
            no_proxy=no_proxy,
            tool_label=f"CODEX [{resolved.name}]",
            env_vars_display=env_vars_display,
            learn=learn,
            memory=memory,
            agent_type="codex",
            code_graph=code_graph,
            backend=backend,
            anyllm_provider=anyllm_provider,
            region=region,
            openai_api_url=resolved.openai_upstream,
            bedrock_api_url=resolved.bedrock_base_url,
        )
        return
    # else: fall through to the existing legacy in-place wrap path below.
```

`_launch_tool` already accepts `openai_api_url` (keyword) and forwards it into its internal `_ensure_proxy(...)` call, but it has **no** `bedrock_api_url`. Add it: in the `_launch_tool` signature add `bedrock_api_url: str | None = None,` immediately after `openai_api_url: str | None = None,`; and in its `_ensure_proxy(...)` call add `bedrock_api_url=bedrock_api_url,` immediately after the existing `openai_api_url=openai_api_url,` line. Place the profile branch BEFORE the existing `_inject_codex_provider_config(port)` + `_launch_tool(...)` block so the legacy path only runs when no profiles file exists.

Important: when the profile branch runs, do NOT call `_inject_codex_provider_config` (that writes `~/.codex`). The owned-config path replaces it entirely.

- [ ] **Step 4: Run the test + import sanity**

Run: `.venv/bin/python -m pytest tests/test_wrap_profile_wiring.py -v`
Expected: PASS (all).
Run: `.venv/bin/python -c "from click.testing import CliRunner; from headroom.cli.wrap import codex; r=CliRunner().invoke(codex,['--help']); print(r.exit_code, '--profile' in r.output)"`
Expected: `0 True`.

- [ ] **Step 5: Commit**

```bash
git add headroom/cli/wrap.py tests/test_wrap_profile_wiring.py
git commit -m "feat(cli): wrap codex --profile via headroom-owned CODEX_HOME

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Bootstrap a starter `profiles.toml`

**Files:**
- Modify: `headroom/cli/profiles.py` — add `ensure_starter_profiles()`
- Modify: `headroom/cli/wrap.py` — call it when no profiles file exists and `--profile` is given
- Test: `tests/test_profiles_resolver.py` (add)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_profiles_resolver.py
def test_ensure_starter_profiles_writes_template(tmp_path, monkeypatch):
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path))
    from headroom.cli.profiles import ensure_starter_profiles, default_profiles_path
    path = ensure_starter_profiles()
    assert path == default_profiles_path()
    assert path.exists()
    body = path.read_text()
    assert "[profiles]" in body and 'default = "personal"' in body
    assert "[profiles.personal]" in body and "[profiles.company]" in body
    # idempotent: second call does not overwrite
    path.write_text(body + "\n# edited\n")
    ensure_starter_profiles()
    assert "# edited" in path.read_text()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_profiles_resolver.py::test_ensure_starter_profiles_writes_template -v`
Expected: FAIL — `ImportError: cannot import name 'ensure_starter_profiles'`.

- [ ] **Step 3: Implement `ensure_starter_profiles`**

In `headroom/cli/profiles.py`:

```python
_STARTER_PROFILES = '''\
# Headroom wrap profiles. Select with: headroom wrap <agent> --profile <name>
# (or set HEADROOM_PROFILE, or change `default` below).
[profiles]
default = "personal"

[profiles.personal]
codex_seed = "~/.codex"                              # ChatGPT / OpenAI Pro
# no claude_seed -> Claude Max (default Anthropic, no Bedrock)

[profiles.company]
claude_seed = "~/.claude/settings.corelight.json"    # Bedrock aperture
codex_seed  = "~/.codex-corelight"                    # aperture /v1 Responses
'''


def ensure_starter_profiles() -> Path:
    """Create a starter profiles.toml if none exists. Idempotent."""
    path = default_profiles_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_STARTER_PROFILES)
    return path
```

Then in `wrap.py` `_resolve_claude_profile` / `_resolve_codex_profile`, change the `if not path.exists():` branch to first call `ensure_starter_profiles()` and re-resolve, so a first-time `--profile company` user gets the template rather than legacy fallback. (Only when `--profile` is explicitly passed; bare wrap with no profiles file keeps legacy behavior.) Show the exact edit: at the top of each resolver helper, `if flag is not None and not path.exists(): from headroom.cli.profiles import ensure_starter_profiles; path = ensure_starter_profiles()`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_profiles_resolver.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add headroom/cli/profiles.py headroom/cli/wrap.py tests/test_profiles_resolver.py
git commit -m "feat(cli): bootstrap starter profiles.toml on first --profile use

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Live two-agent smoke (opt-in) + docs

**Files:**
- Create: `tests/test_profile_smoke.py`
- Modify: `docs/getting-started-subscriptions-and-bedrock.md`

- [ ] **Step 1: Write the opt-in smoke test**

```python
# tests/test_profile_smoke.py
from __future__ import annotations

import os
import subprocess
import sys

import pytest

RUN = os.environ.get("HEADROOM_PROFILE_SMOKE") == "1"
pytestmark = pytest.mark.skipif(not RUN, reason="set HEADROOM_PROFILE_SMOKE=1 to run")


def _help(cmd: str) -> str:
    out = subprocess.run([sys.executable, "-m", "headroom.cli", "wrap", cmd, "--help"],
                         capture_output=True, text=True, timeout=60)
    return out.stdout


def test_profile_option_present_on_both_agents():
    assert "--profile" in _help("claude")
    assert "--profile" in _help("codex")
```

This stays light (CLI-surface check, skipped by default). The full live two-agent run (claude Bedrock invoke + codex `/v1/responses` through one company proxy, asserting `/stats` deltas) is a manual procedure documented in Step 2 — it requires the tailnet, real logins, and the built Rust core, so it is run by the operator, not CI.

- [ ] **Step 2: Document `--profile` in the getting-started guide**

Add a "Profiles (personal / company)" section to `docs/getting-started-subscriptions-and-bedrock.md` covering: the `~/.headroom/profiles.toml` schema (the starter template), `headroom wrap claude --profile company` / `headroom wrap codex --profile company`, the `personal` default + `HEADROOM_PROFILE` env swap, that seeds are read-only and Codex uses a headroom-owned `CODEX_HOME` (`~/.headroom/codex/<profile>/`), and the per-profile port behavior (personal=8787, company auto). Include the manual two-agent verification recipe: run a company claude + a company codex, then `curl -s http://127.0.0.1:<port>/stats` to see compression on both.

- [ ] **Step 3: Run + commit**

Run: `.venv/bin/python -m pytest tests/test_profile_smoke.py -v` → Expected: 1 skipped.
```bash
git add tests/test_profile_smoke.py docs/getting-started-subscriptions-and-bedrock.md
git commit -m "docs+test: profile wraps getting-started + opt-in smoke

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Full sweep + lint + final review

- [ ] **Step 1: Run the full new + adjacent suite**

```bash
.venv/bin/python -m pytest \
  tests/test_profiles_resolver.py \
  tests/test_codex_owned_config.py \
  tests/test_wrap_profile_wiring.py \
  tests/test_profile_smoke.py \
  tests/test_bedrock_config.py \
  tests/test_wrap_bedrock_autodetect.py \
  tests/test_bedrock_routes.py -v
```
Expected: all PASS (smoke skipped). Pre-existing `_core`-lifespan failures in full-app TestClient suites are environmental — confirm any failure is one of those, not new.

- [ ] **Step 2: Lint the changed files**

```bash
uvx ruff check headroom/cli/profiles.py headroom/cli/codex_owned_config.py headroom/cli/wrap.py headroom/proxy/server.py tests/test_profiles_resolver.py tests/test_codex_owned_config.py tests/test_wrap_profile_wiring.py tests/test_profile_smoke.py
```
Expected: `All checks passed!` — fix any findings, re-run.

- [ ] **Step 3: Final commit (if lint fixes were needed)**

```bash
git add -A -- headroom/ tests/
git commit -m "style: lint fixes for profile wraps

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

Do NOT push.

---

## Self-Review Notes (author checklist, applied)

- **Spec coverage:** Component 1 (profiles.toml) → Tasks 1, 6; Component 2 (resolver) → Task 1; Component 3 (proxy lifecycle / per-profile port / health gap) → Tasks 2, 4, 5; Component 4 (codex owned config) → Tasks 3, 5; Component 5 (claude seed-driven) → Task 4; Component 6 (fold ambient auto-detect) → Task 4 (`_resolve_claude_profile` replaces the ambient call; `--bedrock-base-url` retained as override; legacy ambient kept only as no-profiles-file fallback); testing → Tasks 1-5, 7; personal/company table → Tasks 4/5 behavior; non-goals honored (seeds read-only; no new wire handler; bedrock handler/routes untouched).
- **Type consistency:** `ResolvedProfile(name, port, bedrock_base_url, claude_env, openai_upstream, codex_seed_dir)` identical across resolver, claude wiring, codex wiring. `resolve_profile(name, *, profiles_path=None)`, `select_profile_name(*, flag, env, config_default)`, `build_owned_codex_config(seed_text, port)`, `write_codex_owned_config(seed_dir, owned_dir, port)`, `_resolve_claude_profile(*, flag, bedrock_base_url)`, `_resolve_codex_profile(*, flag)` consistent between definition and call sites. Module fn renamed `profiles_path()`→`default_profiles_path()` to avoid the param shadow (Task 1 Step 4).
- **Executor verification points:** (a) confirm `paths.workspace_dir()` honors `HEADROOM_WORKSPACE_DIR` (used by tests to redirect profiles.toml); (b) `_launch_tool` already forwards `openai_api_url`; Task 5 adds the `bedrock_api_url` param + passthrough (exact edit specified); (c) ensure only one `proxy_url`/`port` assignment path remains in the `claude` body after switching to `effective_port`; (d) watch for a circular import between `codex_owned_config` and `wrap` (move the `_strip_codex_headroom_blocks` import function-local if needed).
