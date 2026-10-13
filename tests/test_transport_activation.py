"""activate.v1: a transport whose acquire requires activation serves nothing
until its supervisor, having validated readiness, activates the lease.

The tests hand the transport a listener the test process also holds. A
connection the transport has not accepted stays in that listener's accept
queue, so the test can accept it itself and read its request bytes intact:
proof that the transport neither accepted nor read it.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from headroom.transport.protocol import (
    ACTIVATE_SCHEMA,
    AcquireRequest,
    ProtocolError,
)
from tests.test_transport_protocol import _acquire_payload
from tests.test_transport_serve import (
    _FakeAnthropicServer,
    _loopback_listener,
    _read_json_line,
    _small_request,
    _TransportProcess,
)

REQUEST = b"HEAD /api/hello HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"


def _activated_payload(upstream_url: str) -> dict:
    payload = _acquire_payload(upstream_url=upstream_url)
    payload["activation"] = "required"
    return payload


def _connect_and_write(port: int) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(REQUEST)
    return client


def _assert_unanswered(client: socket.socket, wait: float = 1.0) -> None:
    client.settimeout(wait)
    with pytest.raises(TimeoutError):
        client.recv(1)


def _take_queued(listener: socket.socket) -> bytes:
    """Accept the next queued connection in the test and return its bytes."""

    listener.settimeout(5)
    accepted, _ = listener.accept()
    with accepted:
        accepted.settimeout(5)
        received = b""
        while len(received) < len(REQUEST):
            chunk = accepted.recv(len(REQUEST) - len(received))
            if not chunk:
                break
            received += chunk
    return received


def _answer(client: socket.socket) -> bytes:
    client.settimeout(15)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = client.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _start(tmp_path, listener, payload) -> tuple[_TransportProcess, dict]:
    transport = _TransportProcess(tmp_path, listener=listener)
    threading.Thread(target=transport.process.stderr.read, daemon=True).start()
    threading.Thread(target=transport.process.stdout.read, daemon=True).start()
    transport.send(payload)
    ready = _read_json_line(transport.readiness)
    assert ready["status"] == "ready", ready
    return transport, ready


def test_acquire_activation_is_optional_strict_and_outside_the_binding() -> None:
    plain = AcquireRequest.from_dict(_acquire_payload())
    assert plain.activation_required is False

    payload = _acquire_payload()
    payload["activation"] = "required"
    gated = AcquireRequest.from_dict(payload)
    assert gated.activation_required is True
    # The binding is the same lease's binding either way.
    assert gated.binding_digest == plain.binding_digest
    assert gated.computed_binding_digest() == plain.computed_binding_digest()
    ready = gated.ready_record(
        endpoint="http://127.0.0.1:1", process_identity=plain.parent_process, lease_id="lease-1"
    ).to_dict()
    assert "activation" not in ready

    for value in ("immediate", True, None, ""):
        payload["activation"] = value
        with pytest.raises(ProtocolError, match="acquire.activation"):
            AcquireRequest.from_dict(payload)


def test_nothing_is_accepted_or_read_before_activate_then_queued_clients_are_served(
    tmp_path,
) -> None:
    listener = _loopback_listener()
    port = listener.getsockname()[1]
    with _FakeAnthropicServer() as upstream:
        transport, ready = _start(tmp_path, listener, _activated_payload(upstream.url))
        try:
            assert ready["endpoint"] == f"http://127.0.0.1:{port}"
            # Ready, not activated: a client's request waits unread in the
            # accept queue, where the test itself can still take it.
            first = _connect_and_write(port)
            _assert_unanswered(first)
            assert _take_queued(listener) == REQUEST
            first.close()

            # A client that connects and writes before activate is served
            # only after it.
            waiting = _connect_and_write(port)
            _assert_unanswered(waiting, 0.5)
            activated_at = time.monotonic()
            transport.send({"schema": ACTIVATE_SCHEMA, "lease_id": ready["lease_id"]})
            answer = _answer(waiting)
            waiting.close()
            assert answer.startswith(b"HTTP/1.1 200"), answer
            assert time.monotonic() >= activated_at

            # Activated: the transport serves the lease as before.
            assert _small_request(ready["endpoint"]).status_code == 200
            assert _read_json_line(transport.receipts)["cursor"] == 1
            transport.send(
                {"schema": "headroom.transport.release.v1", "lease_id": ready["lease_id"]}
            )
            assert transport.process.wait(timeout=15) == 0
        finally:
            transport.close()
            listener.close()


@pytest.mark.parametrize(
    "record",
    [
        {"schema": ACTIVATE_SCHEMA, "lease_id": "lease-not-this-one"},
        {"schema": ACTIVATE_SCHEMA, "lease_id": None},
        {"schema": ACTIVATE_SCHEMA},
        {"schema": "headroom.transport.release.v1", "lease_id": "lease-not-this-one"},
    ],
    ids=["other-lease", "null-lease", "no-lease", "release-other-lease"],
)
def test_a_mismatched_activate_is_rejected_and_nothing_is_served(tmp_path, record) -> None:
    listener = _loopback_listener()
    port = listener.getsockname()[1]
    with _FakeAnthropicServer() as upstream:
        transport, ready = _start(tmp_path, listener, _activated_payload(upstream.url))
        try:
            client = _connect_and_write(port)
            transport.send(record)
            assert transport.process.wait(timeout=15) != 0
            # The transport exited without accepting the queued client.
            assert _take_queued(listener) == REQUEST
            client.close()
        finally:
            transport.close()
            listener.close()


def test_control_eof_before_activate_exits_without_serving(tmp_path) -> None:
    listener = _loopback_listener()
    port = listener.getsockname()[1]
    with _FakeAnthropicServer() as upstream:
        transport, _ = _start(tmp_path, listener, _activated_payload(upstream.url))
        try:
            client = _connect_and_write(port)
            transport.control_parent.close()
            assert transport.process.wait(timeout=15) == 0
            assert _take_queued(listener) == REQUEST
            client.close()
            assert upstream.requests == []
        finally:
            transport.close()
            listener.close()


def test_release_before_activate_ends_the_lease_without_serving(tmp_path) -> None:
    listener = _loopback_listener()
    port = listener.getsockname()[1]
    with _FakeAnthropicServer() as upstream:
        transport, ready = _start(tmp_path, listener, _activated_payload(upstream.url))
        try:
            client = _connect_and_write(port)
            transport.send(
                {"schema": "headroom.transport.release.v1", "lease_id": ready["lease_id"]}
            )
            assert transport.process.wait(timeout=15) == 0
            assert _take_queued(listener) == REQUEST
            client.close()
        finally:
            transport.close()
            listener.close()


def test_without_the_opt_in_the_transport_serves_from_readiness_as_before(tmp_path) -> None:
    listener = _loopback_listener()
    port = listener.getsockname()[1]
    with _FakeAnthropicServer() as upstream:
        transport, ready = _start(tmp_path, listener, _acquire_payload(upstream_url=upstream.url))
        try:
            assert set(ready) == {
                "schema",
                "status",
                "endpoint",
                "process_identity",
                "process_group_id",
                "lease_id",
                "runtime_set_digest",
                "account_revision",
                "policy_digest",
                "binding_digest",
                "receipt_cursor",
                "child_environment",
            }
            # Served at once, with no activate ever sent.
            client = _connect_and_write(port)
            assert _answer(client).startswith(b"HTTP/1.1 200")
            client.close()
            # activate.v1 is not part of a lease that did not opt in: like any
            # other non-release record it is an invalid control record.
            transport.send({"schema": ACTIVATE_SCHEMA, "lease_id": ready["lease_id"]})
            assert transport.process.wait(timeout=15) != 0
        finally:
            transport.close()
            listener.close()


def test_activation_holds_on_a_listener_the_transport_opens_itself(tmp_path) -> None:
    with _FakeAnthropicServer() as upstream:
        transport = _TransportProcess(tmp_path)
        threading.Thread(target=transport.process.stderr.read, daemon=True).start()
        threading.Thread(target=transport.process.stdout.read, daemon=True).start()
        try:
            transport.send(_activated_payload(upstream.url))
            ready = _read_json_line(transport.readiness)
            port = int(ready["endpoint"].rsplit(":", 1)[1])
            client = _connect_and_write(port)
            _assert_unanswered(client)
            transport.send({"schema": ACTIVATE_SCHEMA, "lease_id": ready["lease_id"]})
            assert _answer(client).startswith(b"HTTP/1.1 200")
            client.close()
            transport.send(
                {"schema": "headroom.transport.release.v1", "lease_id": ready["lease_id"]}
            )
            assert transport.process.wait(timeout=15) == 0
        finally:
            transport.close()
