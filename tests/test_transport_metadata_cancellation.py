"""Real forwarder/ASGI/receipt seam; no external requests or model execution."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import ClientDisconnect, Request

from headroom.transport.forwarder import forward
from headroom.transport.protocol import AcquireRequest, ReceiptWriter
from tests.test_transport_modes import mode_payload


class PendingBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"data":['
        await asyncio.Event().wait()


async def make_response(
    client, records, *, mode="openai_oauth_passthrough", path="/models", body=None
):
    binding = AcquireRequest.from_dict(mode_payload(mode))
    scope = {
        "type": "http",
        "method": "POST" if body is not None else "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
    }
    proxy = SimpleNamespace(
        _next_request_id=AsyncMock(return_value="synthetic-request"),
        http_client=client,
        openai_pipeline=None,
    )

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    return await forward(
        binding, ReceiptWriter(binding, records.append), proxy, Request(scope, receive)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["openai_oauth_passthrough", "openai_aperture_passthrough"])
@pytest.mark.parametrize("path", ["/models", "/v1/models"])
@pytest.mark.parametrize("disconnect", ["receive", "send"])
@pytest.mark.parametrize("upstream_status", [200, 499, 500])
async def test_only_observed_model_list_disconnect_after_success_gets_marker(
    mode, path, disconnect, upstream_status
):
    records = []
    transport = httpx.MockTransport(lambda _: httpx.Response(upstream_status, stream=PendingBody()))
    async with httpx.AsyncClient(transport=transport) as client:
        response = await make_response(client, records, mode=mode, path=path)
        sent_body = asyncio.Event()

        async def receive():
            await sent_body.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                sent_body.set()
                if disconnect == "send":
                    raise OSError("synthetic downstream disconnect")

        scope = {"type": "http", "asgi": {"spec_version": "2.4" if disconnect == "send" else "2.3"}}
        try:
            await asyncio.wait_for(response(scope, receive, send), timeout=2)
        except ClientDisconnect:
            assert disconnect == "send"
        await response.finalize()  # Must remain exactly-once.

    assert len(records) == 1
    receipt = records[0]
    expected = "metadata_client_cancelled" if upstream_status == 200 else "upstream_status_499"
    assert receipt["error_reason"] == expected
    assert receipt["request_kind"] == "metadata"
    assert receipt["model"] == "metadata"
    assert receipt["outcome"] == "failed"
    assert receipt["upstream_reached"] is True
    assert receipt["accounting_status"] == "evaluated"
    assert receipt["bypass_reason"] is None
    assert receipt["transforms"] == []
    assert (
        receipt["input_tokens_before"]
        == receipt["input_tokens_after"]
        == receipt["saved_tokens"]
        == 0
    )


@pytest.mark.asyncio
async def test_incomplete_metadata_without_observed_disconnect_stays_strict():
    records = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=PendingBody()))
    ) as client:
        response = await make_response(client, records)
        await response.finalize()
    assert records[0]["error_reason"] == "upstream_status_499"


@pytest.mark.asyncio
async def test_upstream_read_failure_is_not_a_client_cancellation():
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("synthetic upstream error")

    records = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=BrokenBody()))
    ) as client:
        response = await make_response(client, records)
        with pytest.raises(httpx.ReadError):
            async for _ in response.body_iterator:
                pass
    assert records[0]["error_reason"] == "upstream_status_499"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,path,body,kind",
    [
        ("openai_oauth_passthrough", "/responses", {"model": "test", "input": "hello"}, None),
        ("anthropic_oauth_passthrough", "/v1/messages/count_tokens", {"messages": []}, "metadata"),
    ],
)
async def test_other_routes_cannot_emit_optional_cancellation(mode, path, body, kind):
    records = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=PendingBody()))
    ) as client:
        response = await make_response(client, records, mode=mode, path=path, body=body)
        sent_body = asyncio.Event()

        async def receive():
            await sent_body.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                sent_body.set()

        await asyncio.wait_for(response({"type": "http"}, receive, send), timeout=2)
        await response.finalize()
    assert len(records) == 1
    assert records[0].get("request_kind") == kind
    assert records[0]["outcome"] == "failed"
    assert records[0]["error_reason"] == "upstream_status_499"


@pytest.mark.asyncio
async def test_shutdown_task_cancellation_is_not_optional_metadata():
    records = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=PendingBody()))
    ) as client:
        response = await make_response(client, records)
        sent_body = asyncio.Event()

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.body":
                sent_body.set()

        task = asyncio.create_task(response({"type": "http"}, receive, send))
        await asyncio.wait_for(sent_body.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await response.finalize()
    assert len(records) == 1
    assert records[0]["error_reason"] == "upstream_status_499"


@pytest.mark.asyncio
async def test_upstream_read_error_remains_strict_when_disconnect_is_also_observed():
    class BrokenBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("synthetic upstream failure")

    records = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=BrokenBody()))
    ) as client:
        response = await make_response(client, records)
        # Record the real ASGI disconnect boundary while allowing the upstream
        # iterator to raise before finalization, fixing the event ordering.
        response.on_disconnect()
        with pytest.raises(httpx.ReadError):
            async for _ in response.body_iterator:
                pass
        await response.finalize()
    assert len(records) == 1
    assert records[0]["error_reason"] == "upstream_status_499"
