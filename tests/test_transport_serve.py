from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from click.testing import CliRunner

from headroom.cli.main import main
from headroom.transport.protocol import AcquireRequest, canonical_digest
from tests.test_transport_protocol import _acquire_payload


class _FakeAnthropicHandler(BaseHTTPRequestHandler):
    server: _FakeAnthropicServer

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        size = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(size))
        self.server.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("authorization"),
                "body": body,
            }
        )
        if self.server.crash_next:
            self.server.crash_next = False
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        response = {
            "id": f"msg-fake-{len(self.server.requests)}",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": [{"type": "text", "text": "visible fake-provider output"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }
        encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


class _FakeAnthropicServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FakeAnthropicHandler)
        self.requests = []
        self.crash_next = False
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> _FakeAnthropicServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=2)


def _read_json_line(file, *, timeout: float = 40.0) -> dict[str, Any]:
    ready, _, _ = select.select([file.fileno()], [], [], timeout)
    assert ready, "timed out waiting for inherited-FD record"
    line = file.readline()
    assert line, "inherited-FD record stream closed"
    return json.loads(line)


class _TransportProcess:
    def __init__(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.control_parent, control_child = socket.socketpair()
        readiness_read, readiness_write = os.pipe()
        receipt_read, receipt_write = os.pipe()
        env = os.environ.copy()
        source_root = Path(__file__).resolve().parents[1]
        env.update(
            {
                "HEADROOM_CONFIG_DIR": str(tmp_path / "config"),
                "HEADROOM_WORKSPACE_DIR": str(tmp_path / "workspace"),
                "HEADROOM_REQUIRE_RUST_CORE": "false",
                "HEADROOM_SKIP_UPSTREAM_CHECK": "1",
                "HEADROOM_UPDATE_CHECK": "off",
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
                "serve",
                "--control-fd",
                str(control_child.fileno()),
                "--readiness-fd",
                str(readiness_write),
                "--receipt-fd",
                str(receipt_write),
            ],
            pass_fds=(control_child.fileno(), readiness_write, receipt_write),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=tmp_path,
            env=env,
        )
        control_child.close()
        os.close(readiness_write)
        os.close(receipt_write)
        self.readiness = os.fdopen(readiness_read, "r", encoding="utf-8")
        self.receipts = os.fdopen(receipt_read, "r", encoding="utf-8")

    def send(self, record: dict[str, Any]) -> None:
        self.control_parent.sendall(
            (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )

    def close(self) -> None:
        if self.control_parent.fileno() >= 0:
            self.control_parent.close()
        self.readiness.close()
        self.receipts.close()
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)


def test_transport_cli_is_registered() -> None:
    result = CliRunner().invoke(main, ["transport", "serve", "--help"])

    assert result.exit_code == 0
    assert "--control-fd" in result.output
    assert "--readiness-fd" in result.output
    assert "--receipt-fd" in result.output


def test_transport_serve_rejects_stale_parent_before_binding(tmp_path) -> None:
    transport = _TransportProcess(tmp_path)
    try:
        payload = _acquire_payload()
        payload["parent_process"]["start_time"] += 10000
        payload["binding_digest"] = AcquireRequest.binding_digest_for(payload)
        transport.send(payload)

        record = _read_json_line(transport.readiness)
        assert record["status"] == "error"
        assert record["error"]["category"] == "stale_parent_process"
        assert transport.process.wait(timeout=10) != 0
        assert transport.readiness.read() == ""
    finally:
        transport.close()


def test_transport_serve_routes_fake_provider_and_emits_receipts(tmp_path) -> None:
    transport = _TransportProcess(tmp_path)
    try:
        with _FakeAnthropicServer() as upstream:
            transport.send(_acquire_payload(upstream_url=upstream.url))
            ready = _read_json_line(transport.readiness)
            assert ready["status"] == "ready"
            assert ready["endpoint"].startswith("http://127.0.0.1:")
            assert ready["receipt_cursor"] == 0
            assert ready["process_group_id"] == os.getpgid(transport.process.pid)

            reducible = "".join(
                f"/src/example.py:{line}: repeated matching value\n" for line in range(1, 400)
            )
            request = {
                "model": "claude-test",
                "max_tokens": 32,
                "stream": False,
                "messages": [
                    {"role": "user", "content": "inspect the search results"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tool-01",
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
                                "tool_use_id": "tool-01",
                                "content": reducible,
                            }
                        ],
                    },
                ],
            }
            headers = {
                "authorization": "Bearer fake-oauth-account-a",
                "user-agent": "claude-cli/test",
                "anthropic-version": "2023-06-01",
            }
            response = httpx.post(ready["endpoint"] + "/v1/messages", json=request, headers=headers)
            assert response.status_code == 200
            assert response.json()["content"] == [
                {"type": "text", "text": "visible fake-provider output"}
            ]
            compressed_receipt = _read_json_line(transport.receipts)
            assert compressed_receipt["cursor"] == 1
            assert compressed_receipt["saved_tokens"] > 0
            assert compressed_receipt["outcome"] == "succeeded"
            assert compressed_receipt["upstream_reached"] is True
            assert upstream.requests[0]["authorization"] == "Bearer fake-oauth-account-a"
            forwarded = json.dumps(upstream.requests[0]["body"], separators=(",", ":"))
            assert len(forwarded) < len(json.dumps(request, separators=(",", ":")))

            zero_response = httpx.post(
                ready["endpoint"] + "/v1/messages",
                json={
                    "model": "claude-test",
                    "max_tokens": 16,
                    "stream": False,
                    "messages": [{"role": "user", "content": "short request"}],
                },
                headers=headers,
            )
            assert zero_response.status_code == 200
            zero_receipt = _read_json_line(transport.receipts)
            assert zero_receipt["cursor"] == 2
            assert zero_receipt["saved_tokens"] == 0
            assert zero_receipt["accounting_status"] == "evaluated"
            assert zero_receipt["bypass_reason"] is None

            transport.send(
                {
                    "schema": "headroom.transport.release.v1",
                    "lease_id": ready["lease_id"],
                }
            )
            assert transport.process.wait(timeout=15) == 0
            assert transport.readiness.read() == ""
            assert not (tmp_path / "workspace").exists()
            assert not (tmp_path / "config").exists()
            assert list(tmp_path.iterdir()) == []
            assert "fake-oauth-account-a" not in json.dumps([compressed_receipt, zero_receipt])
    finally:
        transport.close()


