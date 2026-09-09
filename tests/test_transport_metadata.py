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
@pytest.mark.parametrize("redirect", [False, True])
async def test_model_discovery_is_typed_and_cannot_redirect_outside_binding(redirect):
    binding = AcquireRequest.from_dict(mode_payload("openai_oauth_passthrough"))
    records, requests = [], []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            302 if redirect else 200,
            headers={"location": "https://unbound.example"} if redirect else {},
            stream=RawChunks(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        proxy = SimpleNamespace(
            _next_request_id=AsyncMock(return_value="req-metadata"),
            http_client=client,
            openai_pipeline=None,
        )
        response = await forward(
            binding,
            ReceiptWriter(binding, records.append),
            proxy,
            Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/models",
                    "query_string": b"client_version=test",
                    "headers": [],
                }
            ),
        )
        assert len(requests) == 1
        assert requests[0].method == "GET"
        assert requests[0].url.path == "/models"
        if redirect:
            assert response.status_code == 502
            assert "location" not in response.headers
            assert records[0]["error_reason"] == "upstream_redirect_forbidden"
        else:
            assert records == []
            async for _ in response.body_iterator:
                pass
            assert records[0]["request_kind"] == "metadata"
            assert records[0]["model"] == "metadata"
            assert records[0]["saved_tokens"] == 0
        assert "unbound.example" not in json.dumps(records)
