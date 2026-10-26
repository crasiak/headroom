# Fork divergences to keep when pulling upstream

This fork (crasiak/headroom) carries changes that an upstream merge can silently
undo. After every merge from headroomlabs-ai/headroom, check each item below.

## Security floors (Python)

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
- Known residual (2026-10-26): `undici` and `ip-address` **bundled inside**
  `npm@12.2.1`, which is the latest release. Bundled copies cannot be overridden.
  Bump the `npm` override once a release ships patched copies.
- Regenerate lockfiles with Node's own npm (11+). npm below 8.3 ignores `overrides`.

## Behaviour

- `headroom/transport/runtime.py` `transport_proxy_config`: `http2=False`
  (CRA-1265). Aperture's graceful HTTP/2 GOAWAY made httpcore abort in-flight
  streams, which surfaced as 502 `upstream_request_error`. Upstream defaults to HTTP/2.
- `headroom/transport/forwarder.py` records the upstream exception class as
  `upstream_error` on the request's terminal diagnostic event.