def test_transport_exits_when_private_control_channel_closes(tmp_path) -> None:
    transport = _TransportProcess(tmp_path)
    try:
        with _FakeAnthropicServer() as upstream:
            transport.send(_acquire_payload(upstream_url=upstream.url))
            ready = _read_json_line(transport.readiness)
            assert ready["status"] == "ready"

            transport.control_parent.close()

            assert transport.process.wait(timeout=15) == 0
            assert transport.readiness.read() == ""
    finally:
        transport.close()


def test_transport_request_crash_yields_failed_receipt_without_fallback(tmp_path) -> None:
    transport = _TransportProcess(tmp_path)
    try:
        with _FakeAnthropicServer() as upstream:
            upstream.crash_next = True
            transport.send(_acquire_payload(upstream_url=upstream.url))
            ready = _read_json_line(transport.readiness)

            response = httpx.post(
                ready["endpoint"] + "/v1/messages",
                json={
                    "model": "claude-test",
                    "max_tokens": 16,
                    "stream": False,
                    "messages": [{"role": "user", "content": "small request"}],
                },
                headers={
                    "authorization": "Bearer fake-oauth-account-a",
                    "user-agent": "claude-cli/test",
                    "anthropic-version": "2023-06-01",
                },
            )

            assert response.status_code == 502
            receipt = _read_json_line(transport.receipts)
            assert receipt["outcome"] == "failed"
            assert receipt["error_reason"] == "upstream_request_error"
            assert receipt["upstream_reached"] is False
            assert len(upstream.requests) == 1

            transport.send(
                {
                    "schema": "headroom.transport.release.v1",
                    "lease_id": ready["lease_id"],
                }
            )
            assert transport.process.wait(timeout=15) == 0
    finally:
        transport.close()


