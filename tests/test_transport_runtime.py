from __future__ import annotations

import asyncio
import io
import json
import socket
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from headroom import paths
from headroom.proxy.outcome import RequestOutcome
from headroom.telemetry.toin import reset_toin
from headroom.transport.protocol import AcquireRequest, ReceiptWriter
from headroom.transport.runtime import TransportLease, create_transport_app, serve_transport
from tests.test_transport_protocol import _acquire_payload


@pytest.fixture(autouse=True)
def _restore_stateless_globals(monkeypatch):
    """In-process transport tests must not disable later tests' persistence."""
    monkeypatch.setattr(paths, "_PROCESS_STATELESS", paths._PROCESS_STATELESS)
    yield
    reset_toin()


def test_transport_lease_release_is_idempotent_and_bound() -> None:
    lease = TransportLease("lease-01")

    assert lease.release("lease-01") is True
    assert lease.release("lease-01") is False
    assert lease.released is True

    other = TransportLease("lease-02")
    with pytest.raises(ValueError, match="lease identity mismatch"):
        other.release("wrong-lease")
    assert other.released is False


def test_transport_app_is_stateless_and_has_no_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path / "workspace"))
    request = AcquireRequest.from_dict(_acquire_payload())
    writer = ReceiptWriter(request, lambda record: None)

    app = create_transport_app(request, writer, port=49111)
    config = app.state.proxy.config

    assert config.host == "127.0.0.1"
    assert config.port == 49111
    assert config.stateless is True
    assert config.lossless is True
    assert config.compression_required is True
    assert config.cache_enabled is False
    assert config.fallback_enabled is False
    assert config.log_full_messages is False
    assert not (tmp_path / "workspace").exists()


def test_transport_app_rejects_caller_bypass_without_reading_body() -> None:
    request = AcquireRequest.from_dict(_acquire_payload())
    records: list[dict] = []
    writer = ReceiptWriter(request, records.append, receipt_id_factory=lambda: "receipt-blocked")
    app = create_transport_app(request, writer, port=49112)

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            content=b"this-is-not-even-json",
            headers={
                "authorization": "Bearer fake-oauth-token",
                "x-headroom-bypass": "true",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "transport_policy_error"
    assert records[0]["upstream_reached"] is False
    assert records[0]["bypass_reason"] == "caller_bypass_forbidden"
    assert "fake-oauth-token" not in json.dumps(records)


@pytest.mark.parametrize("header", ["x-headroom-run-id", "x-headroom-binding-digest", "x-client"])
def test_transport_app_rejects_caller_controlled_attribution(header: str) -> None:
    request = AcquireRequest.from_dict(_acquire_payload())
    records: list[dict] = []
    app = create_transport_app(
        request,
        ReceiptWriter(request, records.append),
        port=49113,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={"model": "claude-test", "messages": []},
            headers={"authorization": "Bearer fake-oauth-token", header: "forged"},
        )

    assert response.status_code == 400
    assert records[0]["bypass_reason"] == "caller_attribution_forbidden"


def test_transport_app_rejects_api_key_for_oauth_mode() -> None:
    request = AcquireRequest.from_dict(_acquire_payload())
    records: list[dict] = []
    app = create_transport_app(
        request,
        ReceiptWriter(request, records.append),
        port=49114,
    )

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={"model": "claude-test", "messages": []},
            headers={"x-api-key": "wrong-account-mode"},
        )

    assert response.status_code == 403
    assert records[0]["error_reason"] == "oauth_bearer_required"
    assert records[0]["upstream_reached"] is False


