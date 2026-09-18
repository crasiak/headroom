# Design: Profile-based wraps (`--profile personal|company`)

**Date:** 2026-06-12
**Status:** Approved design, pending implementation plan
**Author:** jws (with Claude)
**Builds on:** `docs/superpowers/specs/2026-06-11-subscription-and-aperture-bedrock-design.md` (the Bedrock-aperture passthrough — already implemented on branch `headroom-hacking`).

## Problem

The user runs two coding agents (Claude Code and Codex) against **two different backends**:

- **personal** — personal subscription accounts (Claude Max, ChatGPT/OpenAI Pro).
- **company** — corelight inference served from AWS Bedrock via the company Tailscale "aperture" gateway (`https://ai.taileb6e.ts.net`).

The aperture exposes **two protocols on the same host**, and the two agents use different ones:

| Agent | Path | Protocol | Seed config today |
|---|---|---|---|
| Claude Code | `/bedrock/model/{id}/invoke[-with-response-stream]` | Bedrock invoke (Anthropic Messages JSON; AWS event-stream on the stream path) | `~/.claude/settings.corelight.json` |
| Codex (gpt-5.5) | `/v1/responses` | OpenAI **Responses** API (`wire_api = "responses"`, `requires_openai_auth = false`) | `~/.codex-corelight/config.toml` (launched via `~/.local/bin/codex-cl`, which sets `CODEX_HOME`) |

Headroom already speaks **both** protocols: the Bedrock passthrough handler (new, 2026-06-11 work) and the OpenAI Responses handler (`handle_openai_responses`, which forwards to `self.OPENAI_API_URL` and compresses the Responses payload when there is no ChatGPT session token). So **no new wire handler is needed.** The gap is **orchestration**: a clean way to point each agent at the right backend, keep personal and company contexts separate, and route both through one compression proxy.

The user wants this expressed as **named profiles** selected with `--profile`, defined in headroom config, seeded from the user's existing config files, with **personal as the default** and company "easily swappable."

## Decisions (from brainstorming, 2026-06-12)

1. **CLI surface:** `headroom wrap claude --profile <name>` / `headroom wrap codex --profile <name>`. Profiles defined in headroom config.
2. **Codex config handling:** headroom owns its own `CODEX_HOME` per profile (`~/.headroom/codex/<profile>/`). The user's `~/.codex-corelight` and `~/.codex` are **never mutated**.
3. **Profile source:** profiles **reference the user's existing config files as seeds** (DRY; the user's files stay the single source of truth). Re-derived on each wrap so edits flow through.
4. **Symmetric seeds:** both agents seed from real files — `claude_seed` (a Claude settings JSON) and `codex_seed` (a Codex config dir).
5. **Default + auto-detect:** a configurable **default profile** (`default = "personal"`); bare `wrap` uses it; `--profile` overrides. The previously-built ambient `CLAUDE_CODE_USE_BEDROCK` auto-detect is **folded into the resolver** (the `company` profile reads the bedrock URL from its `claude_seed`), so there is one mechanism, not two.
6. **Ports:** auto-derived deterministically per profile (`zlib.crc32`), default profile pinned to `8787`, explicit `port` field overrides.
7. **Personal codex:** seeds from `~/.codex` into a headroom-owned copy — uniform with company.

## Design

### Component 1 — Profile config file (`~/.headroom/profiles.toml`, new)

Lives under `workspace_dir()` (`~/.headroom`). Read with `tomllib`. Schema:

```toml
[profiles]
default = "personal"          # used when no --profile / HEADROOM_PROFILE given

[profiles.personal]
codex_seed  = "~/.codex"                              # ChatGPT/OpenAI Pro; pristine personal config
# no claude_seed  -> Claude Max (default Anthropic, no Bedrock)
# no port         -> 8787 (it is the default profile)

[profiles.company]
claude_seed = "~/.claude/settings.corelight.json"     # Bedrock aperture env
codex_seed  = "~/.codex-corelight"                    # aperture /v1 Responses
# no port -> auto-derived (deterministic crc32), e.g. 88xx
```

Fields per `[profiles.<name>]`: `claude_seed` (optional path), `codex_seed` (optional path), `port` (optional int). At least one of `claude_seed`/`codex_seed` should be present for the agent(s) the profile is used with; a profile used to wrap an agent it does not configure falls back to that agent's default routing (see §5).

If `~/.headroom/profiles.toml` does not exist, headroom **bootstraps a starter file** on first `--profile` use (or via an explicit `headroom profile init`) seeded with sensible `personal`/`company` defaults derived from the files that exist on the box — non-goal to auto-discover beyond the two known seeds; the starter is a template the user edits. (Bootstrapping detail finalized in the plan.)

