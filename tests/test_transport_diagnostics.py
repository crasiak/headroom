from __future__ import annotations

import asyncio
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from headroom.transport.protocol import ProcessIdentity


@pytest.mark.parametrize(
    "failure, reason",
    [
        (subprocess.TimeoutExpired("ps", 2), "timeout"),
        (PermissionError("SECRET"), "permission_denied"),
        (OSError("SECRET"), "os_error"),
        (ValueError("SECRET"), "parse_error"),
    ],
)
def test_typed_reader_failure_is_unavailable(monkeypatch, failure, reason):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr("headroom.transport.protocol.subprocess.run", fail)
    observed = ProcessIdentity(os.getpid(), "ps", 1).observe_diagnostics()
    assert observed.state == "unavailable"
    assert observed.reason == reason
    assert observed.reader_error == reason
    assert observed.read_elapsed_ms >= 0
    assert "SECRET" not in repr(observed)


def test_missing_pid_retains_reader_error(monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("ps", 2)

    def missing(*args):
        raise ProcessLookupError()

    monkeypatch.setattr("headroom.transport.protocol.subprocess.run", fail)
    monkeypatch.setattr("headroom.transport.protocol.os.kill", missing)
    observed = ProcessIdentity(999999, "ps", 1).observe_diagnostics()
    assert (observed.state, observed.reason, observed.reader_error) == (
        "dead",
        "missing_pid",
        "timeout",
    )


def test_tracker_bounds_privacy_and_finalization():
    from headroom.transport.diagnostics import Diagnostics

    events = []
    diagnostics = Diagnostics(
        run_id="run-test", lease_id="lease-test", sink=events.append, capacity=2
    )
    records = [diagnostics.begin("openai_responses") for _ in range(5)]
    assert diagnostics.snapshot()["active_requests"] == 5
    assert len(diagnostics.active) == 2
    assert diagnostics.snapshot()["overflow_count"] == 3
    record = records[0]
    record.stage("awaiting_headers")
    record.stage("streaming", status=503)
    record.finish("failed")
    record.finish("completed")
    for other in records[1:]:
        other.finish("cancelled")
    assert diagnostics.snapshot()["active_requests"] == 0
    assert not diagnostics.active
    terminal = next(e for e in events if e.get("stage") == "failed")
    assert terminal["previous_stage"] == "streaming"
    assert terminal["status"] == 503
    assert terminal["elapsed_ms"] >= 0
    assert all("body" not in e and "headers" not in e for e in events)


@pytest.mark.asyncio
async def test_httpx_trace_uses_context_only_and_allowlists_values():
    import httpx

    from headroom.transport.diagnostics import Diagnostics, current_request, trace_request

    events = []
    d = Diagnostics(run_id="r", lease_id="l", sink=events.append)
    record = d.begin("anthropic_messages")
    token = current_request.set(record)
    request = httpx.Request("POST", "https://secret.invalid/?credential=SECRET", content=b"SECRET")
    try:
        await trace_request(request)
        await request.extensions["trace"]("connection.connect_tcp.started", {"host": "SECRET"})
        await request.extensions["trace"](
            "http11.receive_response_headers.started", {"headers": "SECRET"}
        )
        await request.extensions["trace"](
            "http11.receive_response_headers.complete",
            {"return_value": (b"HTTP/1.1", 503, b"SECRET", [])},
        )
    finally:
        current_request.reset(token)
        record.finish("failed")
    assert [e.get("stage") for e in events] == [
        "accepted",
        "upstream_connect",
        "awaiting_headers",
        "streaming",
        "failed",
    ]
    assert "SECRET" not in json.dumps(events)


@pytest.mark.asyncio
async def test_lag_sampler_detects_blockage_and_tears_down():
    import time

    from headroom.transport.diagnostics import Diagnostics

    d = Diagnostics(run_id="r", lease_id="l", sink=lambda e: None)
    task = asyncio.create_task(d.sample_loop(interval=0.005))
    await asyncio.sleep(0.01)
    time.sleep(0.03)
    await asyncio.sleep(0.002)
    assert d.snapshot()["loop_lag_max_ms"] >= 20
    await asyncio.sleep(0.015)
    assert d.snapshot()["loop_lag_ms"] < 20
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_episode_thousand_failures_are_bounded_and_reset():
    from headroom.transport.diagnostics import Diagnostics, ParentEpisode
    from headroom.transport.protocol import ProcessObservation

    now, events = [10.0], []
    d = Diagnostics(run_id="r", lease_id="l", sink=events.append, clock=lambda: now[0])
    episode = ParentEpisode(d, 123)
    failed = ProcessObservation("unavailable", "timeout", "ps", 1, None, "timeout", 2)
    live = ProcessObservation("live", "match", "ps", 1, 1, None, 3)
    for _ in range(1000):
        episode.observe(failed)
        now[0] += 0.1
    episode.observe(live)
    assert len(events) == 3
    assert events[-1]["outcome"] == "recovered"
    assert events[-1]["attempt_count"] == 1001
    assert events[-1]["failed_attempt_count"] == 1000
    assert events[-1]["episode_duration_ms"] == pytest.approx(100000)
    assert events[-1]["cumulative_read_elapsed_ms"] == 2003
    episode.observe(failed)
    assert events[-1]["attempt_count"] == 1
    assert events[-1]["episode_id"] != events[-2]["episode_id"]
    episode.report("monitor_stopped")
    assert events[-1]["outcome"] == "monitor_stopped"


def test_boot_is_immutable_and_marks_source_quality(monkeypatch):
    from pathlib import Path

    from headroom.transport.diagnostics import Diagnostics

    d = Diagnostics(run_id="r", sink=lambda e: None)
    identity = ProcessIdentity(123, "ps", 1)
    boot = d.capture_boot(identity)
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"changed on disk SECRET")
    assert d.capture_boot(identity) == boot
    changed = Diagnostics(run_id="r", sink=lambda e: None).capture_boot(identity)
    assert changed["source_fingerprint"] != boot["source_fingerprint"]
    assert changed["loaded_function_fingerprint"] == boot["loaded_function_fingerprint"]
    assert boot["loaded_code_identity_quality"] == "unknown"
    assert boot["source_identity_quality"] == "launch_disk_manifest"
    assert "SECRET" not in json.dumps(boot)
    assert not any(
        key in d.snapshot() for key in ("run_id", "lease_id", "request_id", "source_manifest")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, path",
    [("anthropic_oauth_passthrough", "/v1/messages"), ("openai_oauth_passthrough", "/responses")],
)
@pytest.mark.parametrize("outcome", ["complete", "timeout", "cancel", "disconnect", "status_error"])
async def test_boundary_counters_finally_and_real_stages(mode, path, outcome, monkeypatch):
    import httpx

    from headroom.transport.diagnostics import Diagnostics, current_request
    from headroom.transport.protocol import AcquireRequest, ReceiptWriter
    from headroom.transport.runtime import _TransportBoundaryMiddleware
    from tests.test_transport_modes import mode_payload

    binding = AcquireRequest.from_dict(mode_payload(mode))
    events = []
    d = Diagnostics(run_id=binding.run_id, sink=events.append)

    async def respond(request):
        trace = request.extensions["trace"]
        await trace("http11.receive_response_headers.started", {"SECRET": "BODY_CANARY"})
        if outcome == "timeout":
            raise httpx.ReadTimeout("SECRET_URL_BODY_CANARY")
        await trace(
            "http11.receive_response_headers.complete",
            {"return_value": (b"HTTP/1.1", 503 if outcome == "status_error" else 200, b"", [])},
        )
        return httpx.Response(503 if outcome == "status_error" else 200, content=b"BODY_CANARY")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        proxy = SimpleNamespace(http_client=client)

        async def fake_app(scope, receive, send):
            assert current_request.get() is not None
            if outcome == "cancel":
                raise asyncio.CancelledError()
            response = await client.post("https://fake.invalid", content=b"BODY_CANARY")
            await send(
                {"type": "http.response.start", "status": response.status_code, "headers": []}
            )
            if outcome == "disconnect":
                await receive()
                return
            await send({"type": "http.response.body", "body": response.content})

        boundary = _TransportBoundaryMiddleware(
            fake_app,
            receipt_writer=ReceiptWriter(binding, lambda e: None),
            binding=binding,
            proxy=proxy,
            diagnostics=d,
        )
        # Exercise the common request lifetime boundary independently of routing
        # policy. Real route integration is covered by local HTTP tests below.
        monkeypatch.setattr(boundary, "_dispatch", fake_app)
        scope = {"type": "http", "path": path}

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        try:
            await boundary(scope, receive, send)
        except (httpx.ReadTimeout, asyncio.CancelledError):
            assert outcome in {"timeout", "cancel"}
        assert d.snapshot()["active_requests"] == 0
        assert current_request.get() is None
    terminal = events[-1]
    assert (
        terminal["stage"]
        == {
            "complete": "completed",
            "timeout": "failed",
            "cancel": "cancelled",
            "disconnect": "cancelled",
            "status_error": "failed",
        }[outcome]
    )
    if outcome == "timeout":
        assert terminal["previous_stage"] == "awaiting_headers"
    assert "BODY_CANARY" not in json.dumps(events)
    assert "SECRET_URL" not in json.dumps(events)