@pytest.mark.asyncio
async def test_proxy_outcome_funnel_emits_bound_receipt() -> None:
    request = AcquireRequest.from_dict(_acquire_payload())
    records: list[dict] = []
    app = create_transport_app(
        request,
        ReceiptWriter(request, records.append, receipt_id_factory=lambda: "receipt-01"),
        port=49115,
    )

    async def no_op_emit(handler, outcome):
        return None

    app.state.proxy._transport_test_emit = no_op_emit
    outcome = RequestOutcome(
        request_id="request-01",
        provider="anthropic",
        model="claude-test",
        original_tokens=30,
        optimized_tokens=20,
        output_tokens=5,
        tokens_saved=10,
        attempted_input_tokens=30,
        transforms_applied=("lossless_search",),
    )

    await app.state.proxy._record_transport_receipt(outcome)

    assert records[0]["binding_digest"] == request.binding_digest
    assert records[0]["saved_tokens"] == 10


@pytest.mark.asyncio
async def test_required_compression_failure_is_not_silently_forwarded() -> None:
    request = AcquireRequest.from_dict(_acquire_payload())
    records: list[dict] = []
    writer = ReceiptWriter(request, records.append)
    app = create_transport_app(request, writer, port=49116)
    proxy = app.state.proxy
    proxy._run_compression_in_executor = AsyncMock(side_effect=RuntimeError("compressor failed"))
    proxy._retry_request = AsyncMock(side_effect=AssertionError("upstream must not be reached"))

    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-test",
                "max_tokens": 32,
                "stream": False,
                "messages": [
                    {"role": "user", "content": "inspect the tool result"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-01",
                                "name": "Bash",
                                "input": {"command": "example"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-01",
                                "content": "same reducible output line\n" * 1000,
                            }
                        ],
                    },
                ],
            },
            headers={
                "authorization": "Bearer fake-oauth-token",
                "user-agent": "claude-cli/test",
                "anthropic-version": "2023-06-01",
            },
        )

    assert response.status_code == 503
    assert response.json()["error"]["message"] == "compression_error"
    proxy._retry_request.assert_not_awaited()
    assert records[0]["error_reason"] == "compression_error"
    assert records[0]["upstream_reached"] is False


@pytest.mark.asyncio
async def test_startup_failure_emits_exactly_one_error_record(monkeypatch) -> None:
    parent, child = socket.socketpair()
    readiness = io.StringIO()
    receipts = io.StringIO()
    parent.sendall((json.dumps(_acquire_payload()) + "\n").encode("utf-8"))
    monkeypatch.setattr(
        "headroom.transport.runtime._open_listener",
        lambda: (_ for _ in ()).throw(OSError("bind failed")),
    )
    try:
        exit_code = await serve_transport(child, readiness, receipts)
    finally:
        parent.close()
        child.close()

    assert exit_code == 4
    records = [json.loads(line) for line in readiness.getvalue().splitlines()]
    assert records == [
        {
            "schema": "headroom.transport.ready.v1",
            "status": "error",
            "error": {
                "category": "startup_failed",
                "message": "isolated transport failed to start",
            },
        }
    ]


@pytest.mark.asyncio
async def test_receipt_finalization_survives_request_task_cancellation(monkeypatch) -> None:
    request = AcquireRequest.from_dict(_acquire_payload())
    entered = asyncio.Event()
    release = asyncio.Event()
    records: list[dict] = []

    async def delayed_sink(record: dict) -> None:
        entered.set()
        await release.wait()
        records.append(record)

    async def no_op_outcome(handler, outcome) -> None:
        return None

    monkeypatch.setattr("headroom.proxy.outcome.emit_request_outcome", no_op_outcome)
    app = create_transport_app(
        request,
        ReceiptWriter(request, delayed_sink, receipt_id_factory=lambda: "receipt-cancel"),
        port=49117,
    )
    outcome = RequestOutcome(
        request_id="request-cancel",
        provider="anthropic",
        model="claude-test",
        original_tokens=30,
        optimized_tokens=20,
        output_tokens=5,
        tokens_saved=10,
        attempted_input_tokens=30,
        transforms_applied=("lossless_search",),
    )

    task = asyncio.create_task(app.state.proxy._record_request_outcome(outcome))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    for _ in range(100):
        if records:
            break
        await asyncio.sleep(0.01)

    assert records[0]["request_id"] == "request-cancel"
    assert records[0]["outcome"] == "succeeded"
