"""Behaviour check: Claude /v1/messages through a pooled lease's forwarder.

A single-lease transport serves Claude's /v1/messages with the proxy app's
handler; a pooled lease forwards it through its own binding (the app's handler
reads a process-wide upstream). This compares what one recording upstream
receives from each path for the same requests.

Same on both paths: method, path, every forwarded request header except
accept-encoding, the client's body bytes when nothing compresses, the
compressed message content, and the client's breakpoint positions.

Known differences, asserted below so a change to either is noticed:
- The forwarder keeps the request's query string (Claude sends ``?beta=true``);
  the app's handler drops it.
- The forwarder asks the upstream for ``accept-encoding: identity`` and relays
  the response bytes untouched; the app's handler forwards the client's value.
- The app's handler adds a ``cache_control`` breakpoint to a block it
  compressed; the forwarder forwards the client's breakpoints only.
- Across turns, the app's handler replays the previous turn's forwarded prefix
  byte for byte. The forwarder recompresses each request; it is prefix-stable
  for the usual pattern (asserted here), but the router can classify an older
  tool result differently once the conversation grows, which then changes that
  block's bytes (and so the prompt cache) on that turn.
- Request bodies without ``model`` or ``messages`` are refused locally (400,
  ``invalid_request_body`` receipt) instead of being passed through.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx

from tests.test_transport_pool import RecordingUpstream, _pool, _ready_lease
from tests.test_transport_protocol import _acquire_payload
from tests.test_transport_serve import _read_json_line, _TransportProcess

HEADERS = {
    "authorization": "Bearer fake-oauth",
    "user-agent": "claude-cli/test",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20,interleaved-thinking-2025-05-14",
    "x-app": "cli",
    "accept": "application/json",
    "content-type": "application/json",
}


def _tool_output(index: int) -> str:
    return "".join(
        f"/src/mod{index}.py:{line}: repeated matching value\n" for line in range(1, 300)
    )


def _conversation(turns: int) -> list[dict[str, Any]]:
    """Claude's pattern: each turn appends a tool result and moves the breakpoint to it."""

    messages: list[dict[str, Any]] = [{"role": "user", "content": "inspect the results"}]
    requests = []
    for index in range(turns):
        messages = [
            *messages,
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": f"t{index}", "name": "Grep", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"t{index}",
                        "content": _tool_output(index),
                    }
                ],
            },
        ]
        current = json.loads(json.dumps(messages))
        current[-1]["content"][0]["cache_control"] = {"type": "ephemeral"}
        requests.append(
            {"model": "claude-test", "max_tokens": 16, "stream": False, "messages": current}
        )
    return requests


SMALL = {
    "model": "claude-test",
    "max_tokens": 16,
    "stream": False,
    "system": [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}],
    "messages": [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}],
        }
    ],
}
CASES: list[tuple[str, dict[str, Any], str]] = [
    ("small", SMALL, "?beta=true"),
    ("stream", {**_conversation(1)[0], "stream": True}, ""),
    *[(f"turn-{index}", body, "") for index, body in enumerate(_conversation(3))],
]


def _drive(endpoint: str, upstream: RecordingUpstream) -> dict[str, dict[str, Any]]:
    observed = {}
    for name, body, query in CASES:
        before = len(upstream.requests)
        response = httpx.post(
            endpoint + "/v1/messages" + query,
            content=json.dumps(body).encode(),
            headers=HEADERS,
            timeout=60,
        )
        assert response.status_code == 200, (name, response.text)
        assert len(upstream.requests) == before + 1
        observed[name] = {"upstream": upstream.requests[before], "client": response.content}
    return observed


def _without_breakpoints(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_breakpoints(item) for key, item in value.items() if key != "cache_control"
        }
    if isinstance(value, list):
        return [_without_breakpoints(item) for item in value]
    return value


def _breakpoints(messages: list[dict[str, Any]]) -> list[int]:
    return [
        index
        for index, message in enumerate(messages)
        if isinstance(message["content"], list)
        and any("cache_control" in block for block in message["content"])
    ]


def test_claude_messages_through_the_forwarder_match_the_app_handler(tmp_path) -> None:
    with RecordingUpstream() as upstream:
        single = _TransportProcess(tmp_path / "serve")
        threading.Thread(target=single.process.stderr.read, daemon=True).start()
        threading.Thread(target=single.process.stdout.read, daemon=True).start()
        try:
            single.send(_acquire_payload(upstream_url=upstream.url))
            ready = _read_json_line(single.readiness)
            served = _drive(ready["endpoint"], upstream)
        finally:
            single.close()
        with _pool(tmp_path / "pool") as pool:
            lease = _ready_lease(pool, upstream, "a")
            lease.activate()
            pooled = _drive(lease.endpoint, upstream)

    for name, _, query in CASES:
        app, forwarder = served[name]["upstream"], pooled[name]["upstream"]
        assert app["path"] == "/v1/messages", name
        assert forwarder["path"] == "/v1/messages" + query, name
        app_headers = dict(app["headers"])
        forwarded_headers = dict(forwarder["headers"])
        assert forwarded_headers.pop("accept-encoding") == "identity"
        app_headers.pop("accept-encoding")
        app_headers.pop("content-length")
        forwarded_headers.pop("content-length")
        assert forwarded_headers == app_headers, name
        assert served[name]["client"] == pooled[name]["client"], name
        # Same compressed content, same client breakpoint positions.
        assert _without_breakpoints(forwarder["body"]) == _without_breakpoints(app["body"]), name
        assert _breakpoints(forwarder["body"]["messages"]) == _breakpoints(
            app["body"]["messages"]
        ), name

    # Nothing compressible: the forwarder relays the client's bytes untouched.
    assert pooled["small"]["upstream"]["raw"] == json.dumps(SMALL).encode()
    assert served["small"]["upstream"]["body"] == SMALL
    # Compressed, and every earlier block forwarded identically on the next turn.
    turns = [pooled[f"turn-{index}"]["upstream"]["body"]["messages"] for index in range(3)]
    assert len(json.dumps(turns[0])) < len(json.dumps(_conversation(1)[0]["messages"]))
    for previous, current in zip(turns, turns[1:]):
        assert _without_breakpoints(current[: len(previous)]) == _without_breakpoints(previous)
