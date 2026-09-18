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
        resp = await proxy.handle_bedrock_passthrough(req, SMOKE_MODEL, stream=False)
        chunks = [c async for c in resp.body_iterator]
        payload = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
        data = json.loads(payload)
        assert data["type"] == "message"
        assert data["role"] == "assistant"

        sreq = _build_request(f"/model/{SMOKE_MODEL}/invoke-with-response-stream", body)
        sresp = await proxy.handle_bedrock_passthrough(sreq, SMOKE_MODEL, stream=True)
        assert sresp.media_type == "application/vnd.amazon.eventstream"
        sbytes = b"".join(
            [c if isinstance(c, bytes) else c.encode() async for c in sresp.body_iterator]
        )
        assert len(sbytes) > 0
    finally:
        await proxy.http_client.aclose()