@pytest.mark.parametrize(
    "mode, path",
    [("anthropic_oauth_passthrough", "/v1/messages"), ("openai_oauth_passthrough", "/responses")],
)
def test_real_local_proxy_emits_private_boot_request_and_shutdown(tmp_path, mode, path):
    import httpx

    from headroom.transport.protocol import AcquireRequest
    from tests.test_transport_modes import mode_payload
    from tests.test_transport_serve import _FakeAnthropicServer, _read_json_line, _TransportProcess

    transport = _TransportProcess(tmp_path)
    try:
        with _FakeAnthropicServer() as upstream:
            payload = mode_payload(mode)
            payload["upstream"]["url"] = upstream.url
            from headroom.transport.protocol import canonical_digest

            payload["upstream"]["digest"] = canonical_digest(
                {"schema": "headroom.transport.upstream.v1", "url": upstream.url}
            )
            payload["binding_digest"] = AcquireRequest.binding_digest_for(payload)
            transport.send(payload)
            ready = _read_json_line(transport.readiness)
            assert ready["status"] == "ready"
            response = httpx.post(
                ready["endpoint"] + path,
                json={
                    "model": "fake",
                    "max_tokens": 32,
                    "stream": False,
                    "messages": [{"role": "user", "content": "BODY_CANARY"}],
                    "input": "BODY_CANARY",
                },
                headers={"authorization": "Bearer SECRET_CANARY"},
            )
            assert response.status_code == 200
            health = httpx.get(ready["endpoint"] + "/health").json()
            assert health["transport_diagnostics"]["active_requests"] == 0
            assert "run_id" not in health["transport_diagnostics"]
            transport.send(
                {"schema": "headroom.transport.release.v1", "lease_id": ready["lease_id"]}
            )
            assert transport.process.wait(timeout=15) == 0
            output = transport.process.stderr.read()
            events = []
            for line in output.splitlines():
                start = line.find('{"')
                if start >= 0:
                    try:
                        value = json.loads(line[start:])
                    except ValueError:
                        continue
                    if value.get("schema") == "headroom.transport.diagnostic.v1":
                        events.append(value)
            (tmp_path / "diagnostic-events.json").write_text(json.dumps(events, indent=2))
            assert any(e["event"] == "transport_boot" for e in events), output
            stages = [e.get("stage") for e in events if e["event"] == "request_stage"]
            assert stages == [
                "accepted",
                "validating",
                "upstream_connect",
                "awaiting_headers",
                "streaming",
                "completed",
            ]
            assert any(e.get("reason") == "lease_release" for e in events)
            assert all(
                e["run_id"] == payload["run_id"] and e["lease_id"] == ready["lease_id"]
                for e in events
            )
            assert "BODY_CANARY" not in json.dumps(events)
            assert "SECRET_CANARY" not in json.dumps(events)
    finally:
        transport.close()
    assert transport.process.poll() is not None