def test_missing_receipt_channel_fails_the_request_closed(tmp_path) -> None:
    transport = _TransportProcess(tmp_path)
    try:
        with _FakeAnthropicServer() as upstream:
            transport.send(_acquire_payload(upstream_url=upstream.url))
            ready = _read_json_line(transport.readiness)
            transport.receipts.close()

            try:
                response = httpx.post(
                    ready["endpoint"] + "/v1/messages",
                    json={
                        "model": "claude-test",
                        "max_tokens": 16,
                        "stream": False,
                        "messages": [{"role": "user", "content": "small request"}],
                    },
                    headers={
                        "authorization": "Bearer fake-oauth-account-a",
                        "user-agent": "claude-cli/test",
                        "anthropic-version": "2023-06-01",
                    },
                )
                assert response.status_code >= 500
            except httpx.TransportError:
                pass
            assert len(upstream.requests) == 1

            transport.send(
                {
                    "schema": "headroom.transport.release.v1",
                    "lease_id": ready["lease_id"],
                }
            )
            assert transport.process.wait(timeout=15) != 0
    finally:
        transport.close()


def test_two_concurrent_worktrees_keep_ports_accounts_and_receipts_isolated(tmp_path) -> None:
    first = _TransportProcess(tmp_path / "first")
    second = _TransportProcess(tmp_path / "second")
    try:
        with _FakeAnthropicServer() as upstream:
            first_payload = _acquire_payload(upstream_url=upstream.url)
            second_payload = _acquire_payload(upstream_url=upstream.url)
            second_payload["run_id"] = "run-02HZZZZZZZZZZZZZZZZZZZZZZZ"
            second_payload["cwd"] = "/private/tmp/worktree-b"
            second_payload["worktree_identity"] = canonical_digest({"label": "worktree-b"})
            second_payload["account"] = {
                "ref": "account://claude/other",
                "revision": canonical_digest({"label": "account-other"}),
                "credential_locator": "keychain://claude/other",
                "provider_mode": "anthropic_oauth_passthrough",
            }
            second_payload["binding_digest"] = AcquireRequest.binding_digest_for(second_payload)
            first.send(first_payload)
            second.send(second_payload)
            first_ready = _read_json_line(first.readiness)
            second_ready = _read_json_line(second.readiness)
            assert first_ready["endpoint"] != second_ready["endpoint"]
            assert first_ready["binding_digest"] != second_ready["binding_digest"]

            def send_request(endpoint: str, token: str) -> httpx.Response:
                return httpx.post(
                    endpoint + "/v1/messages",
                    json={
                        "model": "claude-test",
                        "max_tokens": 16,
                        "stream": False,
                        "messages": [{"role": "user", "content": "small request"}],
                    },
                    headers={
                        "authorization": f"Bearer {token}",
                        "user-agent": "claude-cli/test",
                        "anthropic-version": "2023-06-01",
                    },
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                response_a = pool.submit(
                    send_request, first_ready["endpoint"], "fake-oauth-account-a"
                )
                response_b = pool.submit(
                    send_request, second_ready["endpoint"], "fake-oauth-account-b"
                )
                assert response_a.result(timeout=15).status_code == 200
                assert response_b.result(timeout=15).status_code == 200

            first_receipt = _read_json_line(first.receipts)
            second_receipt = _read_json_line(second.receipts)
            assert first_receipt["account_revision"] == first_payload["account"]["revision"]
            assert second_receipt["account_revision"] == second_payload["account"]["revision"]
            assert {request["authorization"] for request in upstream.requests} == {
                "Bearer fake-oauth-account-a",
                "Bearer fake-oauth-account-b",
            }

            for transport, ready in ((first, first_ready), (second, second_ready)):
                transport.send(
                    {
                        "schema": "headroom.transport.release.v1",
                        "lease_id": ready["lease_id"],
                    }
                )
            assert first.process.wait(timeout=15) == 0
            assert second.process.wait(timeout=15) == 0
    finally:
        first.close()
        second.close()
