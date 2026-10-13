"""The pool isolation gate: several leases in one Headroom process.

Each test starts a real ``headroom transport pool`` process and admits leases
over its admission socket exactly as a supervisor does: one ``admit.v1`` record
sent with the lease's control, readiness, receipt and diagnostics descriptors
and its listener. The test keeps its own copy of every lease listener, so a
connection the pool has not accepted stays in that listener's queue, where the
test can take it and read its request bytes intact.
"""

from __future__ import annotations

import array
import json
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from headroom.cli.main import main
from headroom.transport.pool import (
    ADMISSION_SCHEMA,
    ADMIT_DESCRIPTORS,
    ADMIT_SCHEMA,
    DRAIN_SCHEMA,
    POOL_SCHEMA,
)
from headroom.transport.protocol import (
    ACTIVATE_SCHEMA,
    AcquireRequest,
    canonical_digest,
    current_process_identity,
)
from tests.test_transport_protocol import _acquire_payload

RELEASE_SCHEMA = "headroom.transport.release.v1"
HELLO = b"HEAD /api/hello HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
HEADERS = {
    "authorization": "Bearer fake-oauth-account-a",
    "user-agent": "claude-cli/test",
    "anthropic-version": "2023-06-01",
}


# -- fake upstream --------------------------------------------------------