def test_unsupported_reader_remains_unknown_for_monitor_but_cannot_acquire(monkeypatch):
    monkeypatch.setattr("headroom.transport.protocol.os.kill", lambda *a: None)
    parent = ProcessIdentity(123, "future-reader", 1)
    observed = parent.observe_diagnostics()
    assert (observed.state, observed.reason) == ("unavailable", "unsupported_reader")
    assert parent.observe_live_process() is None
    assert parent.matches_live_process() is False


def test_diagnostic_sink_failure_does_not_change_request_outcome():
    from headroom.transport.diagnostics import Diagnostics

    def fail(event):
        raise OSError("private filesystem information")

    d = Diagnostics(run_id="r", sink=fail)
    record = d.begin("anthropic_messages")
    record.finish("completed")
    assert record.last_stage == "completed"
    assert d.snapshot()["active_requests"] == 0
    assert d.snapshot()["diagnostic_sink_errors"] == 2


def test_repeated_request_transitions_have_a_hard_event_cap():
    from headroom.transport.diagnostics import Diagnostics

    events = []
    d = Diagnostics(run_id="r", sink=events.append)
    record = d.begin("openai_responses")
    for _ in range(1000):
        record.stage("upstream_connect")
        record.stage("awaiting_headers")
    record.finish("failed")
    assert len(events) == 34
    assert events[-1]["stage"] == "failed"
    assert d.snapshot()["active_requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["lease_release", "control_eof", "invalid_control_record"])
async def test_control_shutdown_emits_only_typed_cause(kind):
    import socket

    from headroom.transport.diagnostics import Diagnostics
    from headroom.transport.protocol import ProtocolError
    from headroom.transport.runtime import TransportLease, _ControlChannel, _monitor_control

    events = []
    d = Diagnostics(run_id="r", lease_id="l", sink=events.append)
    control, peer = socket.socketpair()
    server = SimpleNamespace(should_exit=False, force_exit=False)
    try:
        if kind == "lease_release":
            peer.sendall(b'{"schema":"headroom.transport.release.v1","lease_id":"l"}\n')
        elif kind == "control_eof":
            peer.close()
        else:
            peer.sendall(b"SECRET_BODY_CANARY\n")
        try:
            await _monitor_control(_ControlChannel(control), TransportLease("l"), server, d)
        except ProtocolError:
            assert kind == "invalid_control_record"
        assert server.should_exit
        assert events[-1]["reason"] == kind
        assert "SECRET_BODY_CANARY" not in json.dumps(events)
    finally:
        control.close()
        peer.close()
