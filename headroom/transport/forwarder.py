"""Request-side compression with byte-preserving isolated forwarding."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from headroom.proxy.helpers import COMPRESSION_TIMEOUT_SECONDS, read_request_json_with_bytes
from headroom.proxy.outcome import RequestOutcome

from .protocol import AcquireRequest, ReceiptWriter


class _FinalizingResponse(StreamingResponse):
    def __init__(self, *args, finalize, on_body_sent=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.finalize = finalize
        self.on_body_sent = on_body_sent

    async def __call__(self, scope, receive, send):
        async def tracked_send(message):
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
):
    selected = route(binding, request)
    if selected is None:
        await writer.record_rejection(reason="unsupported_transport_route", bypass=False)
        return JSONResponse({"error": "unsupported_transport_route"}, status_code=404)
    suffix, model = selected
    metadata = model == "metadata"
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
            model = body.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError("missing model")
            items = body.get("input")
            if not isinstance(items, (str, list)):
                raise ValueError("missing input")
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
                    "content": items[index]["output"],
                    "tool_call_id": items[index].get("call_id", ""),
                }
                for index in slots
            ]
            pipeline = proxy.openai_pipeline
        else:
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("missing messages")
            slots = []
            pipeline = proxy.anthropic_pipeline
    except Exception:
        await writer.record_rejection(reason="invalid_request_body", bypass=False)
        return JSONResponse({"error": "invalid_request_body"}, status_code=400)

    before = after = 0
    transforms: tuple[str, ...] = ()
    outbound = original
    try:
        if messages:
            result = await proxy._run_compression_in_executor(
                lambda: pipeline.apply(
                    messages=copy.deepcopy(messages),
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
            if after <= before and result.messages != messages:
                changed = copy.deepcopy(body)
                if binding.account.provider_mode.startswith("openai_"):
                    if len(result.messages) != len(slots):
                        raise ValueError("compression changed protocol item count")
                    for index, message in zip(slots, result.messages, strict=True):
                        if not isinstance(message.get("content"), str):
                            raise ValueError("compression changed output shape")
                        changed["input"][index]["output"] = message["content"]
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
    terminal_line = b""
    terminal_line_overflow = False
    terminal_event = False

    def on_body_sent(chunk):
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

    async def finalize():
        nonlocal finalized
        async with finalization_lock:
            if finalized:
                return
            finalized = True
            try:
                await upstream.aclose()
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

    async def stream_bytes():
        nonlocal completed
        try:
            async for chunk in upstream.aiter_raw():
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