class _RecordingHandler(BaseHTTPRequestHandler):
    server: RecordingUpstream
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        size = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(size)
        body = json.loads(raw)
        with self.server.lock:
            self.server.requests.append(
                {
                    "method": "POST",
                    "path": self.path,
                    "headers": {key.lower(): value for key, value in self.headers.items()},
                    "raw": raw,
                    "body": body,
                    "client_port": self.client_address[1],
                }
            )
        if body.get("stream"):
            events = [
                ("message_start", {"type": "message_start", "message": {"id": "msg-fake"}}),
                ("content_block_delta", {"type": "content_block_delta", "delta": {"text": "hi"}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            payload = b"".join(
                f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
            )
            content_type = "text/event-stream"
        else:
            payload = json.dumps(
                {
                    "id": "msg-fake",
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model", "unknown"),
                    "content": [{"type": "text", "text": "visible fake-provider output"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                },
                separators=(",", ":"),
            ).encode()
            content_type = "application/json"
        status = self.server.status
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


class RecordingUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _RecordingHandler)
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.status = 200
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def netloc(self) -> str:
        host, port = self.server_address[:2]
        return f"{host}:{port}"

    def __enter__(self) -> RecordingUpstream:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=2)


# -- pool and lease harness -----------------------------------------------


class Records:
    """Newline-delimited records from one descriptor, without hidden buffering.

    A buffered file would read several records at once and leave select()
    blind to the ones it holds; this reader keeps its own buffer.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.buffer = b""
        self.eof = False

    def fileno(self) -> int:
        return self.fd

    def _fill(self, deadline: float) -> bool:
        """Read once; False if nothing arrived before the deadline."""

        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([self.fd], [], [], remaining)
        if not ready:
            return False
        chunk = os.read(self.fd, 65536)
        if chunk:
            self.buffer += chunk
        else:
            self.eof = True
        return True

    def read(self, timeout: float = 40.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            assert not self.eof, "record stream closed"
            assert self._fill(deadline), "timed out waiting for a record"
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def pending(self, timeout: float = 0.5) -> bool:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer and not self.eof and time.monotonic() < deadline:
            self._fill(deadline)
        return b"\n" in self.buffer

    def at_eof(self, timeout: float = 15.0) -> bool:
        """True once every writer has closed the descriptor; records are kept."""

        deadline = time.monotonic() + timeout
        while not self.eof and time.monotonic() < deadline:
            self._fill(deadline)
        return self.eof

    def remaining(self, timeout: float = 15.0) -> list[dict[str, Any]]:
        assert self.at_eof(timeout)
        records = [json.loads(line) for line in self.buffer.splitlines() if line]
        self.buffer = b""
        return records


def _loopback_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    return listener


class Lease:
    """The supervisor's side of one admitted lease."""

    def __init__(self) -> None:
        self.control, self._control_child = socket.socketpair()
        readiness_read, self._readiness_write = os.pipe()
        receipt_read, self._receipt_write = os.pipe()
        diagnostics_read, self._diagnostics_write = os.pipe()
        self.readiness = Records(readiness_read)
        self.receipts = Records(receipt_read)
        self.diagnostics = Records(diagnostics_read)
        self.listener = _loopback_listener()
        self.port = self.listener.getsockname()[1]
        self.ready: dict[str, Any] | None = None

    def child_fds(self) -> list[int]:
        return [
            self._control_child.fileno(),
            self._readiness_write,
            self._receipt_write,
            self._diagnostics_write,
            self.listener.fileno(),
        ]

    def handed_over(self) -> None:
        """Close the copies only the pool may hold; the listener copy stays."""

        self._control_child.close()
        for fd in (self._readiness_write, self._receipt_write, self._diagnostics_write):
            os.close(fd)
        self._readiness_write = self._receipt_write = self._diagnostics_write = -1

    def send(self, record: dict[str, Any]) -> None:
        self.control.sendall((json.dumps(record, separators=(",", ":")) + "\n").encode())

    def acquire(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.send(payload)
        self.ready = self.readiness.read()
        return self.ready

    def activate(self) -> None:
        assert self.ready is not None
        self.send({"schema": ACTIVATE_SCHEMA, "lease_id": self.ready["lease_id"]})

    def release(self) -> None:
        assert self.ready is not None
        self.send({"schema": RELEASE_SCHEMA, "lease_id": self.ready["lease_id"]})

    @property
    def endpoint(self) -> str:
        assert self.ready is not None
        return self.ready["endpoint"]

    def ended(self, timeout: float = 15.0) -> bool:
        """The pool closed this lease's receipt pipe: the lease is over."""

        return self.receipts.at_eof(timeout)

    def close(self) -> None:
        for item in (self.control, self._control_child, self.listener):
            item.close()
        for records in (self.readiness, self.receipts, self.diagnostics):
            os.close(records.fd)
        for fd in (self._readiness_write, self._receipt_write, self._diagnostics_write):
            if fd >= 0:
                os.close(fd)
        self._readiness_write = self._receipt_write = self._diagnostics_write = -1


class Pool:
    def __init__(self, tmp_path: Path, *args: str) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.admission, child = socket.socketpair()
        source_root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env.update(
            {
                "HEADROOM_CONFIG_DIR": str(tmp_path / "config"),
                "HEADROOM_WORKSPACE_DIR": str(tmp_path / "workspace"),
                "HEADROOM_REQUIRE_RUST_CORE": "false",
                "HEADROOM_SKIP_UPSTREAM_CHECK": "1",
                "HEADROOM_UPDATE_CHECK": "off",
                "HEADROOM_STATELESS": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": os.pathsep.join(
                    value for value in (str(source_root), env.get("PYTHONPATH", "")) if value
                ),
            }
        )
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "headroom.cli",
                "transport",
                "pool",
                "--admission-fd",
                str(child.fileno()),
                *args,
            ],
            pass_fds=[child.fileno()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            cwd=tmp_path,
            env=env,
            start_new_session=True,
        )
        child.close()
        self.stderr: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.admission_reader = Records(self.admission.fileno())
        self.hello = self.admission_reader.read(timeout=60)
        self.leases: list[Lease] = []

    def _drain_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr.append(line)

    def send_admit(self, record: bytes, fds: list[int]) -> dict[str, Any]:
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
        self.admission.sendmsg([record], ancillary)
        return self.admission_reader.read()

    def admit(self, admission_id: str | None = None, lease: Lease | None = None) -> Lease:
        lease = lease or Lease()
        self.leases.append(lease)
        record = {
            "schema": ADMIT_SCHEMA,
            "admission_id": admission_id or f"admit-{len(self.leases)}",
            "fds": list(ADMIT_DESCRIPTORS),
        }
        reply = self.send_admit(
            (json.dumps(record) + "\n").encode(),
            lease.child_fds(),
        )
        assert reply["status"] == "admitted", reply
        assert reply["admission_id"] == record["admission_id"]
        lease.handed_over()
        return lease

    def drain(self) -> dict[str, Any]:
        self.admission.sendall((json.dumps({"schema": DRAIN_SCHEMA}) + "\n").encode())
        return self.admission_reader.read()

    def close(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)
        self.admission.close()
        for lease in self.leases:
            lease.close()


@contextmanager
def _pool(tmp_path: Path, *args: str) -> Iterator[Pool]:
    pool = Pool(tmp_path, *args)
    try:
        assert pool.hello["schema"] == POOL_SCHEMA
        assert pool.hello["status"] == "ready", pool.hello
        assert pool.hello["process_identity"]["pid"] == pool.process.pid
        yield pool
    finally:
        pool.close()
        # Shown with a failing test's output.
        print("".join(pool.stderr))


def _payload(upstream_url: str, label: str, *, parent: dict[str, Any] | None = None) -> dict:
    payload = _acquire_payload(upstream_url=upstream_url)
    payload["run_id"] = f"run-{label}"
    payload["cwd"] = f"/private/tmp/worktree-{label}"
    payload["worktree_identity"] = canonical_digest({"label": f"worktree-{label}"})
    payload["materialization_id"] = f"mat-{label}"
    if parent is not None:
        payload["parent_process"] = parent
    payload["activation"] = "required"
    payload["binding_digest"] = AcquireRequest.binding_digest_for(payload)
    return payload


def _ready_lease(pool: Pool, upstream: RecordingUpstream, label: str, **kwargs: Any) -> Lease:
    lease = pool.admit(f"admit-{label}")
    ready = lease.acquire(_payload(upstream.url, label, **kwargs))
    assert ready["status"] == "ready", ready
    assert ready["endpoint"] == f"http://127.0.0.1:{lease.port}"
    assert ready["process_identity"]["pid"] == pool.process.pid
    assert ready["process_group_id"] == pool.process.pid
    return lease


def _messages_request(marker: str, *, stream: bool = False) -> dict[str, Any]:
    return {
        "model": "claude-test",
        "max_tokens": 16,
        "stream": stream,
        "messages": [{"role": "user", "content": f"request from {marker}"}],
    }


def _post(lease: Lease, marker: str, client: httpx.Client | None = None) -> httpx.Response:
    sender = client or httpx
    return sender.post(
        lease.endpoint + "/v1/messages",
        json=_messages_request(marker),
        headers=HEADERS,
        timeout=30,
    )


def _connect_and_write(port: int, request: bytes = HELLO) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(request)
    return client


def _assert_unanswered(client: socket.socket, wait: float = 1.0) -> None:
    client.settimeout(wait)
    with pytest.raises(TimeoutError):
        client.recv(1)


def _take_queued(listener: socket.socket, size: int = len(HELLO)) -> bytes:
    listener.settimeout(5)
    accepted, _ = listener.accept()
    with accepted:
        accepted.settimeout(5)
        received = b""
        while len(received) < size:
            chunk = accepted.recv(size - len(received))
            if not chunk:
                break
            received += chunk
    return received


def _marker(record: dict[str, Any]) -> str:
    return record["body"]["messages"][0]["content"].removeprefix("request from ")


# -- tests ----------------------------------------------------------------


def test_pool_cli_is_registered_and_caps_leases_at_eight() -> None:
    result = CliRunner().invoke(main, ["transport", "pool", "--help"])
    assert result.exit_code == 0
    assert "--admission-fd" in result.output
    assert "--max-leases" in result.output

    result = CliRunner().invoke(
        main, ["transport", "pool", "--admission-fd", "3", "--max-leases", "9"]
    )
    assert result.exit_code != 0
    assert "--max-leases" in result.output


def test_pool_refuses_an_admission_socket_with_a_path() -> None:
    # A short directory: AF_UNIX paths are limited to about 100 bytes.
    directory = tempfile.TemporaryDirectory(prefix="hrpool", dir="/tmp")
    path = Path(directory.name) / "admission.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "headroom.cli",
                "transport",
                "pool",
                "--admission-fd",
                str(client.fileno()),
            ],
            pass_fds=[client.fileno()],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )
        assert completed.returncode != 0
        assert "filesystem path" in completed.stderr
    finally:
        client.close()
        server.close()
        directory.cleanup()


