# Fork divergences to keep when pulling upstream

This fork (crasiak/headroom) carries changes that an upstream merge can silently
undo. After every merge from headroomlabs-ai/headroom, check each item below.

## Security floors (Python)

- CRA-1534 intentionally raises this personal fork's Python minimum to **3.11**
  for patched `filelock>=4.2.1` (CVE-2026-112909/112910), with
  `litellm>=1.101.3` (CVE-2026-114612). Floors are enforced in wheel/pip
  `Requires-Dist` as well as the canonical uv lock; tool.uv alone does not
  constrain a pip install into an existing environment. Do not restore vulnerable 3.10 marker
  fallbacks. Existing static-analysis dialect/native ABI floors are not support
  promises. Preserve the explicit fork distinction in installation docs.
- `[tool.uv] constraint-dependencies` in `pyproject.toml` is the record. On a
  conflict, keep the **union** of both lists, with the higher floor for each package.
- Never resolve a `uv.lock` conflict by taking upstream's file. Take either side,
  then run `uv lock` so the floors apply again, and confirm with:
  `uv export --frozen --no-dev --no-emit-project --no-hashes --extra all --format requirements-txt > /tmp/req.txt && uvx pip-audit -r /tmp/req.txt --disable-pip --no-deps`
- `mcp>=1.30.0` needs `crewai>=1.15.30`; older crewai pins `mcp~=1.28.1`.
- `pillow` is split by Python version in the `image` extra: 13.x needs 3.11+,
  and 3.10 stays on 12.3 until `requires-python` moves.

## Security overrides (npm)

- `plugins/opencode/package.json` `overrides`: `zod`, `toml`. `@opencode-ai/plugin`
  pins vulnerable versions (zod 4.1.8, toml via effect).
- `e2e/wrap/tools/package.json` keeps the explicit npm artifact dependency,
  `$npm` reference and the complete npm-scoped glob/minimatch/ip-address/undici
  profile. Installed dependency overrides are not inherited by consumers; frozen
  `ci` needs the outer profile too. Keep ordinary minimatch/undici/zod overrides
  separate from npm's undici6 subtree; OpenClaw's own pins are vulnerable.
- CRA-1534's candidate [npm security distribution](e2e/wrap/tools/npm-security/README.md)
  rebuilds the npm 12.2.1 payload from verified upstream code and a separately
  audited complete lock; downstream overrides cannot fix bundled copies.
  Preserve its recipe, integrity, honest fork version, licenses, full-inventory
  checks and offline CLI regressions. Keep the OpenClaw/Codex pins and ordinary
  undici/zod overrides. Do not force minimatch 11 under glob 13: matcher APIs changed.
  Source publication/CI qualification is not installed-runtime adoption.
- Official npm 12.2.1 still bundles vulnerable copies; retire the local
  distribution only after inspecting and qualifying a genuinely patched upstream
  release. A closed upstream issue or a lock-only audit count is not sufficient.
- Regenerate lockfiles with Node's own npm (11+). npm below 8.3 ignores `overrides`.
- After any `plugins/opencode` dependency change, run `npm run build:standalone`
  and copy `dist-standalone/{entry.opencode.js,hook-shim/handler.js}` into
  `headroom/providers/opencode/`. CI compares them byte for byte, because the wheel bundles zod.

## Behaviour

- `headroom/transport/runtime.py` `transport_proxy_config`: `http2=False`
  (CRA-1265). Aperture's graceful HTTP/2 GOAWAY made httpcore abort in-flight
  streams, which surfaced as 502 `upstream_request_error`. Upstream defaults to HTTP/2.
- `headroom/transport/forwarder.py` records the upstream exception class as
  `upstream_error` on the request's terminal diagnostic event.
- CRA-1489: cancelled OpenAI model-list delivery has explicit receipt provenance.
  An observed ASGI downstream disconnect/send `OSError` after upstream 2xx headers,
  before completion and without an observed upstream read error, emits
  `error_reason: metadata_client_cancelled`. The receipt stays **failed** with
  evaluated, zero-token metadata accounting and no bypass. Generic HTTP 499,
  upstream errors, inference/token-counting interruption and shutdown cancellation
  do not get that marker. Ledger separately decides whether that precise optional
  failure may coexist with successful inference; older consumers reject it safely.
  Preserve `tests/test_transport_metadata_cancellation.py` when merging upstream.
