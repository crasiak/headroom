from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from headroom.transport.forwarder import forward
from headroom.transport.protocol import (
    AcquireRequest,
    ProtocolError,
    ReceiptWriter,
    current_process_identity,
)
from tests.test_transport_protocol import _acquire_payload


def mode_payload(mode: str) -> dict:
    payload = _acquire_payload()
    payload["account"]["provider_mode"] = mode
    if mode.startswith("openai_"):
        payload["harness"] = "codex"
        payload["account"]["credential_locator"] = "file:///tmp/fake-codex-account"
    elif mode == "bedrock_aperture_passthrough":
        payload["account"]["credential_locator"] = "https://aperture.example/bedrock"
    payload["binding_digest"] = AcquireRequest.binding_digest_for(payload)
    return payload


@pytest.mark.parametrize(
    "mode",
    ["openai_oauth_passthrough", "openai_aperture_passthrough", "bedrock_aperture_passthrough"],
)
def test_provider_modes_have_exact_native_environment(mode):
    request = AcquireRequest.from_dict(mode_payload(mode))
    ready = request.ready_record(
        endpoint="http://127.0.0.1:12345",
        process_identity=current_process_identity(),
        lease_id="lease-test",
    )
    if mode.startswith("openai_"):
        assert ready.child_environment.set == {}
        assert "ANTHROPIC_AUTH_TOKEN" in ready.child_environment.clear
    else:
        assert ready.child_environment.set == {
            "ANTHROPIC_BEDROCK_BASE_URL": "http://127.0.0.1:12345",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        }
        assert "ANTHROPIC_AUTH_TOKEN" in ready.child_environment.clear


@pytest.mark.parametrize(
    "mode",
    ["openai_oauth_passthrough", "openai_aperture_passthrough", "bedrock_aperture_passthrough"],
)
def test_provider_mode_cannot_be_used_by_the_wrong_harness(mode):
    payload = mode_payload(mode)
    payload["harness"] = "claude-code" if payload["harness"] == "codex" else "codex"
    payload["binding_digest"] = AcquireRequest.binding_digest_for(payload)
    with pytest.raises(ProtocolError, match="harness"):
        AcquireRequest.from_dict(payload)


class RawChunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'event: message\r\ndata: {"text":"unchanged"}\r\n\r\n'
        yield b"\x00\xffbinary-event-frame"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    ["openai_oauth_passthrough", "openai_aperture_passthrough", "bedrock_aperture_passthrough"],
)
@pytest.mark.parametrize("failure", [False, True, "disconnect", "start-failure"])
async def test_isolated_forwarding_fidelity_and_compressor_failure(mode, failure):
    binding = AcquireRequest.from_dict(mode_payload(mode))
    records, requests = [], []
    writer = ReceiptWriter(binding, records.append)
    if mode.startswith("openai_"):
        path = "/responses"
        body = {
            "model": "fake",
            "input": [{"type": "function_call_output", "call_id": "call-1", "output": "reducible"}],
        }
    else:
        path = "/model/fake/invoke-with-response-stream"
        body = {"messages": [{"role": "user", "content": "reducible"}]}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=RawChunks())

    async def compress(fn, **kwargs):
        if failure is True:
            raise RuntimeError("private details must not enter receipts")
        return SimpleNamespace(
            tokens_before=10, tokens_after=10, messages=fn(), transforms_applied=[]
        )

    pipeline = SimpleNamespace(apply=lambda **kwargs: kwargs["messages"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        proxy = SimpleNamespace(
            _next_request_id=AsyncMock(return_value="req-1"),
            _run_compression_in_executor=compress,
            http_client=client,
            openai_pipeline=pipeline,
            anthropic_pipeline=pipeline,
        )
        response = await forward(
            binding,
            writer,
            proxy,
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "query_string": b"",
                    "headers": [(b"authorization", b"Bearer fake-account")],
                },
                receive,
            ),
        )
        if failure is True:
            assert response.status_code == 503
            assert requests == []
            assert records[0]["error_reason"] == "compression_failed"
        elif failure == "disconnect":
            await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
            await response.finalize()
            assert len(records) == 1
            assert records[0]["outcome"] == "failed"
        elif failure == "start-failure":

            async def fail_send(message):
                raise RuntimeError("client disconnected before response start")

            with pytest.raises(RuntimeError):
                await response(
                    {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, fail_send
                )
            assert len(records) == 1
            assert records[0]["outcome"] == "failed"
        else:
            assert records == [], "a receipt must not claim completion before stream consumption"
            raw = b"".join([chunk async for chunk in response.body_iterator])
            assert (
                raw
                == b'event: message\r\ndata: {"text":"unchanged"}\r\n\r\n\x00\xffbinary-event-frame'
            )
            assert requests[0].content == json.dumps(body).encode()
            assert records[0]["outcome"] == "succeeded"
            assert records[0]["saved_tokens"] == 0
        assert "fake-account" not in json.dumps(records)
        assert "private details" not in json.dumps(records)


@pytest.mark.parametrize(
    "mode",
    ["openai_oauth_passthrough", "openai_aperture_passthrough", "bedrock_aperture_passthrough"],
)
def test_actual_transport_new_modes_compress_against_fake_provider(tmp_path, mode):
    from headroom.transport.protocol import canonical_digest
    from tests.test_transport_serve import _FakeAnthropicServer, _read_json_line, _TransportProcess

    transport = _TransportProcess(tmp_path)
    try:
        with _FakeAnthropicServer() as upstream:
            payload = mode_payload(mode)
            payload["upstream"] = {
                "url": upstream.url,
                "digest": canonical_digest(
                    {"schema": "headroom.transport.upstream.v1", "url": upstream.url}
                ),
            }
            payload["binding_digest"] = AcquireRequest.binding_digest_for(payload)
            transport.send(payload)
            ready = _read_json_line(transport.readiness)
            assert ready["status"] == "ready"
            reducible = "".join(
                f"/src/example.py:{line}: repeated matching value\n" for line in range(1, 400)
            )
            if mode.startswith("openai_"):
                path = "/responses"
                body = {
                    "model": "claude-test",
                    "input": [
                        {"type": "function_call_output", "call_id": "call-1", "output": reducible}
                    ],
                }
            else:
                path = "/model/claude-test/invoke"
                body = {
                    "model": "claude-test",
                    "messages": [
                        {"role": "user", "content": "inspect search results"},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call-1",
                                    "name": "Grep",
                                    "input": {"pattern": "matching value"},
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call-1",
                                    "content": reducible,
                                }
                            ],
                        },
                    ],
                }
            response = httpx.post(
                ready["endpoint"] + path,
                json=body,
                headers={"authorization": "Bearer fake-oauth-account"},
                timeout=30,
            )
            assert response.status_code == 200
            receipt = _read_json_line(transport.receipts)
            assert receipt["outcome"] == "succeeded"
            assert receipt["saved_tokens"] > 0
            assert len(json.dumps(upstream.requests[0]["body"])) < len(json.dumps(body))
            assert response.json()["content"] == [
                {"type": "text", "text": "visible fake-provider output"}
            ]
            transport.send(
                {"schema": "headroom.transport.release.v1", "lease_id": ready["lease_id"]}
            )
            assert transport.process.wait(timeout=15) == 0
            assert list(tmp_path.iterdir()) == []
    finally:
        transport.close()
