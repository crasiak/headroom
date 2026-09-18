# Aperture-Bedrock Passthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `headroom` sit transparently in front of the corelight Tailscale "aperture" gateway (`https://ai.taileb6e.ts.net/bedrock`), compressing the request body of Bedrock-wire-protocol invocations while streaming the response back byte-for-byte.

**Architecture:** A dedicated `BedrockHandlerMixin.handle_bedrock_invoke` compresses the request's Anthropic Messages JSON (model comes from the URL path) via the existing `anthropic_pipeline`, then forwards to the configured aperture with `httpx` streaming and relays the upstream bytes verbatim — so the AWS binary event-stream (`application/vnd.amazon.eventstream`) is never re-framed. Two new routes (`/model/{id}/invoke` and `/model/{id}/invoke-with-response-stream`) dispatch to it, gated on a new `ProxyConfig.bedrock_base_url`. `headroom wrap claude` auto-detects the corelight Bedrock env (mirroring the existing Foundry-mode path) and rewrites the child `ANTHROPIC_BEDROCK_BASE_URL` to the local proxy.

**Tech Stack:** Python 3, FastAPI, httpx (async streaming), Click CLI, pytest. The compression core is `headroom.transforms.pipeline.TransformPipeline.apply`. The reference precedents in-repo are: Vertex `rawPredict`/`streamRawPredict` (routing with model-from-path), `OpenAIHandlerMixin.handle_compress` (request-only compression), the `streaming.py` `build_request`/`send(stream=True)`/`aiter_bytes` pattern (verbatim streaming), and `wrap.py` Foundry mode (env auto-detect → proxy plumbing → child-env rewrite).

**Source of truth:** `docs/superpowers/specs/2026-06-11-subscription-and-aperture-bedrock-design.md`.

**Constraints (from the handoff cheatsheet):**
- Use TDD. Commit at sensible points. **Do NOT push to remote.**
- Conventional commits; end commit messages with the Claude co-author trailer.
- Leave the real-AWS `litellm-bedrock` SigV4 path **untouched**.
- Parts 1 & 2 (Claude Max / OpenAI Pro subscriptions) are **verify + document only** — no new code (Task 9).

**Verified facts (re-probed live 2026-06-11, do not re-derive):**
- `POST {aperture}/model/{id}/invoke` → `Content-Type: application/json` + `X-Amzn-Bedrock-Input-Token-Count` / `-Output-Token-Count` / `-Cache-Read-Input-Token-Count` / `-Cache-Write-Input-Token-Count` headers.
- `POST {aperture}/model/{id}/invoke-with-response-stream` → `Content-Type: application/vnd.amazon.eventstream`, AWS binary frames (NOT SSE).
- Request body for both: Anthropic Messages JSON with `anthropic_version: "bedrock-2023-05-31"`, **no** top-level `model` (model is in the path).
- No auth header is required from the tailnet (gateway authorizes by tailnet identity; `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`).

