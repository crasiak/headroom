"""Runtime boundary for one isolated Headroom proxy per prepared launch."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from collections.abc import Mapping
from contextlib import suppress
from typing import Any, TextIO

import uvicorn
from fastapi import FastAPI

from headroom.proxy.models import ProxyConfig
from headroom.proxy.server import create_app

from .protocol import (
    READY_SCHEMA,
    AcquireRequest,
    ProcessIdentity,
    ProtocolError,
    ReceiptWriter,
    current_process_identity,
)

MAX_CONTROL_RECORD_BYTES = 64 * 1024
RELEASE_SCHEMA = "headroom.transport.release.v1"


class TransportLease:
    """An exact, idempotently releasable lifecycle lease."""

    def __init__(self, lease_id: str) -> None:
        self.lease_id = lease_id
        self.released = False

    def release(self, lease_id: str) -> bool:
        if lease_id != self.lease_id:
            raise ValueError("lease identity mismatch")
        if self.released:
            return False
        self.released = True
        return True


class _ControlChannel:
    def __init__(self, control_socket: socket.socket) -> None:
        self.socket = control_socket
        self.socket.setblocking(False)
        self.buffer = bytearray()

    async def read_record(self) -> Mapping[str, Any]:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                if not raw:
                    raise ProtocolError("control record must not be empty")
                try:
                    value = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ProtocolError("control record must be valid UTF-8 JSON") from None
                if not isinstance(value, Mapping):
                    raise ProtocolError("control record must be a JSON object")
                return value
            if len(self.buffer) > MAX_CONTROL_RECORD_BYTES:
                raise ProtocolError("control record exceeds maximum size")
            chunk = await asyncio.get_running_loop().sock_recv(
                self.socket,
                min(65536, MAX_CONTROL_RECORD_BYTES + 1 - len(self.buffer)),
            )
            if not chunk:
                raise EOFError("control descriptor closed")
            self.buffer.extend(chunk)


class _TransportBoundaryMiddleware:
    """Reject caller attempts to alter a prepared transport binding."""

    _HEALTH_PATHS = frozenset({"/health", "/healthz", "/livez", "/readyz"})

    def __init__(
        self, app: Any, *, receipt_writer: ReceiptWriter, binding: AcquireRequest, proxy: Any
    ) -> None:
        self.app = app
        self.receipt_writer = receipt_writer
        self.binding = binding
        self.proxy = proxy

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "websocket":
            await self.receipt_writer.record_rejection(
                reason="unsupported_transport_websocket", bypass=False
            )
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope.get("type") != "http" or scope.get("path") in self._HEALTH_PATHS:
            await self.app(scope, receive, send)
            return
        mode = self.binding.account.provider_mode
        if scope.get("method") == "HEAD" and scope.get("path") == "/api/hello":
            # Claude's connection warm-up is local liveness, not inference.
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        if mode == "anthropic_oauth_passthrough" and scope.get("path") not in {
            "/v1/messages",
            "/v1/messages/count_tokens",
        }:
            await self._reject(send, status=404, reason="unsupported_transport_route", bypass=False)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        headroom_headers = {name for name in headers if name.startswith("x-headroom-")}
        if {"x-headroom-bypass", "x-headroom-mode"} & headroom_headers:
            await self._reject(
                send,
                status=400,
                reason="caller_bypass_forbidden",
                bypass=True,
            )
            return
        if headroom_headers or "x-client" in headers:
            await self._reject(
                send,
                status=400,
                reason="caller_attribution_forbidden",
                bypass=True,
            )
            return
        authorization = headers.get("authorization", "")
        if mode in {"anthropic_oauth_passthrough", "openai_oauth_passthrough"} and (
            not authorization.startswith("Bearer ")
            or not authorization[7:].strip()
            or "x-api-key" in headers
        ):
            await self._reject(
                send,
                status=403,
                reason="oauth_bearer_required",
                bypass=False,
            )
            return
        if (
            mode != "anthropic_oauth_passthrough"
            or scope.get("path") == "/v1/messages/count_tokens"
        ):
            from starlette.requests import Request

            from .forwarder import forward

            response = await forward(
                self.binding, self.receipt_writer, self.proxy, Request(scope, receive)
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    async def _reject(self, send: Any, *, status: int, reason: str, bypass: bool) -> None:
        await self.receipt_writer.record_rejection(reason=reason, bypass=bypass)
        body = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "transport_policy_error",
                    "message": reason,
                },
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def create_transport_app(
    request: AcquireRequest,
    receipt_writer: ReceiptWriter,
    *,
    port: int,
) -> FastAPI:
    """Build a stateless, no-fallback proxy bound to one prepared request."""

    config = ProxyConfig(
        host="127.0.0.1",
        port=port,
        anthropic_api_url=request.upstream.url,
        mode="token",
        optimize=True,
        lossless=request.compression.lossless,
        compression_required=request.compression.required,
        cache_enabled=False,
        rate_limit_enabled=False,
        retry_enabled=False,
        cost_tracking_enabled=False,
        log_requests=False,
        log_full_messages=False,
        fallback_enabled=False,
        memory_enabled=False,
        traffic_learning_enabled=False,
        subscription_tracking_enabled=False,
        periodic_toin_stats_enabled=False,
        periodic_malloc_trim_enabled=False,
        discover_pipeline_extensions=False,
        stateless=True,
        isolated_transport=True,
    )
    app = create_app(config)
    app.state.proxy.transport_receipt_writer = receipt_writer
    app.add_middleware(
        _TransportBoundaryMiddleware,
        receipt_writer=receipt_writer,
        binding=request,
        proxy=app.state.proxy,
    )
    return app


def _write_record(stream: TextIO, value: Mapping[str, Any]) -> None:
    stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


def _error_record(category: str) -> dict[str, Any]:
    messages = {
        "invalid_acquire": "transport acquire record is invalid",
        "stale_parent_process": "prepared parent process identity is no longer live",
        "startup_failed": "isolated transport failed to start",
    }
    return {
        "schema": READY_SCHEMA,
        "status": "error",
        "error": {"category": category, "message": messages[category]},
    }


def _validate_release(value: Mapping[str, Any]) -> str:
    expected = {"schema", "lease_id"}
    if set(value) != expected or value.get("schema") != RELEASE_SCHEMA:
        raise ProtocolError("control channel accepts only a release.v1 record after acquire")
    lease_id = value.get("lease_id")
    if not isinstance(lease_id, str) or not lease_id:
        raise ProtocolError("release.lease_id must be a non-empty string")
    return lease_id


def _open_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    listener.setblocking(False)
    return listener


async def _wait_until_started(
    server: uvicorn.Server,
    serve_task: asyncio.Task[None],
    *,
    timeout: float = 30.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not server.started:
        if serve_task.done():
            await serve_task
            raise RuntimeError("uvicorn exited before readiness")
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("uvicorn readiness timeout")
        await asyncio.sleep(0.01)


async def _monitor_control(
    channel: _ControlChannel,
    lease: TransportLease,
    server: uvicorn.Server,
) -> None:
    try:
        while True:
            release = await channel.read_record()
            if lease.release(_validate_release(release)):
                server.should_exit = True
                return
    except EOFError:
        server.should_exit = True
    except (ProtocolError, ValueError):
        server.force_exit = True
        server.should_exit = True
        raise


async def _monitor_parent(
    parent: ProcessIdentity,
    server: uvicorn.Server,
    *,
    interval: float = 0.25,
) -> None:
    while not server.should_exit:
        await asyncio.sleep(interval)
        if not parent.matches_live_process():
            server.should_exit = True
            return


async def serve_transport(
    control_socket: socket.socket,
    readiness_stream: TextIO,
    receipt_stream: TextIO,
) -> int:
    """Serve one binding until exact release, parent death, or proxy failure."""

    channel = _ControlChannel(control_socket)
    readiness_written = False
    listener: socket.socket | None = None
    monitor_tasks: list[asyncio.Task[None]] = []
    try:
        try:
            request = AcquireRequest.from_dict(await channel.read_record())
        except ProtocolError:
            _write_record(readiness_stream, _error_record("invalid_acquire"))
            readiness_written = True
            return 2
        if not request.parent_process.matches_live_process():
            _write_record(readiness_stream, _error_record("stale_parent_process"))
            readiness_written = True
            return 3

        listener = _open_listener()
        port = int(listener.getsockname()[1])
        lease = TransportLease(f"lease-{uuid.uuid4()}")

        def receipt_sink(value: dict[str, Any]) -> None:
            _write_record(receipt_stream, value)

        writer = ReceiptWriter(request, receipt_sink)
        app = create_transport_app(request, writer, port=port)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        serve_task = asyncio.create_task(server.serve(sockets=[listener]))
        await _wait_until_started(server, serve_task)

        process_identity = current_process_identity()
        if not process_identity.matches_live_process():
            raise RuntimeError("transport process identity changed during startup")
        endpoint = f"http://127.0.0.1:{port}"
        _write_record(
            readiness_stream,
            request.ready_record(
                endpoint=endpoint,
                process_identity=process_identity,
                lease_id=lease.lease_id,
            ).to_dict(),
        )
        readiness_written = True

        monitor_tasks = [
            asyncio.create_task(_monitor_control(channel, lease, server)),
            asyncio.create_task(_monitor_parent(request.parent_process, server)),
        ]
        done, _ = await asyncio.wait(
            [serve_task, *monitor_tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if serve_task not in done:
            server.should_exit = True
        await serve_task
        for task in done:
            if task is not serve_task:
                task.result()
        return 0
    except Exception:
        if not readiness_written:
            _write_record(readiness_stream, _error_record("startup_failed"))
        return 4
    finally:
        for task in monitor_tasks:
            task.cancel()
        for task in monitor_tasks:
            with suppress(asyncio.CancelledError):
                await task
        if listener is not None:
            listener.close()


def serve_transport_fds(control_fd: int, readiness_fd: int, receipt_fd: int) -> int:
    """Own inherited descriptors and run the async transport lifecycle."""

    if len({control_fd, readiness_fd, receipt_fd}) != 3:
        raise ValueError("control, readiness, and receipt descriptors must be distinct")
    control_socket = socket.socket(fileno=control_fd)
    readiness_stream = os.fdopen(readiness_fd, "w", encoding="utf-8", buffering=1)
    receipt_stream = os.fdopen(receipt_fd, "w", encoding="utf-8", buffering=1)
    try:
        return asyncio.run(serve_transport(control_socket, readiness_stream, receipt_stream))
    finally:
        control_socket.close()
        readiness_stream.close()
        receipt_stream.close()