def test_receipts_and_diagnostics_land_only_on_the_owning_lease(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        first = _ready_lease(pool, upstream, "a")
        second = _ready_lease(pool, upstream, "b")
        assert first.ready["lease_id"] != second.ready["lease_id"]
        first.activate()
        second.activate()

        for round_ in range(3):
            assert _post(first, f"a-{round_}").status_code == 200
            assert _post(second, f"b-{round_}").status_code == 200

        for lease, label in ((first, "a"), (second, "b")):
            receipts = [lease.receipts.read() for _ in range(3)]
            assert [receipt["cursor"] for receipt in receipts] == [1, 2, 3]
            assert {receipt["run_id"] for receipt in receipts} == {f"run-{label}"}
            assert {receipt["binding_digest"] for receipt in receipts} == {
                lease.ready["binding_digest"]
            }
            assert all(receipt["outcome"] == "succeeded" for receipt in receipts)

        # Nothing more on either pipe: no receipt crossed to the other lease.
        for lease in (first, second):
            assert not lease.receipts.pending()

        for lease, label in ((first, "a"), (second, "b")):
            lease.release()
            assert lease.ended()
            events = lease.diagnostics.remaining()
            assert events, "every lease has its own diagnostics"
            assert {event["run_id"] for event in events} == {f"run-{label}"}
            assert {event["lease_id"] for event in events} == {lease.ready["lease_id"]}
            names = [event["event"] for event in events]
            assert "transport_boot" in names
            assert "transport_activated" in names

        markers = sorted(_marker(record) for record in upstream.requests)
        assert markers == sorted([f"a-{i}" for i in range(3)] + [f"b-{i}" for i in range(3)])


def test_no_lease_accepts_before_its_own_activate(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        serving = _ready_lease(pool, upstream, "a")
        serving.activate()
        assert _post(serving, "a").status_code == 200

        # A second lease in the same, already serving process: ready but not
        # activated, it accepts nothing and reads nothing.
        waiting = _ready_lease(pool, upstream, "b")
        early = _connect_and_write(waiting.port)
        _assert_unanswered(early)
        assert _take_queued(waiting.listener) == HELLO
        early.close()

        request = json.dumps(_messages_request("b-early")).encode()
        raw = (
            f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"authorization: Bearer fake-oauth-account-a\r\n"
            f"content-type: application/json\r\ncontent-length: {len(request)}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode() + request
        queued = _connect_and_write(waiting.port, raw)
        _assert_unanswered(queued, 0.5)
        assert [_marker(record) for record in upstream.requests] == ["a"]

        # Activating the other lease or serving traffic on it changes nothing.
        assert _post(serving, "a-2").status_code == 200
        _assert_unanswered(queued, 0.5)

        activated_at = time.monotonic()
        waiting.activate()
        queued.settimeout(30)
        answer = b""
        while b"\r\n\r\n" not in answer:
            chunk = queued.recv(4096)
            if not chunk:
                break
            answer += chunk
        queued.close()
        assert answer.startswith(b"HTTP/1.1 200"), answer
        assert time.monotonic() >= activated_at
        assert [_marker(record) for record in upstream.requests] == ["a", "a-2", "b-early"]


def test_a_different_upstream_is_refused_and_hosts_never_cross(tmp_path) -> None:
    with (
        RecordingUpstream() as upstream,
        RecordingUpstream() as other_upstream,
        _pool(tmp_path) as pool,
    ):
        first = _ready_lease(pool, upstream, "a")
        first.activate()

        refused = pool.admit("admit-other-upstream")
        ready = refused.acquire(_payload(other_upstream.url, "b"))
        assert ready == {
            "schema": "headroom.transport.ready.v1",
            "status": "error",
            "error": {
                "category": "pool_scope_mismatch",
                "message": "acquire is outside this pool's sharing scope",
            },
        }
        assert refused.ended()

        other_account = _acquire_payload(upstream_url=upstream.url)
        other_account["account"]["ref"] = "account://claude/other"
        other_account["activation"] = "required"
        other_account["binding_digest"] = AcquireRequest.binding_digest_for(other_account)
        refused_account = pool.admit("admit-other-account")
        assert refused_account.acquire(other_account)["error"]["category"] == (
            "pool_scope_mismatch"
        )

        unactivated = _payload(upstream.url, "c")
        del unactivated["activation"]
        refused_activation = pool.admit("admit-no-activation")
        assert refused_activation.acquire(unactivated)["error"]["category"] == (
            "activation_required"
        )

        second = _ready_lease(pool, upstream, "d")
        second.activate()
        assert _post(first, "a").status_code == 200
        assert _post(second, "d").status_code == 200
        assert other_upstream.requests == []
        # Each request reached the pinned upstream under that upstream's own
        # Host, never a lease endpoint's or another lease's.
        assert {record["headers"]["host"] for record in upstream.requests} == {upstream.netloc}
        assert sorted(_marker(record) for record in upstream.requests) == ["a", "d"]


def test_leases_never_share_an_upstream_connection(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        first = _ready_lease(pool, upstream, "a")
        second = _ready_lease(pool, upstream, "b")
        first.activate()
        second.activate()
        with httpx.Client() as client_a, httpx.Client() as client_b:
            for round_ in range(4):
                assert _post(first, f"a-{round_}", client_a).status_code == 200
                assert _post(second, f"b-{round_}", client_b).status_code == 200
        ports: dict[str, set[int]] = {"a": set(), "b": set()}
        for record in upstream.requests:
            ports[_marker(record)[0]].add(record["client_port"])
        assert ports["a"] and ports["b"]
        assert not ports["a"] & ports["b"]


def test_release_eof_and_parent_death_end_only_their_own_lease(tmp_path) -> None:
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        parent_identity = current_process_identity(parent.pid).to_dict()
        with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
            released = _ready_lease(pool, upstream, "released")
            closed = _ready_lease(pool, upstream, "closed")
            orphaned = _ready_lease(pool, upstream, "orphaned", parent=parent_identity)
            survivor = _ready_lease(pool, upstream, "survivor")
            for lease in (released, closed, orphaned, survivor):
                lease.activate()
                assert _post(lease, lease.ready["lease_id"]).status_code == 200
                assert lease.receipts.read()["cursor"] == 1

            released.release()
            assert released.ended()
            assert _post(survivor, "after-release").status_code == 200

            closed.control.close()
            assert closed.ended()
            assert _post(survivor, "after-eof").status_code == 200

            parent.kill()
            parent.wait(timeout=10)
            assert orphaned.ended(timeout=20)
            events = orphaned.diagnostics.remaining()
            assert any(
                event["event"] == "transport_shutdown" and event["reason"] == "parent_not_live"
                for event in events
            )

            assert pool.process.poll() is None
            assert _post(survivor, "after-parent-death").status_code == 200
            assert [survivor.receipts.read()["cursor"] for _ in range(3)] == [2, 3, 4]
            # An ended lease serves nothing more: its connection waits unaccepted.
            for lease in (released, closed, orphaned):
                client = _connect_and_write(lease.port)
                _assert_unanswered(client, 0.3)
                client.close()

            assert pool.drain() == {"schema": POOL_SCHEMA, "status": "draining", "leases": 1}
            assert pool.process.poll() is None
            survivor.release()
            assert survivor.ended()
            assert pool.process.wait(timeout=20) == 0
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)


def test_sigkill_of_the_pool_closes_every_lease_channel(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        active = _ready_lease(pool, upstream, "a")
        active.activate()
        assert _post(active, "a").status_code == 200
        waiting = _ready_lease(pool, upstream, "b")
        unacquired = pool.admit("admit-unacquired")

        os.kill(pool.process.pid, signal.SIGKILL)
        pool.process.wait(timeout=10)

        for lease in (active, waiting, unacquired):
            assert lease.readiness.at_eof()
            assert lease.receipts.at_eof()
            assert lease.diagnostics.at_eof()
            lease.control.settimeout(5)
            assert lease.control.recv(1) == b""
        assert pool.admission.recv(1) == b""


def test_the_lease_cap_is_enforced_and_a_slot_frees_on_lease_end(tmp_path) -> None:
    with _pool(tmp_path) as pool:
        assert pool.hello["max_leases"] == 8
        leases = [pool.admit(f"admit-{index}") for index in range(8)]

        extra = Lease()
        pool.leases.append(extra)
        record = {"schema": ADMIT_SCHEMA, "admission_id": "admit-9", "fds": list(ADMIT_DESCRIPTORS)}
        reply = pool.send_admit((json.dumps(record) + "\n").encode(), extra.child_fds())
        assert reply == {
            "schema": ADMISSION_SCHEMA,
            "admission_id": "admit-9",
            "status": "rejected",
            "error": {"category": "lease_cap"},
        }
        # The refused lease's descriptors were closed, not kept.
        extra.handed_over()
        assert extra.readiness.at_eof()

        leases[0].control.close()
        assert leases[0].ended()
        pool.admit("admit-10")


def _admit_record(admission_id: str = "admit-x", **overrides: Any) -> bytes:
    record: dict[str, Any] = {
        "schema": ADMIT_SCHEMA,
        "admission_id": admission_id,
        "fds": list(ADMIT_DESCRIPTORS),
    }
    record.update(overrides)
    return (json.dumps(record) + "\n").encode()


def _reject(pool: Pool, record: bytes, fds: list[int], category: str) -> None:
    reply = pool.send_admit(record, fds)
    assert reply["schema"] == ADMISSION_SCHEMA
    assert reply["status"] == "rejected", reply
    assert reply["error"] == {"category": category}


def test_admission_rejects_malformed_records_bad_descriptors_and_reuse(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        live = _ready_lease(pool, upstream, "live")
        live.activate()

        def fresh() -> Lease:
            lease = Lease()
            pool.leases.append(lease)
            return lease

        malformed = [
            b"not json\n",
            b"[]\n",
            _admit_record(schema="headroom.transport.admit.v2"),
            _admit_record(extra=True),
            _admit_record(fds=["control", "readiness", "receipt", "listener", "diagnostics"]),
            _admit_record(admission_id=""),
            _admit_record(admission_id=7),
        ]
        for record in malformed:
            lease = fresh()
            _reject(pool, record, lease.child_fds(), "invalid_admit")
            lease.handed_over()
            assert lease.readiness.at_eof(), record

        too_few = fresh()
        _reject(pool, _admit_record("admit-few"), too_few.child_fds()[:4], "invalid_admit")
        too_few.handed_over()
        assert too_few.readiness.at_eof()

        # A readiness read end, a pipe as control, a non-listening socket.
        wrong = fresh()
        fds = wrong.child_fds()
        _reject(
            pool,
            _admit_record("admit-read-end"),
            [fds[0], wrong.readiness.fd, *fds[2:]],
            "invalid_descriptor",
        )
        wrong.handed_over()
        assert wrong.receipts.at_eof()

        wrong = fresh()
        fds = wrong.child_fds()
        _reject(pool, _admit_record("admit-pipe-control"), [fds[1], *fds[1:]], "invalid_descriptor")
        wrong.handed_over()

        wrong = fresh()
        unbound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        fds = wrong.child_fds()
        _reject(
            pool, _admit_record("admit-unbound"), [*fds[:4], unbound.fileno()], "invalid_descriptor"
        )
        unbound.close()
        wrong.handed_over()

        # Reuse: one pipe twice within an admit, and another live lease's listener.
        reused = fresh()
        fds = reused.child_fds()
        _reject(
            pool,
            _admit_record("admit-dup"),
            [fds[0], fds[1], fds[2], fds[2], fds[4]],
            "descriptor_reuse",
        )
        reused.handed_over()
        assert reused.readiness.at_eof()

        stolen = fresh()
        fds = stolen.child_fds()
        _reject(
            pool,
            _admit_record("admit-stolen"),
            [*fds[:4], live.listener.fileno()],
            "descriptor_reuse",
        )
        stolen.handed_over()

        # An admission id is used once; a drained pool admits nothing.
        accepted = pool.admit("admit-once")
        again = fresh()
        _reject(pool, _admit_record("admit-once"), again.child_fds(), "duplicate_admission")
        again.handed_over()

        assert pool.drain()["status"] == "draining"
        late = fresh()
        _reject(pool, _admit_record("admit-late"), late.child_fds(), "draining")
        late.handed_over()
        assert late.readiness.at_eof()

        # Leases admitted before the drain keep serving.
        assert _post(live, "live").status_code == 200
        accepted.control.close()
        assert accepted.ended()
        live.release()
        assert live.ended()
        assert pool.process.wait(timeout=20) == 0


def test_broken_admission_framing_stops_admitting_but_not_serving(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        live = _ready_lease(pool, upstream, "live")
        live.activate()
        # Two records in one write: the supervisor did not wait for a reply.
        lease = Lease()
        pool.leases.append(lease)
        reply = pool.send_admit(
            _admit_record("admit-a") + _admit_record("admit-b"), lease.child_fds()
        )
        assert reply["error"] == {"category": "invalid_admission_framing"}
        lease.handed_over()
        assert lease.readiness.at_eof()

        assert _post(live, "live").status_code == 200
        live.release()
        assert pool.process.wait(timeout=20) == 0


def test_admission_eof_drains_the_pool(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        live = _ready_lease(pool, upstream, "live")
        live.activate()
        pool.admission.shutdown(socket.SHUT_WR)
        time.sleep(0.5)
        assert pool.process.poll() is None
        assert _post(live, "live").status_code == 200
        live.release()
        assert pool.process.wait(timeout=20) == 0


def test_an_idle_drained_pool_exits(tmp_path) -> None:
    with _pool(tmp_path) as pool:
        assert pool.drain() == {"schema": POOL_SCHEMA, "status": "draining", "leases": 0}
        assert pool.process.wait(timeout=20) == 0


def test_leases_acquired_concurrently_each_wait_for_their_own_activate(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        leases = [pool.admit(f"admit-{label}") for label in "abc"]
        for lease, label in zip(leases, "abc", strict=True):
            lease.send(_payload(upstream.url, label))
        for lease in leases:
            lease.ready = lease.readiness.read()
            assert lease.ready["status"] == "ready", lease.ready
        waiting = [_connect_and_write(lease.port) for lease in leases]
        for client in waiting:
            _assert_unanswered(client, 0.3)

        leases[1].activate()
        assert _post(leases[1], "b").status_code == 200
        for index in (0, 2):
            _assert_unanswered(waiting[index], 0.3)
            assert _take_queued(leases[index].listener) == HELLO
        for client in waiting:
            client.close()


def test_a_lease_whose_receipt_pipe_breaks_stops_serving_alone(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        broken = _ready_lease(pool, upstream, "broken")
        healthy = _ready_lease(pool, upstream, "healthy")
        broken.activate()
        healthy.activate()
        os.close(broken.receipts.fd)
        broken.receipts.fd = os.open(os.devnull, os.O_RDONLY)

        try:
            response = _post(broken, "broken")
            assert response.status_code >= 500
        except httpx.TransportError:
            pass
        assert broken.readiness.at_eof()
        assert _post(healthy, "healthy").status_code == 200
        assert healthy.receipts.read()["run_id"] == "run-healthy"


def test_sigterm_ends_every_lease_and_the_pool(tmp_path) -> None:
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        active = _ready_lease(pool, upstream, "a")
        active.activate()
        unacquired = pool.admit("admit-unacquired")
        os.kill(pool.process.pid, signal.SIGTERM)
        assert pool.process.wait(timeout=20) == 0
        for lease in (active, unacquired):
            assert lease.receipts.at_eof()