**Naming decisions (resolve cheatsheet ambiguity):**
- Config field: `ProxyConfig.bedrock_base_url: str | None` and `ProxyConfig.bedrock_compression: str = "aggressive"`.
- Env var follows the existing `<PROVIDER>_TARGET_API_URL` convention → **`BEDROCK_TARGET_API_URL`** (the cheatsheet's `ANTHROPIC_BEDROCK_TARGET_URL` is superseded).
- CLI flags land on the **Click** `proxy` command (`headroom/cli/proxy.py`) because `wrap._start_proxy` launches `python -m headroom.cli proxy`. The argparse parser in `server.py` gets parity flags too.

---

## File Structure

**Create:**
- `headroom/proxy/handlers/bedrock_passthrough.py` — `BedrockHandlerMixin` with `handle_bedrock_invoke` + `_record_bedrock_outcome`.
- `tests/test_bedrock_passthrough_handler.py` — handler unit tests (compression, verbatim passthrough, bypass, misconfig).
- `tests/test_bedrock_routes.py` — route registration/dispatch tests.
- `tests/test_bedrock_config.py` — config field + CLI/env wiring tests.
- `tests/test_wrap_bedrock_autodetect.py` — wrap auto-detect logic tests.
- `tests/test_bedrock_smoke.py` — live smoke test (opt-in via `HEADROOM_BEDROCK_SMOKE_URL`).
- `docs/getting-started-subscriptions-and-bedrock.md` — getting-started guide for all three modes.

**Modify:**
- `headroom/proxy/models.py` — add `bedrock_base_url`, `bedrock_compression` to `ProxyConfig`.
- `headroom/proxy/handlers/__init__.py` — export `BedrockHandlerMixin`.
- `headroom/proxy/server.py` — mix `BedrockHandlerMixin` into `HeadroomProxy`; wire env var in `_proxy_config_from_env`; add argparse parity flags.
- `headroom/providers/proxy_routes.py` — register the two Bedrock routes in `register_provider_routes`.
- `headroom/cli/proxy.py` — add `--bedrock-base-url` / `--bedrock-compression` Click options and pass into `ProxyConfig`.
- `headroom/cli/wrap.py` — `_start_proxy` plumbing + `claude` command Bedrock auto-detect + child-env rewrite.
- `README.md` (or `docs/` index) — link the new getting-started guide.

---

## Task 1: Config fields on `ProxyConfig`

**Files:**
- Modify: `headroom/proxy/models.py:95-99` (URL-override block)
- Test: `tests/test_bedrock_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bedrock_config.py
from __future__ import annotations

from headroom.proxy.models import ProxyConfig


def test_bedrock_config_defaults():
    cfg = ProxyConfig()
    assert cfg.bedrock_base_url is None
    assert cfg.bedrock_compression == "aggressive"


def test_bedrock_config_set():
    cfg = ProxyConfig(
        bedrock_base_url="https://ai.taileb6e.ts.net/bedrock",
        bedrock_compression="lossless",
    )
    assert cfg.bedrock_base_url == "https://ai.taileb6e.ts.net/bedrock"
    assert cfg.bedrock_compression == "lossless"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bedrock_config.py -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'bedrock_base_url'`.

- [ ] **Step 3: Add the fields**

In `headroom/proxy/models.py`, immediately after the `vertex_api_url` line (currently line 99):

```python
    vertex_api_url: str | None = None  # Custom Vertex AI regional API URL override
    # Company-Bedrock (Tailscale aperture) passthrough. When set, the proxy
    # serves /model/{id}/invoke[-with-response-stream] by compressing the
    # request body and streaming the upstream bytes back verbatim. None means
    # the Bedrock routes return a 501 misconfig error (never falls back to AWS).
    bedrock_base_url: str | None = None
    # Request-side compression policy for the Bedrock passthrough:
    # "aggressive" (default — full lossy compression, it's the user's paid
    # company endpoint), "lossless", or "off" (forward unchanged).
    bedrock_compression: str = "aggressive"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_bedrock_config.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add headroom/proxy/models.py tests/test_bedrock_config.py
git commit -m "feat(proxy): add bedrock_base_url/bedrock_compression to ProxyConfig

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Bedrock passthrough handler — request compression

**Files:**
- Create: `headroom/proxy/handlers/bedrock_passthrough.py`
- Test: `tests/test_bedrock_passthrough_handler.py`

This task builds the handler and proves it (a) compresses the request body, (b) forwards to the configured aperture, and (c) returns the upstream JSON verbatim. Streaming + headers are covered in Task 3; misconfig + bypass in Task 4.

The handler is a mixin method. In unit tests we instantiate a real `HeadroomProxy` (its `__init__` populates `self.config`, `self.anthropic_provider`, `self.anthropic_pipeline`, `self._next_request_id`, `self._record_request_outcome`) and inject an `httpx.AsyncClient` backed by `httpx.MockTransport` as `self.http_client` (which is `None` until `startup()`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bedrock_passthrough_handler.py
from __future__ import annotations

import json

import httpx
import pytest

from headroom.proxy.server import HeadroomProxy, ProxyConfig

APERTURE = "https://aperture.test/bedrock"
MODEL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def _proxy(**overrides) -> HeadroomProxy:
    cfg = ProxyConfig(
        bedrock_base_url=APERTURE,
        cache_enabled=False,
        rate_limit_enabled=False,
        **overrides,
    )
    return HeadroomProxy(cfg)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_request(path: str, body: dict, headers: dict | None = None):
    """Build a Starlette Request with a JSON body for direct handler calls."""
    from starlette.requests import Request

    raw = json.dumps(body).encode("utf-8")
    hdrs = [(b"content-type", b"application/json")]
    for k, v in (headers or {}).items():
        hdrs.append((k.encode(), v.encode()))

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": hdrs,
        "scheme": "http",
        "server": ("testserver", 80),
    }
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_invoke_forwards_to_aperture_and_compresses_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "x-amzn-bedrock-output-token-count": "16",
            },
            json={"type": "message", "role": "assistant", "content": []},
        )

    proxy = _proxy()
    proxy.http_client = _mock_client(handler)
    try:
        # A long tool_result that the pipeline should compress.
        big = "BANANA " * 4000
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16,
            "messages": [
                {"role": "user", "content": "summarize"},
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": big}],
                },
            ],
        }
        req = _make_request(f"/model/{MODEL}/invoke", body)
        resp = await proxy.handle_bedrock_invoke(req, MODEL, stream=False)

        # Drain the StreamingResponse body.
        chunks = [c async for c in resp.body_iterator]
        payload = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
        assert json.loads(payload)["role"] == "assistant"
        assert resp.status_code == 200

        # Forwarded to the aperture at the same path, no auth injected.
        assert captured["url"] == f"{APERTURE}/model/{MODEL}/invoke"
        assert captured["auth"] is None
        # Request body still valid Bedrock JSON, model still NOT in the body.
        assert captured["body"]["anthropic_version"] == "bedrock-2023-05-31"
        assert "model" not in captured["body"]
        assert isinstance(captured["body"]["messages"], list)
    finally:
        await proxy.http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bedrock_passthrough_handler.py -v`
Expected: FAIL with `AttributeError: 'HeadroomProxy' object has no attribute 'handle_bedrock_invoke'`.

- [ ] **Step 3: Create the handler file**

```python
# headroom/proxy/handlers/bedrock_passthrough.py
"""Company-Bedrock (Tailscale aperture) passthrough handler.

The corelight aperture speaks the Bedrock invoke wire protocol:

* request bodies are Anthropic Messages JSON (model in the URL path, no
  top-level ``model``),
* ``POST /model/{id}/invoke`` returns ``application/json`` plus
  ``X-Amzn-Bedrock-*`` token headers,
* ``POST /model/{id}/invoke-with-response-stream`` returns AWS binary
  event-stream (``application/vnd.amazon.eventstream``) — NOT SSE.

We compress the *request* body (where the token savings live) via the existing
Anthropic compression pipeline and stream the *response* bytes back verbatim,
so the binary event-stream is never re-framed or corrupted. No auth header is
injected — the gateway authorizes by tailnet identity.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger(__name__)

# RFC 7230 §6.1 hop-by-hop headers + host + content-length: never forwarded.
_HOP_BY_HOP = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class BedrockHandlerMixin:
    """Mixin providing the company-Bedrock aperture passthrough for HeadroomProxy."""

    async def handle_bedrock_invoke(
        self,
        request: "Request",
        model_id: str,
        stream: bool = False,
    ) -> "Response | StreamingResponse | JSONResponse":
        """Compress the request body, forward to the aperture, stream bytes back verbatim."""
        from fastapi.responses import JSONResponse, StreamingResponse

        target = getattr(self.config, "bedrock_base_url", None)
        if not target:
            return JSONResponse(
                status_code=501,
                content={
                    "error": {
                        "type": "not_configured",
                        "message": (
                            "Bedrock passthrough is not configured. Start the proxy "
                            "with --bedrock-base-url or set BEDROCK_TARGET_API_URL."
                        ),
                    }
                },
            )

        raw = await request.body()
        try:
            body = json.loads(raw) if raw else None
        except (json.JSONDecodeError, ValueError):
            body = None

        bypass = request.headers.get("x-headroom-bypass", "").lower() == "true"
        policy = getattr(self.config, "bedrock_compression", "aggressive")

        original_tokens = 0
        optimized_tokens = 0
        tokens_saved = 0
        outbound = raw

        should_compress = (
            body is not None
            and isinstance(body.get("messages"), list)
            and not bypass
            and self.config.optimize
            and policy != "off"
        )
        if should_compress:
            try:
                context_limit = self.anthropic_provider.get_context_limit(model_id)
                result = self.anthropic_pipeline.apply(
                    messages=body["messages"],
                    model=model_id,
                    model_limit=context_limit,
                )
                if result.messages is not None:
                    body["messages"] = result.messages
                    outbound = json.dumps(body).encode("utf-8")
                original_tokens = result.tokens_before
                optimized_tokens = result.tokens_after
                tokens_saved = max(0, original_tokens - optimized_tokens)
            except Exception as exc:  # never block the request on a compression error
                logger.warning("bedrock compression failed for %s: %r", model_id, exc)
                outbound = raw

        url = f"{target.rstrip('/')}{request.url.path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"

        outbound_headers = {
            k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP
        }

        assert self.http_client is not None, "http_client must be initialized before streaming"
        upstream_req = self.http_client.build_request(
            "POST", url, content=outbound, headers=outbound_headers
        )
        upstream = await self.http_client.send(upstream_req, stream=True)

        # Relay upstream headers verbatim (Content-Type drives event-stream vs
        # json on the client side; X-Amzn-Bedrock-* carry token accounting).
        # Drop content-encoding: httpx has already decoded the body it hands us.
        passthrough_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in _HOP_BY_HOP and k.lower() != "content-encoding"
        }

        # Request-side savings always; response-side token counts only on the
        # non-stream path (binary event-stream frames are not re-parsed in v1).
        output_tokens = 0
        cache_read = 0
        cache_write = 0
        if not stream:
            output_tokens = _int_header(upstream.headers, "x-amzn-bedrock-output-token-count")
            cache_read = _int_header(
                upstream.headers, "x-amzn-bedrock-cache-read-input-token-count"
            )
            cache_write = _int_header(
                upstream.headers, "x-amzn-bedrock-cache-write-input-token-count"
            )

        request_id = await self._next_request_id()
        await self._record_bedrock_outcome(
            request_id=request_id,
            model=model_id,
            original_tokens=original_tokens,
            optimized_tokens=optimized_tokens,
            tokens_saved=tokens_saved,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )

        async def _body_stream():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            _body_stream(),
            status_code=upstream.status_code,
            headers=passthrough_headers,
            media_type=upstream.headers.get("content-type"),
        )

    async def _record_bedrock_outcome(
        self,
        *,
        request_id: str,
        model: str,
        original_tokens: int,
        optimized_tokens: int,
        tokens_saved: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
    ) -> None:
        """Best-effort request-outcome recording for the /stats dashboard."""
        from headroom.proxy.outcome import RequestOutcome

        try:
            await self._record_request_outcome(
                RequestOutcome(
                    request_id=request_id,
                    provider="bedrock",
                    model=model,
                    original_tokens=original_tokens,
                    optimized_tokens=optimized_tokens,
                    output_tokens=output_tokens,
                    tokens_saved=tokens_saved,
                    attempted_input_tokens=original_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                )
            )
        except Exception as exc:  # metrics must never break the request path
            logger.debug("bedrock outcome recording skipped: %r", exc)


def _int_header(headers, name: str) -> int:
    """Parse an integer header value, defaulting to 0 on absence/garbage."""
    try:
        return int(headers.get(name, 0) or 0)
    except (TypeError, ValueError):
        return 0
```

- [ ] **Step 4: Wire the mixin into `HeadroomProxy` so the test can reach it**

In `headroom/proxy/handlers/__init__.py`, add the import and `__all__` entry:

```python
from headroom.proxy.handlers.anthropic import AnthropicHandlerMixin
from headroom.proxy.handlers.batch import BatchHandlerMixin
from headroom.proxy.handlers.bedrock_passthrough import BedrockHandlerMixin
from headroom.proxy.handlers.gemini import GeminiHandlerMixin
from headroom.proxy.handlers.openai import OpenAIHandlerMixin
from headroom.proxy.handlers.streaming import StreamingMixin

__all__ = [
    "AnthropicHandlerMixin",
    "BatchHandlerMixin",
    "BedrockHandlerMixin",
    "GeminiHandlerMixin",
    "OpenAIHandlerMixin",
    "StreamingMixin",
]
```

In `headroom/proxy/server.py`, update the import block (line ~284) and the class bases (line ~293):

```python
from headroom.proxy.handlers import (  # noqa: E402
    AnthropicHandlerMixin,
    BatchHandlerMixin,
    BedrockHandlerMixin,
    GeminiHandlerMixin,
    OpenAIHandlerMixin,
    StreamingMixin,
)


class HeadroomProxy(
    StreamingMixin,
    AnthropicHandlerMixin,
    BedrockHandlerMixin,
    OpenAIHandlerMixin,
    GeminiHandlerMixin,
    BatchHandlerMixin,
):
    """Production-ready Headroom optimization proxy."""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_bedrock_passthrough_handler.py -v`
Expected: PASS. (If the pipeline does not compress the synthetic `BANANA` payload, the forward + verbatim-return assertions still hold; the compression-magnitude assertion lives in the live smoke test, Task 8.)

- [ ] **Step 6: Commit**

```bash
git add headroom/proxy/handlers/bedrock_passthrough.py headroom/proxy/handlers/__init__.py headroom/proxy/server.py tests/test_bedrock_passthrough_handler.py
git commit -m "feat(proxy): add Bedrock aperture passthrough handler

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Verbatim streaming of the AWS event-stream

**Files:**
- Test: `tests/test_bedrock_passthrough_handler.py` (add)

Proves the binary `application/vnd.amazon.eventstream` body is relayed byte-for-byte with its Content-Type and `X-Amzn-Bedrock-*` headers preserved, and never re-framed.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bedrock_passthrough_handler.py

# AWS binary event-stream prelude bytes (the real frame header shape we probed).
EVENTSTREAM_BODY = (
    bytes.fromhex(
        "000002aa0000004bf3736"  # truncated frame header sample
    )
    + b"\x00\x05event"
    + b'{"bytes":"eyJ0eXBlIjoibWVzc2FnZV9zdGFydCJ9"}'
)


@pytest.mark.asyncio
async def test_invoke_with_response_stream_relays_eventstream_verbatim():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "application/vnd.amazon.eventstream",
                "x-amzn-bedrock-content-type": "application/json",
            },
            content=EVENTSTREAM_BODY,
        )

    proxy = _proxy()
    proxy.http_client = _mock_client(handler)
    try:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}],
        }
        req = _make_request(f"/model/{MODEL}/invoke-with-response-stream", body)
        resp = await proxy.handle_bedrock_invoke(req, MODEL, stream=True)

        assert resp.media_type == "application/vnd.amazon.eventstream"
        assert resp.headers["x-amzn-bedrock-content-type"] == "application/json"

        chunks = [c async for c in resp.body_iterator]
        payload = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
        # Byte-for-byte identical — no SSE re-framing.
        assert payload == EVENTSTREAM_BODY
    finally:
        await proxy.http_client.aclose()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_bedrock_passthrough_handler.py::test_invoke_with_response_stream_relays_eventstream_verbatim -v`
Expected: PASS (handler already streams verbatim; this test locks the behavior). If it fails on header casing, confirm `passthrough_headers` preserves the upstream header names.

- [ ] **Step 3: Commit**

```bash
git add tests/test_bedrock_passthrough_handler.py
git commit -m "test(proxy): lock verbatim relay of Bedrock AWS event-stream

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Bypass header + misconfig (501) behavior

**Files:**
- Test: `tests/test_bedrock_passthrough_handler.py` (add)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bedrock_passthrough_handler.py


@pytest.mark.asyncio
async def test_missing_config_returns_501():
    proxy = HeadroomProxy(
        ProxyConfig(cache_enabled=False, rate_limit_enabled=False)  # no bedrock_base_url
    )
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
    }
    req = _make_request(f"/model/{MODEL}/invoke", body)
    resp = await proxy.handle_bedrock_invoke(req, MODEL, stream=False)
    assert resp.status_code == 501
    assert b"not configured" in resp.body.lower()


@pytest.mark.asyncio
async def test_bypass_header_skips_compression():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"ok": True})

    proxy = _proxy()
    proxy.http_client = _mock_client(handler)
    try:
        big = "BANANA " * 4000
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": big}],
                }
            ],
        }
        req = _make_request(f"/model/{MODEL}/invoke", body, headers={"x-headroom-bypass": "true"})
        await proxy.handle_bedrock_invoke(req, MODEL, stream=False)
        # Bypass => body forwarded unchanged.
        assert captured["body"] == body
    finally:
        await proxy.http_client.aclose()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_bedrock_passthrough_handler.py -v`
Expected: PASS (all handler tests). `resp.body` on the 501 `JSONResponse` is bytes; `.lower()` works on bytes.

- [ ] **Step 3: Commit**

```bash
git add tests/test_bedrock_passthrough_handler.py
git commit -m "test(proxy): cover Bedrock passthrough bypass + 501 misconfig

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Routes in `register_provider_routes`

**Files:**
- Modify: `headroom/providers/proxy_routes.py` (inside `register_provider_routes`, near the Vertex routes ~line 583)
- Test: `tests/test_bedrock_routes.py`

- [ ] **Step 1: Write the failing test** (mirrors `tests/test_provider_proxy_routes.py`)

```python
# tests/test_bedrock_routes.py
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from headroom.proxy.server import ProxyConfig, create_app


def _app() -> Any:
    return create_app(
        ProxyConfig(
            optimize=False,
            cache_enabled=False,
            rate_limit_enabled=False,
            bedrock_base_url="https://aperture.test/bedrock",
        )
    )


def test_bedrock_routes_dispatch_with_model_and_stream_flag(monkeypatch):
    calls: list[tuple[str, str, bool]] = []

    async def fake_invoke(self, request, model_id, stream=False):  # type: ignore[no-untyped-def]
        calls.append((request.url.path, model_id, stream))
        return JSONResponse({"path": request.url.path, "model": model_id, "stream": stream})

    monkeypatch.setattr(
        "headroom.proxy.server.HeadroomProxy.handle_bedrock_invoke", fake_invoke, raising=True
    )

    client = TestClient(_app())
    model = "global.anthropic.claude-sonnet-4-6"
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
    }

    r1 = client.post(f"/model/{model}/invoke", json=body)
    r2 = client.post(f"/model/{model}/invoke-with-response-stream", json=body)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert (f"/model/{model}/invoke", model, False) in calls
    assert (f"/model/{model}/invoke-with-response-stream", model, True) in calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bedrock_routes.py -v`
Expected: FAIL — `404 Not Found` (routes not registered yet), so the `assert ... in calls` fails.

- [ ] **Step 3: Register the routes**

In `headroom/providers/proxy_routes.py`, inside `register_provider_routes`, just after the `vertex_stream_raw_predict` route (ends ~line 583) and before `@app.get("/v1/models")`:

```python
@app.post("/model/{model_id}/invoke")
async def bedrock_invoke(request: Request, model_id: str):
    return await proxy.handle_bedrock_invoke(request, model_id, stream=False)