### Component 2 — Profile resolver (`headroom/cli/profiles.py`, new — pure, unit-testable)

```python
@dataclass(frozen=True)
class ResolvedProfile:
    name: str
    port: int
    # Claude side (None => default Anthropic / Claude Max, no Bedrock)
    bedrock_base_url: str | None  # from claude_seed env.ANTHROPIC_BEDROCK_BASE_URL
    claude_env: dict[str, str]  # full claude_seed `env` block (model overrides, etc.)
    # Codex side (None => default OpenAI / ChatGPT)
    openai_upstream: str | None  # codex_seed provider base_url minus trailing /v1
    codex_seed_dir: str | None  # source CODEX_HOME to clone
```

Resolution:
- **`port`**: explicit `profile.port` → use it; else `name == default_profile` → `8787`; else `8788 + zlib.crc32(name.encode()) % 1000` (deterministic, machine-independent; **not** Python's salted `hash()`).
- **claude side**: if `claude_seed` set, read JSON; `claude_env = data["env"]`; `bedrock_base_url = claude_env.get("ANTHROPIC_BEDROCK_BASE_URL")`. (USE_BEDROCK / SKIP_AUTH come through `claude_env`.) No `claude_seed` → `bedrock_base_url=None`, `claude_env={}`.
- **codex side**: if `codex_seed` set, read `<dir>/config.toml`; find `model_provider` name → `[model_providers.<name>].base_url`; `openai_upstream = _strip_v1(base_url)`; `codex_seed_dir = <dir>`. If the provider `base_url` already points at a local proxy (a prior in-place wrap), treat `openai_upstream` as **default OpenAI** (None) and rely on the seed's *backup* / strip step (see §4). No `codex_seed` → both `None`.
- Missing seed file named by the profile → raise a clear error naming the path. Unknown profile name → error listing available profiles.

Selection precedence (resolved before the resolver): `--profile` flag > `HEADROOM_PROFILE` env > `[profiles].default` > built-in `"personal"`.

### Component 3 — Proxy lifecycle: one profile-complete proxy per profile-port

A profile may route **both** agents, and `wrap claude` / `wrap codex` are separate invocations that each call `_ensure_proxy`. To make the second invocation attach to a correctly-configured proxy, **both** wraps start the proxy with the profile's **full** upstream set:

- `--bedrock-base-url {bedrock_base_url}` when the profile has one,
- `--openai-api-url {openai_upstream}` when the profile has one,
- on `--port {resolved.port}`.

`_ensure_proxy` already reuses a healthy proxy on the port. Because each profile has a **distinct port**, a `company` proxy (8788-ish, both aperture upstreams) and a `personal` proxy (8787, default upstreams) coexist without contaminating each other, and the second agent of a profile attaches to the already-running profile proxy. **Per-profile ports are the primary isolation** and resolve the common cross-profile contamination case by construction.

**Known gap to close in the plan (verified against code 2026-06-12):** `_ensure_proxy`'s reuse path checks the running proxy's `openai_api_url` (exposed in `/health`, `server.py:~1681`) and restarts on mismatch, but the `/health` config block does **not** expose `bedrock_api_url`, and there is no parallel bedrock-mismatch check. So the narrow residual case — the user edits a profile's `claude_seed`/aperture and re-wraps *the same profile* while its old proxy is still alive on that port — would silently reuse the stale Bedrock upstream. The plan MUST close this by: (1) adding `bedrock_api_url` to the `/health` config payload, and (2) extending the `_ensure_proxy` mismatch check to restart (idle) / warn (active) when the running proxy's bedrock upstream differs from the profile's — mirroring the existing `openai_api_url` mismatch machinery exactly.

### Component 4 — `wrap codex --profile X` (headroom-owned CODEX_HOME)

1. Resolve the profile.
2. Ensure the profile proxy (§3).
3. **Write the owned config**: `owned = ~/.headroom/codex/<profile>/`; copy `<codex_seed_dir>/config.toml` → `owned/config.toml`, then:
   - run the existing `_strip_codex_headroom_blocks` on the copy first (so a seed that is *itself* already wrapped in-place — e.g. today's `~/.codex` — is neutralized), then
   - rewrite the active provider's `base_url` (and top-level `openai_base_url` if present) to `http://127.0.0.1:{port}/v1`.
   - The owned config is regenerated every run (cheap; keeps it in sync with seed edits). Only `config.toml` is templated; other `CODEX_HOME` state (auth.json, sessions) — **decision in plan**: symlink the non-config items from the seed dir, or set them fresh. Default proposal: symlink `auth.json` from the seed dir so login carries over; keep sessions separate per profile.
4. Launch `codex` with `CODEX_HOME=owned` and the user's extra args.
5. The proxy's `is_chatgpt_auth` logic does the rest: ChatGPT-token traffic (personal) → `chatgpt.com`; no-auth aperture traffic (company) → `--openai-api-url`. Same mechanism, both profiles.

The user's `~/.codex-corelight` and `~/.codex` are never written. (The old in-place `wrap codex` path stays for backward compat but is superseded by profiles; recommend `headroom unwrap codex` to restore `~/.codex` since the personal profile now owns a separate copy.)

### Component 5 — `wrap claude --profile X`

1. Resolve the profile.
2. Ensure the profile proxy (§3).
3. Child env: start from `os.environ`, overlay `resolved.claude_env` (the seed's full `env` block — model overrides, max tokens, etc.), then:
   - if `bedrock_base_url` set → Bedrock mode: reuse the existing `_apply_bedrock_child_env` (set child `ANTHROPIC_BEDROCK_BASE_URL=http://127.0.0.1:{port}`, preserve `CLAUDE_CODE_USE_BEDROCK`/`SKIP_BEDROCK_AUTH` from the seed env);
   - else → default mode: set `ANTHROPIC_BASE_URL=http://127.0.0.1:{port}` (Claude Max OAuth passthrough, today's behavior).
4. Launch `claude`.

This **reuses the Bedrock wrap machinery** built on 2026-06-11; the only change is the *input source* — bedrock URL + flags come from the resolved profile (`claude_seed`) instead of ambient `os.environ`.

### Component 6 — Refactor: fold ambient auto-detect into the resolver

`_detect_bedrock_aperture(env, flag_override)` (built 2026-06-11) is **superseded**: the `company` profile reads the same values from `claude_seed`. Remove the ambient `os.environ` auto-detect from the `claude` command and route the bedrock inputs through `ResolvedProfile`. The `--bedrock-base-url` flag on `wrap claude` is retained as a manual override (forces Bedrock mode without a profile), but the default path is profile-driven. (Keeping the proxy-side `--bedrock-base-url` / `BEDROCK_TARGET_API_URL` and the handler/routes/config from the prior work unchanged.)

## Personal vs company — concrete behavior

| | `--profile personal` (default) | `--profile company` |
|---|---|---|
| Proxy port | 8787 | auto (e.g. 88xx) |
| `wrap claude` | `ANTHROPIC_BASE_URL=…:8787` → Anthropic direct (Claude Max OAuth) | Bedrock mode → aperture `/bedrock` (compressed) |
| `wrap codex` | owned `CODEX_HOME` seeded from `~/.codex` → ChatGPT via `chatgpt.com` | owned `CODEX_HOME` seeded from `~/.codex-corelight` → aperture `/v1/responses` (compressed) |
| Seeds | `codex_seed=~/.codex` | `claude_seed=~/.claude/settings.corelight.json`, `codex_seed=~/.codex-corelight` |
| User files mutated | none | none |

## Testing

- **Resolver (unit):** port derivation (default→8787, deterministic crc32, explicit override, cross-run stability); claude_seed parsing (env block, bedrock url, missing file error); codex_seed parsing (provider base_url, `_strip_v1`, already-wrapped seed → default upstream); unknown profile / missing seed errors; selection precedence (flag > env > config default).
- **Codex owned-config writer (unit):** copy + `_strip_codex_headroom_blocks` + base_url rewrite to the proxy; seed dir untouched (assert source bytes unchanged); regeneration idempotent.
- **Wrap wiring (unit, mocked subprocess):** `wrap claude --profile company` starts proxy with `--bedrock-base-url` + `--openai-api-url` on the profile port and applies the seed env + bedrock child-env; `wrap codex --profile company` sets `CODEX_HOME` to the owned dir and writes the rewritten config; `--profile personal` and bare-wrap-uses-default; `HEADROOM_PROFILE` override.
- **Live smoke (opt-in, both agents):** with the company profile, a Bedrock invoke and an OpenAI `/v1/responses` call both round-trip through one proxy and show compression on `/stats`.

## Non-goals

- No new wire handler — Bedrock (done) + OpenAI Responses (existing) cover both protocols.
- No mutation of `~/.codex`, `~/.codex-corelight`, or `~/.claude/settings*.json` — seeds are read-only.
- No auto-discovery of profiles beyond writing a starter template; the user curates `profiles.toml`.
- No change to the Bedrock handler/routes/proxy-config from the 2026-06-11 work.
- No multi-tenant/3rd-profile features beyond what the extensible `--profile <name>` mechanism already allows.
