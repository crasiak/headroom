import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from headroom.transport.forwarder import forward
from headroom.transport.protocol import AcquireRequest, ReceiptWriter
from tests.test_transport_modes import mode_payload


class CompletedThenOpen(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("chunks", [
    [b'event: response.completed\ndata: {}\n\n'],
    [b'event: response.', b'completed\r\n', b'data: ' + b'x' * 100000, b'\r\n\r\n'],
])
async def test_native_disconnect_after_delivered_completion_is_success(chunks):
    binding = AcquireRequest.from_dict(mode_payload("openai_oauth_passthrough"))
    records = []
    delivered = asyncio.Event()
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/responses",
        "query_string": b"",
        "headers": [],
        "server": ("127.0.0.1", 80),
        "asgi": {"spec_version": "2.3"},
    }
    body = b'{"model":"fixture","input":"hello"}'
    request = Request(
        scope, AsyncMock(return_value={"type": "http.request", "body": body, "more_body": False})
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=CompletedThenOpen(chunks)
            )
        )
    ) as client:
        proxy = SimpleNamespace(
            http_client=client,
            openai_pipeline=None,
            _next_request_id=AsyncMock(return_value="native-completion"),
        )
        response = await forward(binding, ReceiptWriter(binding, records.append), proxy, request)

        async def receive():
            await delivered.wait()
            return {"type": "http.disconnect"}

        sent = 0

        async def send(message):
            nonlocal sent
            if message["type"] == "http.response.body":
                sent += 1
                if sent == len(chunks):
                    delivered.set()

        await asyncio.wait_for(response(scope, receive, send), 2)
    assert len(records) == 1
    assert records[0]["outcome"] == "succeeded"
    assert records[0]["upstream_reached"] is True