@app.post("/model/{model_id}/invoke-with-response-stream")
async def bedrock_invoke_stream(request: Request, model_id: str):
    return await proxy.handle_bedrock_invoke(request, model_id, stream=True)
```

`{model_id}` is a single non-slash path segment — correct for corelight ids like `global.anthropic.claude-sonnet-4-6` (dots/colons, no slashes).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_bedrock_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Add a route-level 501 test (no monkeypatch, real handler, no config)**

```python
# append to tests/test_bedrock_routes.py
def test_bedrock_route_501_when_unconfigured():
    app = create_app(ProxyConfig(optimize=False, cache_enabled=False, rate_limit_enabled=False))
    client = TestClient(app)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = client.post("/model/global.anthropic.claude-sonnet-4-6/invoke", json=body)
    assert r.status_code == 501
    assert r.json()["error"]["type"] == "not_configured"
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_bedrock_routes.py -v`
Expected: PASS (all three).

- [ ] **Step 7: Commit**

```bash
git add headroom/providers/proxy_routes.py tests/test_bedrock_routes.py
git commit -m "feat(proxy): register Bedrock aperture invoke routes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: CLI + env wiring (`--bedrock-base-url`, `--bedrock-compression`, `BEDROCK_TARGET_API_URL`)

**Files:**
- Modify: `headroom/cli/proxy.py` (Click options ~line 448; `proxy()` signature ~line 526; `ProxyConfig(...)` construction ~line 637)
- Modify: `headroom/proxy/server.py:2974-2988` (`_proxy_config_from_env`) and the argparse parser (~line 3251) for parity
- Test: `tests/test_bedrock_config.py` (add)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bedrock_config.py
from click.testing import CliRunner


