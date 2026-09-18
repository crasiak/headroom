# Design: Subscription auth & company-Bedrock (Tailscale aperture) support

**Date:** 2026-06-11
**Status:** Approved design, pending implementation
**Author:** jws (with Claude)

## Problem

Headroom is assumed to "only support API-key mode" for Codex and Claude. The user
wants to drive headroom with:

1. A **Claude Max** subscription (OAuth login, not an API key).
2. An **OpenAI Pro / ChatGPT** subscription (Codex OAuth, not an API key).
3. The user's **company Bedrock** models, reached over Tailscale through a gateway
   ("aperture") at `https://ai.taileb6e.ts.net/bedrock`.

## Findings (verified against the code and the live aperture)

### Subscriptions (parts 1 & 2) already work — premise is out of date

- `headroom/proxy/auth_mode.py` already classifies `sk-ant-oat-*` (Claude Max OAuth),
  3-segment JWT bearer tokens (Codex/ChatGPT OAuth), and CLI user-agents
  (`claude-cli/`, `codex-cli/`) into `OAUTH` / `SUBSCRIPTION` modes, with a
  lossless-only passthrough compression policy.
- `headroom wrap codex` already overrides Codex's `openai_base_url` so ChatGPT-plan
  traffic cannot bypass the proxy (`wrap.py:_inject_codex_provider_config`).
- `headroom wrap claude` sets `ANTHROPIC_BASE_URL` to the local proxy; Claude Code
  forwards its OAuth token, which the proxy passes through.

**Conclusion:** parts 1 & 2 need *empirical verification + documentation*, not new
code. If verification surfaces a real break, fix it then (out of scope until proven).

### Company Bedrock (part 3) is the real gap

`~/.claude/settings.corelight.json` shows Claude Code in **Bedrock wire-protocol**
mode:

```
CLAUDE_CODE_USE_BEDROCK=1
CLAUDE_CODE_SKIP_BEDROCK_AUTH=1            # gateway authorizes by tailnet identity; no client SigV4
ANTHROPIC_BEDROCK_BASE_URL=https://ai.taileb6e.ts.net/bedrock
```

Headroom's existing Bedrock support is **outbound translation to real AWS** (the
`litellm-bedrock` backend signs SigV4). It has **no** handling of
`ANTHROPIC_BEDROCK_BASE_URL` and **no** route for incoming Bedrock invoke paths, so
it cannot sit transparently in front of this gateway.

Live probes of the aperture (no auth needed from the tailnet) confirmed the wire
format:

| Endpoint | Response `Content-Type` | Shape |
|---|---|---|
| `POST /model/{id}/invoke` | `application/json` | Anthropic message JSON + `X-Amzn-Bedrock-*` token headers |
| `POST /model/{id}/invoke-with-response-stream` | `application/vnd.amazon.eventstream` | **AWS binary event-stream** (base64 JSON inside binary frames) — **NOT SSE** |

The request body for both is the Anthropic Messages JSON with
`anthropic_version: "bedrock-2023-05-31"` and **no** top-level `model` (the model is
in the URL path).

**Design-critical consequence:** the streaming response is AWS binary event-stream.
Routing it through the existing `handle_anthropic_messages` path (which re-frames
responses as SSE) would corrupt it. We therefore need a **dedicated Bedrock
passthrough handler** that compresses the *request* body but streams the *response*
bytes through verbatim.

## Design

### Component 1 — Bedrock passthrough handler (new)

`headroom/proxy/handlers/bedrock_passthrough.py` (mixin method, e.g.
`handle_bedrock_invoke`):

1. Read the request JSON body.
2. Run the existing Anthropic compression core
   `self.anthropic_pipeline.apply(messages=, system=, tools=, model=, model_limit=)`
   on it. Model id comes from the URL path. This is where the token savings happen.
3. Re-serialize the (possibly compressed) body.
4. Forward to `{bedrock_base_url}{request.url.path}` via the proxy's httpx client,
   **streaming**, preserving method, query, and content-type, and **without
   injecting any auth header** (skip-auth gateway). Drop hop-by-hop headers; preserve
   `accept` / content-type.
