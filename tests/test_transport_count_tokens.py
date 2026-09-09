import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from headroom.transport.forwarder import forward
from headroom.transport.protocol import AcquireRequest, ReceiptWriter
from tests.test_transport_modes import RawChunks, mode_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,path",
    [
        ("anthropic_oauth_passthrough", "/v1/messages/count_tokens"),
        ("bedrock_aperture_passthrough", "/model/fixture/count-tokens"),
    ],
)
async def test_count_tokens_preserves_body_and_is_metadata(mode, path):
    binding = AcquireRequest.from_dict(mode_payload(mode))
    body = b'{"model":"fixture","messages":[{"role":"user","content":"fixture"}]}'
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, stream=RawChunks())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        proxy = SimpleNamespace(
            http_client=client,
            openai_pipeline=None,
            _next_request_id=AsyncMock(return_value="metadata-count"),
        )
        records = []
        receive = AsyncMock(return_value={"type": "http.request", "body": body, "more_body": False})
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "scheme": "http",
                "path": path,
                "query_string": b"beta=true",
                "headers": [(b"content-type", b"application/json")],
                "server": ("127.0.0.1", 80),
            },
            receive,
        )
        response = await forward(binding, ReceiptWriter(binding, records.append), proxy, request)
        async for _ in response.body_iterator:
            pass
    assert seen[0].content == body
    assert seen[0].url.params["beta"] == "true"
    assert records[0]["request_kind"] == "metadata"
    assert records[0]["model"] == "metadata"
    assert records[0]["saved_tokens"] == 0
    assert "messages" not in json.dumps(records)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/inference-profiles", "/inference-profiles/global.fixture"])
async def test_optional_bedrock_discovery_preserves_404_evidence(path):
    binding = AcquireRequest.from_dict(mode_payload("bedrock_aperture_passthrough"))
    seen, records = [], []

    def respond(request):
        seen.append(request)
        return httpx.Response(404, stream=RawChunks())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        proxy = SimpleNamespace(
            http_client=client,
            openai_pipeline=None,
            _next_request_id=AsyncMock(return_value="discovery"),
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "path": path,
                "query_string": b"maxResults=10",
                "headers": [],
                "server": ("127.0.0.1", 80),
            }
        )
        response = await forward(binding, ReceiptWriter(binding, records.append), proxy, request)
        async for _ in response.body_iterator:
            pass
    assert response.status_code == 404
    assert seen[0].url.path.endswith(path)
    assert seen[0].url.params["maxResults"] == "10"
    assert seen[0].content == b""
    assert records[0]["request_kind"] == "metadata"
    assert records[0]["outcome"] == "failed"
    assert records[0]["error_reason"] == "upstream_status_404"
    assert records[0]["saved_tokens"] == 0