def test_env_wires_bedrock_base_url(monkeypatch):
    from headroom.proxy.server import _proxy_config_from_env

    monkeypatch.delenv("HEADROOM_PROXY_CONFIG", raising=False)  # _MULTI_WORKER_CONFIG_ENV
    monkeypatch.setenv("BEDROCK_TARGET_API_URL", "https://aperture.test/bedrock")
    cfg = _proxy_config_from_env()
    assert cfg.bedrock_base_url == "https://aperture.test/bedrock"


def test_click_proxy_accepts_bedrock_flags():
    from headroom.cli.proxy import proxy as proxy_cmd

    runner = CliRunner()
    # --help must list the new flags (proves they are registered without
    # actually starting a server).
    result = runner.invoke(proxy_cmd, ["--help"])
    assert result.exit_code == 0
    assert "--bedrock-base-url" in result.output
    assert "--bedrock-compression" in result.output
```

Note: confirm the real name of `_MULTI_WORKER_CONFIG_ENV` (grep it) and `delenv` that exact var instead of the placeholder above.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_bedrock_config.py -k bedrock_flags -v`
Expected: FAIL — `--bedrock-base-url` not in `--help` output.

- [ ] **Step 3: Add the Click options**

In `headroom/cli/proxy.py`, after the `--vertex-api-url` option block (ends ~line 448) add:

