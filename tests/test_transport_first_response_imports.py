"""Isolated transport must serve and finalize responses without LiteLLM (CRA-459).

Moving the provider pricing import off startup only helps if the first response
does not pay for it instead. These tests run the real ``transport serve`` child
against a loopback fake provider and a loopback beacon sink, then read an
import-attempt log the child wrote *as each attempt happened*. A broad
``except ImportError`` around an import cannot make an attempt look like
success: the attempt is on disk before the import itself runs.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from headroom.transport.protocol import AcquireRequest, canonical_digest
from tests.test_transport_modes import mode_payload
from tests.test_transport_serve import _read_json_line, _TransportProcess

MODES = (
    "anthropic_oauth_passthrough",
    "openai_oauth_passthrough",
    "openai_aperture_passthrough",
    "bedrock_aperture_passthrough",
)
PUBLIC_MODEL = "claude-sonnet-4-5"
# A private gateway alias: Headroom's Claude context table matches its prefix,
# but it is not a public id, so telemetry must drop it.
PRIVATE_MODEL = "claude-sonnet-4-5-acme-internal"
BEDROCK_MODEL = "anthropic.claude-sonnet-4-5-20250929-v1:0"
_CREDENTIAL_PREFIXES = ("ANTHROPIC_", "OPENAI_", "AWS_", "GEMINI_", "GOOGLE_", "AZURE_")

# Runs in the child before Headroom is imported. Every attempt to import the
# SDK is appended (with its stack) immediately; the summary is written by the
# first-registered atexit hook, which runs after Headroom's own exit flushes.
_SENTINEL_BOOT = textwrap.dedent(
    """
    import atexit, builtins, importlib, json, os, runpy, sys, traceback
    _log = os.environ["HEADROOM_TEST_IMPORT_LOG"]
    _real_import, _real_import_module = builtins.__import__, importlib.import_module

    def _note(name):
        if name == "litellm" or name.startswith("litellm."):
            with open(_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"attempt": name, "stack": traceback.format_stack()}) + "\\n")

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 0:
            _note(name)
        return _real_import(name, globals, locals, fromlist, level)

    def _import_module(name, package=None):
        if not name.startswith("."):
            _note(name)
        return _real_import_module(name, package)

    builtins.__import__ = _import
    importlib.import_module = _import_module

    def _summary():
        with open(_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"summary": {
                "litellm_loaded": "litellm" in sys.modules,
                "pricing_loaded": "headroom.pricing.litellm_pricing" in sys.modules,
                "headroom": sys.modules["headroom"].__file__,
            }}) + "\\n")

    atexit.register(_summary)
    sys.argv = ["headroom", *sys.argv[1:]]
    runpy.run_module("headroom.cli", run_name="__main__", alter_sys=True)
    """
)


def _event_stream_message(payload: bytes) -> bytes:
    """Encode one AWS event-stream message (prelude, headers, payload, CRCs)."""
    headers = {
        ":event-type": "chunk",
        ":content-type": "application/json",
        ":message-type": "event",
    }
    encoded = b"".join(
        bytes([len(name)])
        + name.encode()
        + b"\x07"
        + len(value).to_bytes(2, "big")
        + value.encode()
        for name, value in headers.items()
    )
    prelude = (16 + len(encoded) + len(payload)).to_bytes(4, "big") + len(encoded).to_bytes(
        4, "big"
    )
    message = prelude + zlib.crc32(prelude).to_bytes(4, "big") + encoded + payload
    return message + zlib.crc32(message).to_bytes(4, "big")


def _anthropic_events(model: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg-fake-stream",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "visible streamed output"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 4},
        },
        {"type": "message_stop"},
    ]


def _stream_chunks(path: str, model: str) -> tuple[str, list[bytes]]:
    if path.endswith("/invoke-with-response-stream"):
        return "application/vnd.amazon.eventstream", [
            _event_stream_message(
                json.dumps(
                    {"bytes": base64.b64encode(json.dumps(event).encode()).decode()},
                    separators=(",", ":"),
                ).encode()
            )
            for event in _anthropic_events(model)
        ]
    if path == "/responses":
        events = [
            ("response.created", {"type": "response.created", "response": {"id": "resp-1"}}),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "visible streamed output"},
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"id": "resp-1", "status": "completed"},
                },
            ),
        ]
    else:
        events = [(event["type"], event) for event in _anthropic_events(model)]
    return "text/event-stream", [
        f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()
        for name, data in events
    ]


class _ProviderHandler(BaseHTTPRequestHandler):
    server: _FakeProvider

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
        self.server.requests.append({"path": self.path, "body": body})
        scenario = self.server.scenario
        model = body.get("model") or BEDROCK_MODEL
        if scenario == "fail":
            self._json(500, {"type": "error", "error": {"type": "api_error", "message": "boom"}})
        elif scenario in {"stream", "stall"}:
            content_type, chunks = _stream_chunks(self.path, model)
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.end_headers()
            for chunk in chunks[:1] if scenario == "stall" else chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
            self.server.sent.append(b"".join(chunks))
            if scenario == "stall":
                self._wait_for_client_close()
        elif self.path == "/responses":
            self._json(200, {"id": "resp-1", "status": "completed", "output": []})
        else:
            self._json(
                200,
                {
                    "id": "msg-fake",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [{"type": "text", "text": "visible fake-provider output"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                },
            )

    def _json(self, status: int, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _wait_for_client_close(self) -> None:
        self.connection.settimeout(0.05)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if self.connection.recv(1, socket.MSG_PEEK) == b"":
                    self.server.stall_closed.set()
                    return
            except TimeoutError:
                continue
            except OSError:
                self.server.stall_closed.set()
                return

    def log_message(self, format: str, *args: Any) -> None:
        return


class _FakeProvider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _ProviderHandler)
        self.requests: list[dict[str, Any]] = []
        self.sent: list[bytes] = []
        self.scenario = "json"
        self.stall_closed = threading.Event()
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> _FakeProvider:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=2)


class _SinkHandler(BaseHTTPRequestHandler):
    server: _BeaconSink

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.posts.append(self.rfile.read(int(self.headers.get("content-length", "0"))))
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


class _BeaconSink(_FakeProvider):
    def __init__(self) -> None:
        ThreadingHTTPServer.__init__(self, ("127.0.0.1", 0), _SinkHandler)
        self.posts: list[bytes] = []
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)


def _request(mode: str, *, saving: bool, stream: bool, model: str) -> tuple[str, dict[str, Any]]:
    tool_output = (
        "".join(f"/src/example.py:{line}: repeated matching value\n" for line in range(1, 400))
        if saving
        else "short"
    )
    if mode.startswith("openai_"):
        return "/responses", {
            "model": model,
            "stream": stream,
            "input": [{"type": "function_call_output", "call_id": "call-1", "output": tool_output}],
        }
    messages = [
        {"role": "user", "content": "inspect the search results"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tool-01", "name": "Grep", "input": {"pattern": "x"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tool-01", "content": tool_output}],
        },
    ]
    if mode == "bedrock_aperture_passthrough":
        suffix = "invoke-with-response-stream" if stream else "invoke"
        return f"/model/{BEDROCK_MODEL}/{suffix}", {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 32,
            "messages": messages,
        }
    return "/v1/messages", {
        "model": model,
        "max_tokens": 32,
        "stream": stream,
        "messages": messages,
    }


def _import_log(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    summaries = [record["summary"] for record in records if "summary" in record]
    return [record for record in records if "attempt" in record], (
        summaries[-1] if summaries else None
    )


@pytest.mark.parametrize("beacon", ["on", "off"])
@pytest.mark.parametrize("mode", MODES)
def test_isolated_transport_finalizes_responses_without_litellm(tmp_path, mode, beacon) -> None:
    import_log = tmp_path / "imports.jsonl"
    import_log.touch()
    transport_root = tmp_path / "transport"
    with _FakeProvider() as upstream, _BeaconSink() as sink:
        transport = _TransportProcess(
            transport_root,
            launcher=("-c", _SENTINEL_BOOT),
            extra_env={
                # Synthetic credentials only: nothing from the developer's shell.
                **{name: None for name in os.environ if name.startswith(_CREDENTIAL_PREFIXES)},
                "HEADROOM_TEST_IMPORT_LOG": str(import_log),
                "HEADROOM_BEACON": beacon,
                "HEADROOM_TELEMETRY_ENDPOINT": sink.url + "/v1/logs",
                "DO_NOT_TRACK": "",
                "HEADROOM_OFFLINE": "",
            },
        )
        try:
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
            headers = {"authorization": "Bearer fake-oauth-account", "user-agent": "claude-cli/t"}
            endpoint = ready["endpoint"]
            receipts = []

            # Nonzero savings, then zero savings (with an unlisted private name).
            upstream.scenario = "json"
            path, body = _request(mode, saving=True, stream=False, model=PUBLIC_MODEL)
            response = httpx.post(endpoint + path, json=body, headers=headers, timeout=30)
            assert response.status_code == 200
            receipts.append(_read_json_line(transport.receipts))
            assert receipts[-1]["outcome"] == "succeeded"
            assert receipts[-1]["saved_tokens"] > 0
            assert len(json.dumps(upstream.requests[-1]["body"])) < len(json.dumps(body))

            path, body = _request(mode, saving=False, stream=False, model=PRIVATE_MODEL)
            response = httpx.post(endpoint + path, json=body, headers=headers, timeout=30)
            assert response.status_code == 200
            receipts.append(_read_json_line(transport.receipts))
            assert receipts[-1]["outcome"] == "succeeded"
            assert receipts[-1]["saved_tokens"] == 0

            # Streaming completion: the client receives the provider bytes intact.
            upstream.scenario = "stream"
            path, body = _request(mode, saving=True, stream=True, model=PUBLIC_MODEL)
            with httpx.stream(
                "POST", endpoint + path, json=body, headers=headers, timeout=30
            ) as streamed:
                assert streamed.status_code == 200
                raw = b"".join(streamed.iter_raw())
            receipts.append(_read_json_line(transport.receipts))
            assert receipts[-1]["outcome"] == "succeeded"
            if mode == "bedrock_aperture_passthrough":
                assert raw == upstream.sent[-1], "event-stream bytes must pass through unchanged"
                eventstream = pytest.importorskip("botocore.eventstream")
                buffer = eventstream.EventStreamBuffer()
                buffer.add_data(raw)
                decoded = [
                    json.loads(base64.b64decode(json.loads(message.payload)["bytes"]))
                    for message in buffer
                ]
                assert [event["type"] for event in decoded][-1] == "message_stop"
            else:
                assert b"visible streamed output" in raw

            # Upstream failure is a failed receipt, not a fallback.
            upstream.scenario = "fail"
            path, body = _request(mode, saving=True, stream=False, model=PUBLIC_MODEL)
            response = httpx.post(endpoint + path, json=body, headers=headers, timeout=30)
            assert response.status_code == 500
            receipts.append(_read_json_line(transport.receipts))
            assert receipts[-1]["outcome"] == "failed"

            # Client cancellation mid-stream releases the upstream connection.
            upstream.scenario = "stall"
            path, body = _request(mode, saving=True, stream=True, model=PUBLIC_MODEL)
            with httpx.stream(
                "POST", endpoint + path, json=body, headers=headers, timeout=30
            ) as stalled:
                assert stalled.status_code == 200
                assert next(stalled.iter_raw())
            receipts.append(_read_json_line(transport.receipts))
            if mode != "anthropic_oauth_passthrough":
                # The forwarder classifies an unfinished stream as 499. The
                # Anthropic handler reports the upstream status instead; that
                # classification predates and is outside this regression.
                assert receipts[-1]["outcome"] == "failed"
            assert upstream.stall_closed.wait(10), "upstream stream was not released"

            assert [receipt["cursor"] for receipt in receipts] == [1, 2, 3, 4, 5]
            transport.send(
                {"schema": "headroom.transport.release.v1", "lease_id": ready["lease_id"]}
            )
            assert transport.process.wait(timeout=30) == 0
            leftovers = sorted(
                str(path.relative_to(transport_root)) for path in transport_root.rglob("*")
            )
            if beacon == "on" and mode == "anthropic_oauth_passthrough":
                # Predates CRA-459 (the unchanged baseline leaves it too): the
                # beacon's exit flush persists an install id after stateless
                # cleanup has removed the config directory.
                assert leftovers == ["config", "config/install_id"]
            else:
                assert leftovers == []
        finally:
            transport.close()

    attempts, summary = _import_log(import_log)
    assert summary is not None, "child did not reach its exit hook"
    assert summary["headroom"].startswith(str(Path(__file__).resolve().parents[1]))
    assert attempts == [], "LiteLLM import attempted:\n" + "".join(attempts[0]["stack"])
    assert summary == {**summary, "litellm_loaded": False, "pricing_loaded": False}

    wire = b"".join(sink.posts)
    assert b"acme" not in wire
    assert b"fake-oauth-account" not in wire
    if beacon == "off" or mode != "anthropic_oauth_passthrough":
        assert sink.posts == []
    else:
        sessions = [
            {attr["key"]: attr["value"] for attr in record["body"]["kvlistValue"]["values"]}
            for post in sink.posts
            for resource in json.loads(post)["resourceLogs"]
            for scope in resource["scopeLogs"]
            for record in scope["logRecords"]
        ]
        assert sessions, "beacon on: the shutdown flush must reach the loopback sink"
        models = [
            value["stringValue"]
            for session in sessions
            for value in session["models"]["arrayValue"].get("values", [])
        ]
        assert models == [PUBLIC_MODEL]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "CRA-459 follow-up: a model id absent from Headroom's Claude context table "
        "reaches litellm.get_model_info in AnthropicProvider.get_context_limit while "
        "the request is being compressed. Moving context limits to the pinned catalog "
        "is outside the approved telemetry/savings scope and needs a decision."
    ),
)
def test_unknown_model_context_limit_lookup_does_not_import_litellm(tmp_path) -> None:
    script = textwrap.dedent(
        """
        import json, sys
        from headroom.providers.anthropic import AnthropicProvider
        limit = AnthropicProvider(warn=False).get_context_limit("acme-internal-llama")
        print(json.dumps({"limit": limit, "litellm": "litellm" in sys.modules}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "HEADROOM_CONFIG_DIR": str(tmp_path)},
    )
    assert json.loads(result.stdout) == {"limit": 128000, "litellm": False}
