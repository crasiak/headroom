# Getting Started: Subscriptions & Company Bedrock

Headroom supports three non-API-key access modes: **Claude Max subscription** (OAuth),
**OpenAI Pro / ChatGPT subscription** (Codex OAuth), and **company Bedrock via a
Tailscale gateway** ("aperture"). You do not need an API key for any of these modes.
See the [design doc](superpowers/specs/2026-06-11-subscription-and-aperture-bedrock-design.md)
for the full architecture and wire-format analysis.

---

## Mode 1: Claude Max subscription (`headroom wrap claude`)

Claude Code authenticates with a `sk-ant-oat-*` OAuth bearer token (not an API key).
`headroom wrap claude` sets `ANTHROPIC_BASE_URL` to the local proxy; the proxy
classifies the token as `OAuth` mode and passes it through unchanged to Anthropic.

```bash
headroom wrap claude
```

No extra flags are needed. Claude Code proceeds normally; the proxy handles compression
transparently.

**Confirm traffic is flowing through the proxy:**

```bash
# Check live counters
curl -s http://127.0.0.1:8787/stats | jq .

# Or tail the proxy log
tail -f ~/.headroom/logs/proxy.log
```

---

## Mode 2: OpenAI Pro / ChatGPT subscription (`headroom wrap codex`)

Codex CLI authenticates via a 3-segment JWT bearer token. Without intervention, Codex
can resolve its provider URL independently and bypass the proxy. `headroom wrap codex`
calls `_inject_codex_provider_config` to override Codex's `openai_base_url`, forcing
all traffic through the local proxy. The proxy passes the JWT through unchanged.

```bash
headroom wrap codex
```

**Confirm traffic is flowing through the proxy:**

```bash
curl -s http://127.0.0.1:8787/stats | jq .
```

---

## Mode 3: Company Bedrock via Tailscale aperture

This is the main new capability. Headroom proxies Bedrock wire-protocol traffic
(`POST /model/{id}/invoke` and `POST /model/{id}/invoke-with-response-stream`)
to a company Bedrock gateway reachable over Tailscale. The proxy compresses the
request body (Anthropic Messages JSON) and relays the response verbatim — both
non-streaming (`application/json`) and streaming
(`application/vnd.amazon.eventstream`) responses are handled correctly.

### Zero-flag auto-detect (corelight profile)

If your environment already has the corelight profile variables set, `headroom wrap claude`
auto-detects the aperture with no extra flags:

```bash
# These three env vars in your environment trigger auto-detect:
export CLAUDE_CODE_USE_BEDROCK=1
export CLAUDE_CODE_SKIP_BEDROCK_AUTH=1
export ANTHROPIC_BEDROCK_BASE_URL=https://ai.taileb6e.ts.net/bedrock

headroom wrap claude
```

On startup you will see a line like:

```
  Bedrock aperture: https://ai.taileb6e.ts.net/bedrock (via http://127.0.0.1:8787)
```

The proxy rewrites the child's `ANTHROPIC_BEDROCK_BASE_URL` to point at the local
proxy. The gateway authorizes by Tailscale identity — no client SigV4 is required
(hence `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`).

### Explicit / force a specific aperture URL

Pass `--bedrock-base-url` to force the aperture regardless of the environment:

```bash
headroom wrap claude --bedrock-base-url https://ai.taileb6e.ts.net/bedrock
```

### Standalone proxy (no `wrap`)

Start the proxy directly and point any Bedrock client at it:

```bash
# Via flag:
headroom proxy --bedrock-base-url https://ai.taileb6e.ts.net/bedrock

# Or via env var:
BEDROCK_TARGET_API_URL=https://ai.taileb6e.ts.net/bedrock headroom proxy
```

Then configure your Bedrock client to use the local proxy. For Claude Code:

```bash
export CLAUDE_CODE_USE_BEDROCK=1
export CLAUDE_CODE_SKIP_BEDROCK_AUTH=1
export ANTHROPIC_BEDROCK_BASE_URL=http://127.0.0.1:8787   # bare — no /bedrock suffix
                                                           # the proxy forwards the path
```

### Compression policy

The `--bedrock-compression` flag (or `HEADROOM_BEDROCK_COMPRESSION` env var) controls
request-body compression. Since this is your company's paid endpoint, the default is
`aggressive`.

```bash
headroom proxy --bedrock-base-url https://ai.taileb6e.ts.net/bedrock \
               --bedrock-compression aggressive   # default — compress request body
               # --bedrock-compression lossless   # lossless only (not yet distinct from aggressive in v1)
               # --bedrock-compression off        # disable compression entirely
```

To skip compression for a single request, set the header:

```
x-headroom-bypass: true
```

Request-side token savings are reported on `GET /stats`. Response-side token
accounting is available on the non-streaming path only in v1.

### What the proxy does and does not change

- **Does:** compresses the Anthropic Messages JSON request body before forwarding.
- **Does:** relays the response byte-for-byte (`application/json` and AWS binary
  `application/vnd.amazon.eventstream` both work).
- **Does not:** add or verify SigV4 signatures. The existing `litellm-bedrock` backend
  (real-AWS SigV4) is untouched.

---

## Troubleshooting

- **HTTP 501 "not configured"** — the proxy received a Bedrock invoke path but no
  aperture URL was configured. Set `--bedrock-base-url` or `BEDROCK_TARGET_API_URL`.

- **Connection errors to the aperture** — verify Tailscale is connected and the
  aperture is reachable: `curl -s https://ai.taileb6e.ts.net/bedrock` (or the relevant
  path) from your machine.

- **Auth errors** — confirm `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1` is set so Claude Code
  does not attempt SigV4 signing. The gateway authenticates by Tailscale identity
  automatically.

- **General proxy issues** — check `~/.headroom/logs/proxy.log` for structured log
  lines including `auth_mode_classified` and any upstream error details.