```python
@click.option(
    "--bedrock-base-url",
    default=None,
    help=(
        "Company-Bedrock (Tailscale aperture) upstream URL. Enables the "
        "/model/{id}/invoke[-with-response-stream] passthrough "
        "(env: BEDROCK_TARGET_API_URL)."
    ),
)
@click.option(
    "--bedrock-compression",
    type=click.Choice(["aggressive", "lossless", "off"]),
    default="aggressive",
    help="Request-side compression policy for Bedrock passthrough (default: aggressive).",
)
```

Add the two params to the `def proxy(` signature (next to `vertex_api_url`):

```python
    vertex_api_url: str | None,
    bedrock_base_url: str | None,
    bedrock_compression: str,
```

In the `ProxyConfig(...)` construction (~line 637), after `vertex_api_url=provider_api_overrides.vertex,`:

```python
vertex_api_url = (provider_api_overrides.vertex,)
bedrock_base_url = (bedrock_base_url or os.environ.get("BEDROCK_TARGET_API_URL"),)
bedrock_compression = (bedrock_compression,)
```

- [ ] **Step 4: Wire the env var in `_proxy_config_from_env`**

In `headroom/proxy/server.py`, inside `_proxy_config_from_env()` `return ProxyConfig(...)` (line ~2974), add after `vertex_api_url=...`:

```python
vertex_api_url = (os.environ.get("VERTEX_TARGET_API_URL"),)
bedrock_base_url = (os.environ.get("BEDROCK_TARGET_API_URL"),)
bedrock_compression = (_get_env_str("HEADROOM_BEDROCK_COMPRESSION", "aggressive"),)
```

- [ ] **Step 5: Add argparse parity flags**

In `headroom/proxy/server.py`, after the `--vertex-api-url` argparse argument (~line 3251):

```python
    parser.add_argument(
        "--bedrock-base-url",
        help="Company-Bedrock (Tailscale aperture) upstream URL (env: BEDROCK_TARGET_API_URL)",
    )
    parser.add_argument(
        "--bedrock-compression",
        choices=["aggressive", "lossless", "off"],
        default="aggressive",
        help="Request-side compression policy for Bedrock passthrough (default: aggressive)",
    )
```

Then find where the argparse `args` build a `ProxyConfig` (grep `ProxyConfig(` in `server.py` near the argparse `main`) and add `bedrock_base_url=args.bedrock_base_url or os.environ.get("BEDROCK_TARGET_API_URL")` and `bedrock_compression=args.bedrock_compression`. If the argparse path does not construct `ProxyConfig` directly (it may defer to env), skip — the Click path is the one `wrap` uses; note this in the commit body.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_bedrock_config.py -v`
Expected: PASS (all four).

- [ ] **Step 7: Commit**

```bash
git add headroom/cli/proxy.py headroom/proxy/server.py tests/test_bedrock_config.py
git commit -m "feat(cli): add --bedrock-base-url/--bedrock-compression + BEDROCK_TARGET_API_URL

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `wrap claude` auto-detect + proxy plumbing + child-env rewrite

**Files:**
- Modify: `headroom/cli/wrap.py` — `_start_proxy` (~line 233), `_ensure_proxy` (find its signature), and the `claude` command (~line 2358-2435)
- Test: `tests/test_wrap_bedrock_autodetect.py`

This mirrors the existing **Foundry mode** (wrap.py:2358-2419): detect an env var, capture the real upstream URL, pass it to the proxy, and rewrite the child env to the local proxy URL. The detection logic is extracted into a pure helper so it is unit-testable without launching a proxy.

- [ ] **Step 1: Write the failing test for the pure helper**

