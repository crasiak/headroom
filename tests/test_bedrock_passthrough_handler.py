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
        big = "BANANA " * 4000
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16,
            "messages": [
                {"role": "user", "content": "summarize"},
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": big}
                    ],
                },
            ],
        }
        req = _make_request(f"/model/{MODEL}/invoke", body)
        resp = await proxy.handle_bedrock_invoke(req, MODEL, stream=False)

        chunks = [c async for c in resp.body_iterator]
        payload = b"".join(
            c if isinstance(c, bytes) else c.encode() for c in chunks
        )
        assert json.loads(payload)["role"] == "assistant"
        assert resp.status_code == 200

        assert captured["url"] == f"{APERTURE}/model/{MODEL}/invoke"
        assert captured["auth"] is None
        assert captured["body"]["anthropic_version"] == "bedrock-2023-05-31"
        assert "model" not in captured["body"]
        assert isinstance(captured["body"]["messages"], list)
    finally:
        await proxy.http_client.aclose()
