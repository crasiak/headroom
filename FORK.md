# Fork divergences to keep when pulling upstream

This fork (crasiak/headroom) carries changes that an upstream merge can silently
undo. After every merge from headroomlabs-ai/headroom, check each item below.

## Security floors (Python)

- CRA-1534 intentionally raises this personal fork's Python minimum to **3.11**
  for patched `filelock>=4.2.1` (CVE-2026-112909/112910), with
  `litellm>=1.101.3` (CVE-2026-114612). Do not restore vulnerable 3.10 marker
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
- `e2e/wrap/tools/package.json` `overrides`: `npm`, `undici`, `zod`. openclaw pins
  vulnerable versions.
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