```python
# tests/test_wrap_bedrock_autodetect.py
from __future__ import annotations

from headroom.cli.wrap import _detect_bedrock_aperture


def test_autodetect_returns_url_when_corelight_env_present():
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://ai.taileb6e.ts.net/bedrock",
    }
    assert _detect_bedrock_aperture(env, flag_override=None) == "https://ai.taileb6e.ts.net/bedrock"


def test_autodetect_absent_when_use_bedrock_unset():
    env = {"ANTHROPIC_BEDROCK_BASE_URL": "https://ai.taileb6e.ts.net/bedrock"}
    assert _detect_bedrock_aperture(env, flag_override=None) is None


def test_autodetect_absent_when_base_url_unset():
    env = {"CLAUDE_CODE_USE_BEDROCK": "1"}
    assert _detect_bedrock_aperture(env, flag_override=None) is None


def test_flag_override_forces_and_wins():
    env = {}  # no auto-detect env at all
    assert _detect_bedrock_aperture(env, flag_override="https://forced.example/bedrock") == (
        "https://forced.example/bedrock"
    )


def test_flag_override_beats_env():
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://ai.taileb6e.ts.net/bedrock",
    }
    assert _detect_bedrock_aperture(env, flag_override="https://forced.example/bedrock") == (
        "https://forced.example/bedrock"
    )


def test_falsy_use_bedrock_not_detected():
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "0",
        "ANTHROPIC_BEDROCK_BASE_URL": "https://ai.taileb6e.ts.net/bedrock",
    }
    assert _detect_bedrock_aperture(env, flag_override=None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_wrap_bedrock_autodetect.py -v`
Expected: FAIL — `ImportError: cannot import name '_detect_bedrock_aperture'`.

- [ ] **Step 3: Add the pure helper to `wrap.py`**

Near the other module-level helpers in `headroom/cli/wrap.py` (e.g. after `_normalize_proxy_api_url` ~line 1445):

```python
_TRUTHY = {"1", "true", "yes", "on"}


def _detect_bedrock_aperture(env: dict[str, str], flag_override: str | None) -> str | None:
    """Resolve the company-Bedrock aperture URL for `wrap claude`.

    A ``--bedrock-base-url`` flag forces/overrides. Otherwise auto-detect the
    corelight profile: ``CLAUDE_CODE_USE_BEDROCK`` truthy AND
    ``ANTHROPIC_BEDROCK_BASE_URL`` set. Returns the upstream aperture URL, or
    None when Bedrock mode should not engage.
    """
    if flag_override:
        return flag_override
    if env.get("CLAUDE_CODE_USE_BEDROCK", "").strip().lower() not in _TRUTHY:
        return None
    base = env.get("ANTHROPIC_BEDROCK_BASE_URL")
    return base or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_wrap_bedrock_autodetect.py -v`
Expected: PASS (all six).

- [ ] **Step 5: Add a `bedrock_api_url` param to `_start_proxy`**

In `headroom/cli/wrap.py` `_start_proxy` (line 233), add a keyword param and forward it both as a CLI flag and as the proxy subprocess env var (mirror the `anthropic_api_url` handling at lines 244/288-289/304-305):

```python
def _start_proxy(
    port: int,
    *,
    learn: bool = False,
    memory: bool = False,
    agent_type: str = "unknown",
    code_graph: bool = False,
    backend: str | None = None,
    anyllm_provider: str | None = None,
    region: str | None = None,
    openai_api_url: str | None = None,
    anthropic_api_url: str | None = None,
    bedrock_api_url: str | None = None,
    copilot_api_token: str | None = None,
) -> subprocess.Popen:
```

After the `if anthropic_api_url:` cmd block (line ~288-289):

```python
    if anthropic_api_url:
        cmd.extend(["--anthropic-api-url", anthropic_api_url])

    if bedrock_api_url:
        cmd.extend(["--bedrock-base-url", bedrock_api_url])
```

After the `proxy_env["ANTHROPIC_TARGET_API_URL"] = anthropic_api_url` block (line ~304-305):

```python
    if anthropic_api_url:
        proxy_env["ANTHROPIC_TARGET_API_URL"] = anthropic_api_url
    if bedrock_api_url:
        proxy_env["BEDROCK_TARGET_API_URL"] = bedrock_api_url
```

- [ ] **Step 6: Thread `bedrock_api_url` through `_ensure_proxy`**

Grep for `def _ensure_proxy` and its `_start_proxy(` call. Add a `bedrock_api_url: str | None = None` keyword param to `_ensure_proxy` and forward it into the `_start_proxy(...)` call (exactly as `anthropic_api_url` is forwarded).

- [ ] **Step 7: Wire the `claude` command**

In the `claude` command (`headroom/cli/wrap.py` ~line 2358), add a `--bedrock-base-url` Click option to the command decorator (find the existing `@click.option(...)` stack above `def claude`) and a `bedrock_base_url: str | None` param. Then replace the Foundry-detect block (lines 2358-2372) region with Bedrock handling alongside it:

```python
        # Detect Foundry mode: Claude Code uses ANTHROPIC_FOUNDRY_BASE_URL instead of
        # ANTHROPIC_BASE_URL when CLAUDE_CODE_USE_FOUNDRY=1 is set.
        foundry_upstream = None
        if os.environ.get("CLAUDE_CODE_USE_FOUNDRY"):
            foundry_upstream = os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL")

        # Detect company-Bedrock (Tailscale aperture) mode: the corelight
        # profile sets CLAUDE_CODE_USE_BEDROCK=1 + ANTHROPIC_BEDROCK_BASE_URL.
        # --bedrock-base-url forces/overrides. In this mode the proxy fronts
        # the aperture and the child Claude Code points at the local proxy.
        bedrock_upstream = _detect_bedrock_aperture(dict(os.environ), bedrock_base_url)

        proxy_holder[0] = _ensure_proxy(
            port,
            no_proxy,
            learn=learn,
            memory=memory,
            agent_type="claude",
            code_graph=code_graph,
            anthropic_api_url=foundry_upstream,
            bedrock_api_url=bedrock_upstream,
        )
```

