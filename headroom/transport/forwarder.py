"""Request-side compression with byte-preserving isolated forwarding."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from headroom.cache.prefix_tracker import _strip_cache_control, normalize_message_cache_control
from headroom.proxy.helpers import COMPRESSION_TIMEOUT_SECONDS, read_request_json_with_bytes
from headroom.proxy.outcome import RequestOutcome

from .protocol import AcquireRequest, ReceiptWriter
from .replay import MAX_CAPTURED_RESPONSE_BYTES, LeaseReplayState, ReplayTurn, parse_response

# The route kind of Claude's /v1/messages. A single-lease transport serves it
# through the proxy app; a pooled lease, whose upstream must come from its own
# binding rather than the app's process-wide upstream, forwards it here.
ANTHROPIC_MESSAGES = "anthropic_messages"


class _FinalizingResponse(StreamingResponse):
    def __init__(
        self,
        *args: Any,
        finalize: Callable[[], Awaitable[None]],
        on_body_sent: Callable[[bytes], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.finalize = finalize
        self.on_body_sent = on_body_sent

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def tracked_send(message: Message) -> None:
            await send(message)
            if self.on_body_sent and message["type"] == "http.response.body":
                self.on_body_sent(message.get("body", b""))

        try:
            await super().__call__(scope, receive, tracked_send)
        finally:
            # Disconnect can happen before the response iterator starts.
            await asyncio.shield(self.finalize())


def route(binding: AcquireRequest, request: Request) -> tuple[str, str] | None:
    path = request.url.path
    if (
        binding.account.provider_mode == "bedrock_aperture_passthrough"
        and request.method == "GET"
        and re.fullmatch(r"/inference-profiles(?:/[^/]+)?", path)
    ):
        return path, "metadata"
    if (
        binding.account.provider_mode == "anthropic_oauth_passthrough"
        and request.method == "POST"
        and path == "/v1/messages/count_tokens"
    ):
        return path, "metadata"
    if (
        binding.account.provider_mode == "anthropic_oauth_passthrough"
        and request.method == "POST"
        and path == "/v1/messages"
    ):
        return path, ANTHROPIC_MESSAGES
    if binding.account.provider_mode.startswith("openai_"):
        if request.method == "GET" and path in {"/models", "/v1/models"}:
            return "/models", "metadata"
        if request.method == "POST" and path in {
            "/responses",
            "/v1/responses",
            "/responses/compact",
            "/v1/responses/compact",
        }:
            return path.removeprefix("/v1"), "responses"
        # Pi's ChatGPT provider always posts `<baseUrl>/codex/responses`; the
        # ChatGPT upstream already ends in `/codex`, so it is the same route.
        if (
            binding.account.provider_mode == "openai_oauth_passthrough"
            and request.method == "POST"
            and path in {"/codex/responses", "/v1/codex/responses"}
        ):
            return "/responses", "responses"
        return None
    match = re.fullmatch(r"/model/(.+)/(invoke|invoke-with-response-stream|count-tokens)", path)
    if request.method == "POST" and match:
        return f"/model/{quote(match[1], safe='')}/{match[2]}", "metadata" if match[
            2
        ] == "count-tokens" else match[1]
    return None


async def forward(
    binding: AcquireRequest,
    writer: ReceiptWriter,
    proxy: Any,
    request: Request,
    replay: LeaseReplayState | None = None,
) -> JSONResponse | StreamingResponse:
    """Compress and forward one request on its binding.

    With ``replay`` (a pooled lease's own conversation state), Claude's
    /v1/messages is compressed as the proxy app's token-mode handler does it:
    cached compressions swapped in, the previous turn's forwarded prefix
    replayed over drift, and the turn recorded after the response.
    """
    selected = route(binding, request)
    if selected is None:
        await writer.record_rejection(reason="unsupported_transport_route", bypass=False)
        return JSONResponse({"error": "unsupported_transport_route"}, status_code=404)
    suffix, model = selected
    metadata = model == "metadata"
    anthropic_messages = model == ANTHROPIC_MESSAGES
    started = time.monotonic()
    request_id = await proxy._next_request_id()
    try:
        body, original = (
            ({}, b"") if request.method == "GET" else await read_request_json_with_bytes(request)
        )
        if not isinstance(body, dict):
            raise ValueError("expected object")
        if metadata:
            messages, slots = [], []
            pipeline = proxy.openai_pipeline
        elif binding.account.provider_mode.startswith("openai_"):
            requested_model = body.get("model")
            if not isinstance(requested_model, str) or not requested_model:
                raise ValueError("missing model")
            model = requested_model
            items = body.get("input")
            if not isinstance(items, (str, list)):
                raise ValueError("missing input")
            output_items = items if isinstance(items, list) else []
            # Preserve every protocol item and all non-tool text. Only mutable
            # string function results enter the existing lossless pipeline.
            slots = (
                [
                    index
                    for index, item in enumerate(items)
                    if isinstance(item, dict)
                    and item.get("type") == "function_call_output"
                    and isinstance(item.get("output"), str)
                ]
                if isinstance(items, list)
                else []
            )
            messages = [
                {
                    "role": "tool",
                    "content": output_items[index]["output"],
                    "tool_call_id": output_items[index].get("call_id", ""),
                }
                for index in slots
            ]
            pipeline = proxy.openai_pipeline
        else:
            if binding.account.provider_mode == "anthropic_oauth_passthrough":
                # Claude's /v1/messages names its model in the body.
                requested_model = body.get("model")
                if not isinstance(requested_model, str) or not requested_model:
                    raise ValueError("missing model")
                model = requested_model
            requested_messages = body.get("messages")
            if not isinstance(requested_messages, list) or not requested_messages:
                raise ValueError("missing messages")
            messages = requested_messages
            slots = []
            pipeline = proxy.anthropic_pipeline
    except Exception:
        await writer.record_rejection(reason="invalid_request_body", bypass=False)
        return JSONResponse({"error": "invalid_request_body"}, status_code=400)

    before = after = 0
    transforms: tuple[str, ...] = ()
    outbound = original
    turn: ReplayTurn | None = None
    forwarded_messages: list[dict[str, Any]] = []
    try:
        if messages and anthropic_messages and replay is not None:
            pristine = copy.deepcopy(messages)
            turn, forwarded_messages, before, after, transforms = await _compress_with_replay(
                replay, proxy, pipeline, request, body, messages, model, request_id
            )
            if forwarded_messages != pristine:
                changed = copy.deepcopy(body)
                changed["messages"] = forwarded_messages
                outbound = json.dumps(changed, ensure_ascii=False, separators=(",", ":")).encode()
            else:
                after = before
        elif messages:
            # Claude moves its cache breakpoint to the newest block every turn,
            # and the router never compresses a block that carries one. Left in
            # place, each block would go uncompressed on its first turn and
            # compressed on the next, rewriting the cached prefix every turn.
            # Compress the content without breakpoints and put the client's
            # breakpoints back afterwards, so each block is forwarded the same
            # way on every turn.
            compression_input = _strip_cache_control(messages) if anthropic_messages else messages
            result = await proxy._run_compression_in_executor(
                lambda: pipeline.apply(
                    messages=copy.deepcopy(compression_input),
                    model=model,
                    model_limit=200_000,
                    request_id=request_id,
                    protect_recent=0,
                ),
                timeout=COMPRESSION_TIMEOUT_SECONDS,
            )
            before, after = result.tokens_before, result.tokens_after
            if before < 0 or after < 0:
                raise ValueError("invalid compression accounting")
            if after <= before and result.messages != compression_input:
                changed = copy.deepcopy(body)
                if binding.account.provider_mode.startswith("openai_"):
                    if len(result.messages) != len(slots):
                        raise ValueError("compression changed protocol item count")
                    for index, message in zip(slots, result.messages, strict=True):
                        if not isinstance(message.get("content"), str):
                            raise ValueError("compression changed output shape")
                        changed["input"][index]["output"] = message["content"]
                elif anthropic_messages:
                    changed["messages"] = normalize_message_cache_control(
                        result.messages, None, client_messages=messages
                    )
                else:
                    changed["messages"] = result.messages
                outbound = json.dumps(changed, ensure_ascii=False, separators=(",", ":")).encode()
                transforms = tuple(result.transforms_applied)
            else:
                after = before
    except Exception:
        await writer.record_rejection(reason="compression_failed", bypass=False)
        return JSONResponse({"error": "compression_failed"}, status_code=503)

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower()
        not in {
            "host",
            "content-length",
            "content-encoding",
            "transfer-encoding",
            "connection",
            "accept-encoding",
        }
        and not key.lower().startswith("x-headroom-")
    }
    headers["content-type"] = "application/json"
    headers["accept-encoding"] = "identity"
    url = binding.upstream.url + suffix
    if request.url.query:
        url += "?" + request.url.query
    try:
        upstream = await proxy.http_client.send(
            proxy.http_client.build_request(request.method, url, headers=headers, content=outbound),
            stream=True,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        await writer.record_rejection(reason="upstream_request_error", bypass=False)
        return JSONResponse({"error": "upstream_request_error"}, status_code=502)

    if 300 <= upstream.status_code < 400:
        await upstream.aclose()
        await writer.record_rejection(
            reason="upstream_redirect_forbidden", bypass=False, upstream_reached=True
        )
        return JSONResponse({"error": "upstream_redirect_forbidden"}, status_code=502)

    completed = False
    captured: bytearray | None = bytearray() if turn is not None else None
    terminal_line = b""
    terminal_line_overflow = False
    terminal_event = False

    def on_body_sent(chunk: bytes) -> None:
        nonlocal completed, terminal_line, terminal_line_overflow, terminal_event
        if metadata or not binding.account.provider_mode.startswith("openai_"):
            return
        # Observe only bytes successfully delivered to the native client. Retain
        # a bounded line prefix, not the (potentially huge) completed-event data.
        for part_index, part in enumerate(chunk.split(b"\n")):
            if part_index:
                line = terminal_line.rstrip(b"\r")
                if not terminal_line_overflow:
                    if line.startswith(b"event:"):
                        terminal_event = line[6:].strip() == b"response.completed"
                    elif not line:
                        if terminal_event:
                            completed = True
                        terminal_event = False
                terminal_line = b""
                terminal_line_overflow = False
            if len(terminal_line) + len(part) > 80:
                terminal_line_overflow = True
            terminal_line = (terminal_line + part[:81])[:81]

    finalized = False
    finalization_lock = asyncio.Lock()

    async def finalize() -> None:
        nonlocal finalized
        async with finalization_lock:
            if finalized:
                return
            finalized = True
            try:
                await upstream.aclose()
                if (
                    turn is not None
                    and replay is not None
                    and captured is not None
                    and completed
                    and 200 <= upstream.status_code < 300
                ):
                    replay.record_response(
                        turn,
                        forwarded=forwarded_messages,
                        original=messages,
                        response=parse_response(
                            bytes(captured), upstream.headers.get("content-type", ""), proxy
                        ),
                    )
            finally:
                await writer.record(
                    RequestOutcome(
                        request_id=request_id,
                        provider=binding.account.provider_mode,
                        model=model,
                        original_tokens=before,
                        optimized_tokens=after,
                        output_tokens=0,
                        tokens_saved=before - after,
                        attempted_input_tokens=before,
                        transforms_applied=transforms,
                        total_latency_ms=(time.monotonic() - started) * 1000,
                        status_code=upstream.status_code if completed else 499,
                    ),
                    request_kind="metadata" if metadata else "inference",
                )

    async def stream_bytes() -> AsyncIterator[bytes]:
        nonlocal completed, captured
        try:
            async for chunk in upstream.aiter_raw():
                if captured is not None:
                    if len(captured) + len(chunk) <= MAX_CAPTURED_RESPONSE_BYTES:
                        captured.extend(chunk)
                    else:
                        # Too large to record; the previous turn's state stays.
                        captured = None
                yield chunk
            completed = True
        finally:
            await asyncio.shield(finalize())

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}
    }
    return _FinalizingResponse(
        stream_bytes(),
        status_code=upstream.status_code,
        headers=response_headers,
        finalize=finalize,
        on_body_sent=on_body_sent,
    )


async def _compress_with_replay(
    replay: LeaseReplayState,
    proxy: Any,
    pipeline: Any,
    request: Request,
    body: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    request_id: str,
) -> tuple[ReplayTurn, list[dict[str, Any]], int, int, tuple[str, ...]]:
    """Token-mode compression of Claude's messages, mirroring the app handler.

    The same steps, in the same order, as the proxy's Anthropic token-mode
    path: prepare_turn (cached compressions in, provider-confirmed prefix
    frozen), the pipeline with the same arguments, the compression cache
    update, finalize_turn (previous forwarded prefix replayed over drift), the
    inflation guard, and the client's breakpoints re-placed.
    """

    from headroom.agent_savings import proxy_pipeline_kwargs
    from headroom.proxy.auth_mode import classify_auth_mode
    from headroom.proxy.handlers.anthropic import _strip_streaming_only_content_fields
    from headroom.proxy.session_engine import (
        FREEZE_POLICY_CONFIRMED_CLAMP,
        finalize_turn,
        prepare_turn,
    )
    from headroom.transforms.cold_prefix import anthropic_cache_ttl_seconds
    from headroom.transforms.compression_policy import resolve_policy
    from headroom.utils import extract_user_query

    # As the app handler: canonicalize in place before anything is tracked.
    _strip_streaming_only_content_fields(messages)
    client_messages = copy.deepcopy(messages)
    turn = replay.begin(request, model, body, client_messages)
    confirmed_frozen = max(int(turn.frozen_message_count or 0), 0)
    prep = prepare_turn(
        turn.compression_cache,
        messages,
        policy=FREEZE_POLICY_CONFIRMED_CLAMP,
        tracker_frozen=turn.frozen_message_count,
    )
    working = prep.pipeline_input
    context_limit = proxy.anthropic_provider.get_context_limit(model)
    compression_policy = resolve_policy(classify_auth_mode(request.headers))
    cache_ttl = anthropic_cache_ttl_seconds(model, client_messages, body.get("system"))
    result = await proxy._run_compression_in_executor(
        lambda: pipeline.apply(
            messages=working,
            model=model,
            model_limit=context_limit,
            context=extract_user_query(working),
            frozen_message_count=prep.frozen_message_count,
            prefix_replay_guaranteed=True,
            idle_seconds=turn.idle_seconds,
            biases=None,
            request_id=request_id,
            compression_policy=compression_policy,
            cache_ttl_seconds=cache_ttl,
            **proxy_pipeline_kwargs(proxy.config),
        ),
        timeout=COMPRESSION_TIMEOUT_SECONDS,
    )
    if result.messages != working:
        turn.compression_cache.update_from_result(messages, result.messages)
    before, after = result.tokens_before, result.tokens_after
    if before < 0 or after < 0:
        raise ValueError("invalid compression accounting")
    final = finalize_turn(
        result.messages,
        client_messages,
        turn.previous_original,
        turn.previous_forwarded,
        confirmed_frozen_count=confirmed_frozen,
    )
    forwarded = final.messages
    if after > before and not final.replayed:
        # The app handler's inflation guard: never forward a larger form.
        forwarded, after = client_messages, before
    forwarded = normalize_message_cache_control(
        forwarded, turn.previous_forwarded, client_messages=client_messages
    )
    return turn, forwarded, before, after, tuple(result.transforms_applied)
