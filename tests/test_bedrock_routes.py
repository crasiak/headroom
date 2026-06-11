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
    body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]}

    r1 = client.post(f"/model/{model}/invoke", json=body)
    r2 = client.post(f"/model/{model}/invoke-with-response-stream", json=body)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert (f"/model/{model}/invoke", model, False) in calls
    assert (f"/model/{model}/invoke-with-response-stream", model, True) in calls


def test_bedrock_route_501_when_unconfigured():
    app = create_app(
        ProxyConfig(optimize=False, cache_enabled=False, rate_limit_enabled=False)
    )
    client = TestClient(app)
    body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]}
    r = client.post("/model/global.anthropic.claude-sonnet-4-6/invoke", json=body)
    assert r.status_code == 501
    assert r.json()["error"]["type"] == "not_configured"