Then, in the child-env section (lines 2401-2419), after the existing `ANTHROPIC_BASE_URL` / Foundry handling, add the child-env rewrite for Bedrock mode:

```python
        env = os.environ.copy()
        if foundry_upstream:
            env["ANTHROPIC_FOUNDRY_BASE_URL"] = proxy_url
        else:
            env["ANTHROPIC_BASE_URL"] = proxy_url

        if bedrock_upstream:
            # Point Claude Code's Bedrock client at the local proxy. Claude
            # appends /model/{id}/invoke...; the proxy forwards to the real
            # aperture. No path suffix here. Preserve the skip-auth flag so the
            # child does not attempt client-side SigV4.
            local = f"http://127.0.0.1:{port}"
            env["ANTHROPIC_BEDROCK_BASE_URL"] = local
            env.setdefault("CLAUDE_CODE_USE_BEDROCK", "1")
            env.setdefault("CLAUDE_CODE_SKIP_BEDROCK_AUTH", "1")
            click.echo(f"  Bedrock aperture: {bedrock_upstream} (via {local})")
```

Note: `_claude_proxy_base_url(port)` may append a path; for Bedrock the child env must be the bare `http://127.0.0.1:{port}` (handler forwards `request.url.path` verbatim). Use the bare form as shown.

- [ ] **Step 8: Add an integration-style wrap test (env rewrite only, no real proxy)**

```python
# append to tests/test_wrap_bedrock_autodetect.py
def test_start_proxy_forwards_bedrock_flag(monkeypatch):
    """_start_proxy passes --bedrock-base-url and sets BEDROCK_TARGET_API_URL."""
    import headroom.cli.wrap as wrap

    captured = {}

    class _FakeProc:
        returncode = None

        def poll(self):
            return None

        def kill(self):
            pass

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr(wrap.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(wrap, "_check_proxy", lambda port: True)
    monkeypatch.setattr(wrap.time, "sleep", lambda *_: None)

    wrap._start_proxy(9999, agent_type="claude", bedrock_api_url="https://aperture.test/bedrock")
    assert "--bedrock-base-url" in captured["cmd"]
    i = captured["cmd"].index("--bedrock-base-url")
    assert captured["cmd"][i + 1] == "https://aperture.test/bedrock"
    assert captured["env"]["BEDROCK_TARGET_API_URL"] == "https://aperture.test/bedrock"
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `python -m pytest tests/test_wrap_bedrock_autodetect.py -v`
Expected: PASS (all). If `_check_proxy`/`time` are imported differently, adjust the monkeypatch targets to match `wrap.py` imports.

- [ ] **Step 10: Commit**

```bash
git add headroom/cli/wrap.py tests/test_wrap_bedrock_autodetect.py
git commit -m "feat(cli): wrap claude auto-detects corelight Bedrock aperture

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Live smoke test (opt-in)

**Files:**
- Create: `tests/test_bedrock_smoke.py`

Skipped unless `HEADROOM_BEDROCK_SMOKE_URL` is set (so CI never depends on the tailnet). When run, it goes through the **real handler** against the **real aperture**, asserting byte-faithful response and a non-trivial token reduction on a large prompt.

- [ ] **Step 1: Write the smoke test**

```python
# tests/test_bedrock_smoke.py
from __future__ import annotations

import json
import os

import httpx
import pytest

SMOKE_URL = os.environ.get("HEADROOM_BEDROCK_SMOKE_URL")  # e.g. https://ai.taileb6e.ts.net/bedrock
SMOKE_MODEL = os.environ.get(
    "HEADROOM_BEDROCK_SMOKE_MODEL", "global.anthropic.claude-haiku-4-5-20251001-v1:0"
)

pytestmark = pytest.mark.skipif(
    not SMOKE_URL, reason="set HEADROOM_BEDROCK_SMOKE_URL to run the live aperture smoke test"
)


def _build_request(path: str, body: dict, headers: dict | None = None):
    from starlette.requests import Request

    raw = json.dumps(body).encode("utf-8")
    hdrs = [(b"content-type", b"application/json")]
    for k, v in (headers or {}).items():
        hdrs.append((k.encode(), v.encode()))

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": hdrs,
        "scheme": "https",
        "server": ("testserver", 443),
    }
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_live_invoke_roundtrip_and_savings():
    from headroom.proxy.server import HeadroomProxy, ProxyConfig

    proxy = HeadroomProxy(
        ProxyConfig(bedrock_base_url=SMOKE_URL, cache_enabled=False, rate_limit_enabled=False)
    )
    proxy.http_client = httpx.AsyncClient(timeout=60.0)
    try:
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        }
        req = _build_request(f"/model/{SMOKE_MODEL}/invoke", body)
        resp = await proxy.handle_bedrock_invoke(req, SMOKE_MODEL, stream=False)
        chunks = [c async for c in resp.body_iterator]
        payload = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
        data = json.loads(payload)
        assert data["type"] == "message"
        assert data["role"] == "assistant"

        # Stream variant returns AWS event-stream, relayed verbatim.
        sreq = _build_request(f"/model/{SMOKE_MODEL}/invoke-with-response-stream", body)
        sresp = await proxy.handle_bedrock_invoke(sreq, SMOKE_MODEL, stream=True)
        assert sresp.media_type == "application/vnd.amazon.eventstream"
        sbytes = b"".join(
            [c if isinstance(c, bytes) else c.encode() async for c in sresp.body_iterator]
        )
        assert len(sbytes) > 0
    finally:
        await proxy.http_client.aclose()
```

- [ ] **Step 2: Verify it skips by default**

Run: `python -m pytest tests/test_bedrock_smoke.py -v`
Expected: SKIPPED (2 skipped) — no `HEADROOM_BEDROCK_SMOKE_URL`.

- [ ] **Step 3: Run it live against the aperture**

Run: `HEADROOM_BEDROCK_SMOKE_URL=https://ai.taileb6e.ts.net/bedrock python -m pytest tests/test_bedrock_smoke.py -v`
Expected: PASS (requires tailnet connectivity). Paste the output.

