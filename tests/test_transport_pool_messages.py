"""Behaviour check: Claude /v1/messages through a pooled lease's forwarder.

A single-lease transport serves Claude's /v1/messages with the proxy app's
handler; a pooled lease forwards it through its own binding (the app's handler
reads a process-wide upstream) with its own copy of the handler's per-session
prefix replay. This compares what one recording upstream receives from each
path for the same requests.

Same on both paths: method, path, every forwarded request header except
accept-encoding, the client response bytes, and the forwarded body bytes on
every turn of a five-turn conversation, including the turn where the router
would otherwise reclassify an older tool result.

Known differences, asserted below so a change to either is noticed:
- The forwarder keeps the request's query string (Claude sends ``?beta=true``);
  the app's handler drops it.
- The forwarder asks the upstream for ``accept-encoding: identity`` and relays
  the response bytes untouched; the app's handler forwards the client's value.
- Request bodies without ``model`` or ``messages`` are refused locally (400,
  ``invalid_request_body`` receipt) instead of being passed through.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import httpx

from tests.test_transport_pool import Lease, RecordingUpstream, _pool, _ready_lease
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
# The turn at which the router, recompressing statelessly, would send the
# turn-1 JSON tool result (message 4) uncompressed again.
DRIFT_TURN = 3
DRIFTING_MESSAGE = 4


def _search_output(index: int) -> str:
    return "".join(
        f"/src/mod{index}.py:{line}: repeated matching value {index}\n" for line in range(1, 300)
    )


def _json_output(index: int) -> str:
    return json.dumps(
        [
            {
                "id": k,
                "name": f"item{k}-{index}",
                "status": "ok",
                "tags": ["a", "b"],
                "score": k % 7,
            }
            for k in range(200)
        ]
    )


def _conversation(turns: int) -> list[dict[str, Any]]:
    """Claude's pattern: each turn appends a tool result and moves the breakpoint to it."""

    messages: list[dict[str, Any]] = [{"role": "user", "content": "start"}]
    requests = []
    for index in range(turns):
        output = _search_output(index) if index % 2 == 0 else _json_output(index)
        messages = [
            *messages,
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": f"t{index}", "name": "Grep", "input": {"q": index}}
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": f"t{index}", "content": output}],
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
STREAMED = {**_conversation(2)[1], "system": "streamed conversation", "stream": True}
CASES: list[tuple[str, dict[str, Any], str]] = [
    ("small", SMALL, "?beta=true"),
    ("stream", STREAMED, ""),
    *[(f"turn-{index}", body, "") for index, body in enumerate(_conversation(5))],
]


def _send(endpoint: str, body: dict[str, Any], query: str = "") -> httpx.Response:
    response = httpx.post(
        endpoint + "/v1/messages" + query,
        content=json.dumps(body).encode(),
        headers=HEADERS,
        timeout=60,
    )
    assert response.status_code == 200, response.text
    return response


def _drive(endpoint: str, upstream: RecordingUpstream) -> dict[str, dict[str, Any]]:
    observed = {}
    for name, body, query in CASES:
        before = len(upstream.requests)
        response = _send(endpoint, body, query)
        assert len(upstream.requests) == before + 1
        observed[name] = {"upstream": upstream.requests[before], "client": response.content}
    return observed


def _forwarded(upstream: RecordingUpstream, lease: Lease, body: dict[str, Any]) -> dict:
    before = len(upstream.requests)
    _send(lease.endpoint, body)
    return upstream.requests[before]["body"]


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
        assert forwarded_headers == app_headers, name
        assert served[name]["client"] == pooled[name]["client"], name
        # Byte for byte the body the app's handler forwards, on every turn.
        assert forwarder["raw"] == app["raw"], name

    # Compressed, and every earlier message forwarded identically next turn.
    turns = [pooled[f"turn-{index}"]["upstream"]["body"]["messages"] for index in range(5)]
    assert len(json.dumps(turns[0])) < len(json.dumps(_conversation(1)[0]["messages"]))
    for previous, current in zip(turns, turns[1:]):
        assert _strip_breakpoints(current[: len(previous)]) == _strip_breakpoints(previous)


def _strip_breakpoints(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_breakpoints(item) for key, item in value.items() if key != "cache_control"
        }
    if isinstance(value, list):
        return [_strip_breakpoints(item) for item in value]
    return value


def test_replay_state_is_per_lease_and_released_with_it(tmp_path) -> None:
    conversation = _conversation(DRIFT_TURN + 1)
    drifting_client = conversation[DRIFT_TURN]["messages"][DRIFTING_MESSAGE]
    with RecordingUpstream() as upstream, _pool(tmp_path) as pool:
        first = _ready_lease(pool, upstream, "first")
        second = _ready_lease(pool, upstream, "second")
        first.activate()
        second.activate()

        replayed = [_forwarded(upstream, first, body) for body in conversation]
        # The first lease's own history keeps the message as it forwarded it.
        assert (
            replayed[DRIFT_TURN]["messages"][DRIFTING_MESSAGE]
            == replayed[DRIFT_TURN - 1]["messages"][DRIFTING_MESSAGE]
        )
        assert replayed[DRIFT_TURN - 1]["messages"][DRIFTING_MESSAGE] != drifting_client

        # The same conversation (same session id) arriving cold on another
        # lease gets nothing from the first lease's state: no replay, so the
        # router's cold classification stands.
        cold = _forwarded(upstream, second, conversation[DRIFT_TURN])
        assert _strip_breakpoints(cold["messages"][DRIFTING_MESSAGE]) == _strip_breakpoints(
            drifting_client
        )
        assert cold != replayed[DRIFT_TURN]

        first.release()
        assert first.ended()
        events = list(first.diagnostics.remaining())
        released = [event for event in events if event["event"] == "replay_state_released"]
        assert len(released) == 1
        assert released[0]["sessions"] == 1
        assert released[0]["trackers"] >= 1
        # The lease's detection-memo partition (CRA-1001) ends with it too.
        memo_released = [event for event in events if event["event"] == "detection_memo_released"]
        assert len(memo_released) == 1
        # Its compressions detected content under its own scope, and the
        # release dropped those verdicts (no lease ever shares the partition).
        assert memo_released[0]["entries"] >= 1

        second.control.close()
        assert second.ended()
        assert [
            event["sessions"]
            for event in second.diagnostics.remaining()
            if event["event"] == "replay_state_released"
        ] == [1]