5. Stream the raw upstream response bytes straight back, preserving the upstream
   `Content-Type` (handles both `application/json` and
   `application/vnd.amazon.eventstream`) and the `X-Amzn-Bedrock-*` headers.
6. Record request-side metrics (original vs compressed tokens). Response-side token
   counts are available from `X-Amzn-Bedrock-Output-Token-Count` on the non-stream
   path; the stream path records request-side only (no re-parsing of binary frames in
   v1).

A `x-headroom-bypass: true` request header skips compression (parity with other
handlers) for debugging.

### Component 2 — Routes (new, in `providers/proxy_routes.py`)

```
POST /model/{model_id}/invoke                       -> handle_bedrock_invoke(..., stream=False)
POST /model/{model_id}/invoke-with-response-stream  -> handle_bedrock_invoke(..., stream=True)
```

`model_id` is a single non-slash path segment (corelight ids are dot/colon
separated, e.g. `global.anthropic.claude-sonnet-4-6`). The routes are only
*functional* when a `bedrock_base_url` is configured; otherwise they return a clear
501/misconfig error (they never fall back to real AWS).

### Component 3 — Config (new field, mirrors existing URL overrides)

- `ProxyConfig.bedrock_base_url: str | None = None` in `proxy/models.py`.
- `--bedrock-base-url` flag on `headroom proxy` (next to `--anthropic-api-url`).
- Env var `ANTHROPIC_BEDROCK_TARGET_URL` wired in `_proxy_config_from_env()`.
- Compression policy knob: `--bedrock-compression {aggressive,lossless}`
  (default `aggressive`). The aperture is the user's paid company endpoint, so
  default to full lossy compression for maximum savings; the flag exists because the
  gateway's tolerance for compressed request shape is unverified.

### Component 4 — `wrap claude` integration (auto-detect)

In `headroom/cli/wrap.py`, when wrapping `claude`:

- **Auto-detect**: if `CLAUDE_CODE_USE_BEDROCK` is truthy and
  `ANTHROPIC_BEDROCK_BASE_URL` is set in the environment (the corelight profile),
  enter Bedrock mode automatically. A `--bedrock-base-url` flag overrides/forces it.
- In Bedrock mode, wrap:
  - captures the real aperture URL, passes it to the proxy as `--bedrock-base-url`
    (and/or `ANTHROPIC_BEDROCK_TARGET_URL` in the proxy subprocess env),
  - rewrites the child Claude Code env so
    `ANTHROPIC_BEDROCK_BASE_URL=http://127.0.0.1:{port}` (no path suffix — Claude
    appends `/model/{id}/invoke...`, and the handler forwards to
    `{aperture}/model/{id}/invoke...`),
  - preserves `CLAUDE_CODE_USE_BEDROCK=1` / `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`,
  - leaves `modelOverrides` / model env vars untouched (the gateway resolves them).

This keeps the common case zero-flag: a corelight user just runs `headroom wrap
claude` and gets compression in front of their company model.

## Testing

- **Unit:** new route registration; `handle_bedrock_invoke` request compression +
  verbatim response passthrough (mock httpx returning both `application/json` and
  `application/vnd.amazon.eventstream`); config parsing for the new flag/env;
  wrap auto-detect logic (env present vs absent, flag override).
- **Live smoke:** with the proxy running and `--bedrock-base-url` pointed at the real
  aperture, replay a small invoke + invoke-with-response-stream and assert
  byte-faithful response + a non-trivial token reduction on a large prompt.
- **Verification of parts 1 & 2:** run `headroom wrap claude` / `headroom wrap codex`
  on the subscription logins and confirm traffic flows through the proxy
  (`/stats`, proxy log) without auth errors. Document the commands.

## Non-goals

- No SigV4 / real-AWS Bedrock changes (existing `litellm-bedrock` path is untouched).
- No re-parsing of AWS binary event-stream frames for response-side token accounting
  in v1.
- No new auth handling for subscriptions (already present); only verification + docs.
```