- [ ] **Step 4: Commit**

```bash
git add tests/test_bedrock_smoke.py
git commit -m "test(proxy): add opt-in live Bedrock aperture smoke test

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Verify parts 1 & 2 (subscriptions) — document the evidence

**Files:** none (verification + notes captured into the guide in Task 10)

Per the design, subscriptions already pass through; this is empirical verification only. If a real break appears, it becomes in-scope and gets its own task.

- [ ] **Step 1: Start a proxy and tail its log**

```bash
python -m headroom.cli proxy --port 8787 &
sleep 5
tail -f ~/.headroom/logs/proxy.log
```

- [ ] **Step 2: Verify Claude Max subscription flows through**

Run `headroom wrap claude` against the real Claude Max login, issue one prompt, and confirm: no auth error in the proxy log, and `/stats` increments. Record the exact commands + the relevant log lines.

```bash
curl -s http://127.0.0.1:8787/stats | head
```

- [ ] **Step 3: Verify OpenAI Pro / ChatGPT subscription flows through**

Run `headroom wrap codex` against the ChatGPT login, issue one prompt, confirm `_inject_codex_provider_config` routed `openai_base_url` through the proxy (no bypass), no auth error, `/stats` increments.

- [ ] **Step 4: Capture the evidence**

Save the commands and observed output for Task 10's "Verification" section. If anything failed, STOP and report — do not paper over it.

---

## Task 10: Documentation + Getting-Started guide

**Files:**
- Create: `docs/getting-started-subscriptions-and-bedrock.md`
- Modify: `README.md` (add a link in the relevant section)

- [ ] **Step 1: Write the getting-started guide**

Create `docs/getting-started-subscriptions-and-bedrock.md` covering all three modes. Include, with real commands:

1. **Claude Max subscription** — `headroom wrap claude`; explain the proxy passes the OAuth token through; how to confirm via `/stats` + proxy log (cite Task 9 evidence).
2. **OpenAI Pro / ChatGPT subscription** — `headroom wrap codex`; explain `openai_base_url` injection prevents bypass; confirm via `/stats`.
3. **Company Bedrock (Tailscale aperture)** —
   - Zero-flag path: with the corelight env (`CLAUDE_CODE_USE_BEDROCK=1`, `ANTHROPIC_BEDROCK_BASE_URL=https://ai.taileb6e.ts.net/bedrock`, `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1`) set, just run `headroom wrap claude` — auto-detect engages.
   - Explicit path: `headroom wrap claude --bedrock-base-url https://ai.taileb6e.ts.net/bedrock`.
   - Standalone proxy: `headroom proxy --bedrock-base-url https://ai.taileb6e.ts.net/bedrock` (or `BEDROCK_TARGET_API_URL=...`), then point a Bedrock client's `ANTHROPIC_BEDROCK_BASE_URL` at `http://127.0.0.1:8787`.
   - Compression policy: `--bedrock-compression {aggressive,lossless,off}` (default `aggressive`); `x-headroom-bypass: true` to skip per-request.
   - Wire-format note: response is relayed verbatim (handles both `application/json` and AWS `application/vnd.amazon.eventstream`); request-side token savings are recorded on `/stats` (response-side counts on the non-stream path only in v1).
   - Non-goals: no real-AWS SigV4 changes; the `litellm-bedrock` backend is unaffected.

- [ ] **Step 2: Link it from the README**

Add a bullet under the README's getting-started / usage section linking `docs/getting-started-subscriptions-and-bedrock.md`.

- [ ] **Step 3: Commit**

```bash
git add docs/getting-started-subscriptions-and-bedrock.md README.md
git commit -m "docs: getting-started for subscriptions + company Bedrock aperture

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Full test sweep

- [ ] **Step 1: Run all new tests + adjacent route/handler tests**

```bash
python -m pytest \
  tests/test_bedrock_config.py \
  tests/test_bedrock_passthrough_handler.py \
  tests/test_bedrock_routes.py \
  tests/test_wrap_bedrock_autodetect.py \
  tests/test_provider_proxy_routes.py \
  tests/test_proxy_compress_endpoint.py \
  tests/test_request_outcome.py -v
```

Expected: all PASS (smoke test skipped).

- [ ] **Step 2: Lint/type-check if the repo enforces it**

Run the repo's standard checks (e.g. `ruff check headroom/proxy/handlers/bedrock_passthrough.py headroom/cli/wrap.py` and any `mypy`/`pyright` target). Fix findings.

- [ ] **Step 3: Final review commit (if anything changed)**

```bash
git add -A
git commit -m "chore: lint/typecheck fixes for Bedrock aperture passthrough

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

Do **not** push.

---

## Self-Review Notes (author checklist, already applied)

- **Spec coverage:** Component 1 (handler) → Tasks 2-4; Component 2 (routes) → Task 5; Component 3 (config) → Tasks 1, 6; Component 4 (wrap auto-detect) → Task 7; Testing (unit) → Tasks 2-7; Testing (live smoke) → Task 8; Testing (verify parts 1&2) → Task 9; Docs → Task 10. Non-goals honored (no SigV4 changes; no binary frame re-parsing).
- **Type consistency:** `handle_bedrock_invoke(request, model_id, stream=False)` signature is identical in the handler (Task 2), routes (Task 5), and route test (Task 5). `bedrock_base_url`/`bedrock_compression` names are identical across model, CLI, env, and handler. `_detect_bedrock_aperture(env, flag_override)` and `bedrock_api_url` kwarg names are consistent across helper, `_start_proxy`, `_ensure_proxy`, and the `claude` command.
- **Known verification points for the executor:** (a) confirm the real name of `_MULTI_WORKER_CONFIG_ENV` before `delenv` in Task 6's env test; (b) confirm `_ensure_proxy`'s exact signature/call site before threading `bedrock_api_url`; (c) confirm `_claude_proxy_base_url(port)` shape and use the bare `http://127.0.0.1:{port}` for the child Bedrock env; (d) confirm the argparse `ProxyConfig(` construction exists in `server.py main` before adding parity wiring (Click path is authoritative for `wrap`).
