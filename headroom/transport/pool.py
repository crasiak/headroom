"""One Headroom process serving several isolated transport leases.

A pool is the multi-lease form of ``headroom transport serve``. Its supervisor
(Ledger's ledgerd) starts it with an inherited, pathless admission socket and
hands it one lease at a time as ``admit.v1``: a single newline-terminated JSON
record sent in the same ``sendmsg`` as exactly five descriptors (SCM_RIGHTS),
in this order:

    control      the lease's private control socket (AF_UNIX, SOCK_STREAM)
    readiness    write end of the lease's readiness pipe
    receipt      write end of the lease's receipt pipe
    diagnostics  write end of the lease's diagnostics pipe
    listener     the lease's own loopback TCP listener (127.0.0.1)

The pool answers every admit with one ``admission.v1`` record before the
supervisor may send the next one. An admitted lease then runs the unchanged
single-lease protocol on its own descriptors: ``acquire.v1`` on control,
``ready.v1`` on readiness, ``receipt.v1`` on receipt, and ``activate.v1`` then
``release.v1`` on control. A pooled lease must request activation, so no byte
of its traffic is accepted before its supervisor has validated its readiness
and activated it. Release, control EOF, a failed lease and the death of a
lease's parent process end only that lease.

Every lease shares one proxy engine (imports, transforms and the compression
executor) but owns its binding, upstream, receipt writer, diagnostics, HTTP
client and listener. The first acquire the pool serves pins its sharing scope
(account, runtime set, policy, upstream, profile revision and lock hash);
an acquire outside it is refused.
"""

from __future__ import annotations

import array
import asyncio
import errno
import fcntl
import json
import logging
import logging.config
import os
import signal
import socket
import stat
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, cast

import httpx
import uvicorn
import uvicorn.config
from starlette.middleware.errors import ServerErrorMiddleware
from starlette.types import ASGIApp

from headroom.proxy.outcome import RequestOutcome

from .diagnostics import Diagnostics, trace_request
from .protocol import (
    READY_SCHEMA,
    AcquireRequest,
    ProcessIdentity,
    ProtocolError,
    ReceiptWriter,
    canonical_digest,
    current_process_identity,
)
from .replay import LeaseReplayState
from .runtime import (
    TransportLease,
    _ActivationGatedServer,
    _ConnectionDrain,
    _ControlChannel,
    _DrainMiddleware,
    _inherited_listener,
    _monitor_control,
    _monitor_parent,
    _TransportBoundaryMiddleware,
    _wait_until_started,
    transport_proxy_config,
)

ADMIT_SCHEMA = "headroom.transport.admit.v1"
ADMISSION_SCHEMA = "headroom.transport.admission.v1"
DRAIN_SCHEMA = "headroom.transport.drain.v1"
POOL_SCHEMA = "headroom.transport.pool.v1"
POOL_SCOPE_SCHEMA = "headroom.transport.pool_scope.v1"
ADMIT_DESCRIPTORS = ("control", "readiness", "receipt", "diagnostics", "listener")

MAX_POOL_LEASES = 8
DEFAULT_LEASE_COMPRESSION_CONCURRENCY = 2
MAX_ADMISSION_RECORD_BYTES = 4096
MAX_ADMISSION_ID_LENGTH = 128
# Room for more descriptors than an admit may carry, so an oversized batch is
# received whole and closed rather than truncated by the kernel.
_MAX_RECEIVED_DESCRIPTORS = 16
_DIAGNOSTIC_BUFFER_LIMIT = 1024 * 1024
_PIPE_CLOSE_TIMEOUT = 1.0
# The engine's process-wide upstream. Pooled requests take their upstream from
# their own lease's binding; anything that still reached for the process-wide
# one would fail to resolve instead of reaching another lease's upstream.
UNBOUND_UPSTREAM = "https://headroom-pool-unbound.invalid"

logger = logging.getLogger(__name__)

_POOL_ERRORS = {
    "invalid_acquire": "transport acquire record is invalid",
    "stale_parent_process": "prepared parent process identity is no longer live",
    "startup_failed": "isolated transport failed to start",
    "activation_required": "a pooled lease must require activation",
    "pool_scope_mismatch": "acquire is outside this pool's sharing scope",
}


@dataclass(frozen=True)
class LeaseContext:
    """The lease a request belongs to, for code that reaches the shared engine."""

    lease_id: str
    binding: AcquireRequest
    receipt_writer: ReceiptWriter


current_lease: ContextVar[LeaseContext | None] = ContextVar("transport_lease", default=None)


class LeaseScopedReceiptWriter:
    """The engine's receipt writer in a pool: each call writes to its request's lease.

    A pooled engine has no single receipt pipe. Any engine path that records a
    transport receipt resolves the lease from the request's context, and fails
    closed outside one rather than writing to some other lease's pipe.
    """

    @staticmethod
    def _writer() -> ReceiptWriter:
        context = current_lease.get()
        if context is None:
            raise RuntimeError("no transport lease is bound to this request")
        return context.receipt_writer

    async def record(self, outcome: RequestOutcome, *, request_kind: str = "inference") -> None:
        await self._writer().record(outcome, request_kind=request_kind)

    async def record_rejection(
        self, *, reason: str, bypass: bool, upstream_reached: bool = False
    ) -> None:
        await self._writer().record_rejection(
            reason=reason, bypass=bypass, upstream_reached=upstream_reached
        )


def pool_scope(request: AcquireRequest) -> dict[str, Any]:
    """The acquire fields every lease of one pool must share."""

    return {
        "schema": POOL_SCOPE_SCHEMA,
        "account": request.account.to_binding_dict(),
        "runtime_set_digest": request.runtime_set_digest,
        "policy_digest": request.policy_digest,
        "upstream_digest": request.upstream.digest,
        "profile_revision": request.profile_revision,
        "lock_hash": request.lock_hash,
    }


class AdmissionRejected(Exception):
    """An admit the pool refused; the admission channel stays usable."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class AdmissionFramingError(Exception):
    """The admission stream can no longer be trusted; the pool stops admitting."""


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


def _close_fds(fds: list[int]) -> None:
    for fd in fds:
        with suppress(OSError):
            os.close(fd)


class _AdmissionChannel:
    """Admission records with their descriptors, strictly one at a time."""

    def __init__(self, admission_socket: socket.socket) -> None:
        self.socket = admission_socket
        self.socket.setblocking(False)

    async def _recvmsg(self) -> tuple[bytes, list[tuple[int, int, bytes]], int]:
        loop = asyncio.get_running_loop()
        space = socket.CMSG_SPACE(_MAX_RECEIVED_DESCRIPTORS * array.array("i").itemsize)
        while True:
            try:
                data, ancillary, flags, _ = self.socket.recvmsg(
                    MAX_ADMISSION_RECORD_BYTES + 1, space
                )
                return data, ancillary, flags
            except (BlockingIOError, InterruptedError):
                readable: asyncio.Future[None] = loop.create_future()
                loop.add_reader(self.socket.fileno(), _resolve, readable)
                try:
                    await readable
                finally:
                    loop.remove_reader(self.socket.fileno())

    async def read(self) -> tuple[bytes, list[int]]:
        """One record and the descriptors sent with its first byte.

        Raises EOFError at a clean end of stream and AdmissionFramingError when
        the stream breaks the framing rules; descriptors received with a broken
        record are closed.
        """

        buffer = bytearray()
        fds: list[int] = []
        while True:
            data, ancillary, flags = await self._recvmsg()
            received: list[int] = []
            foreign = False
            for level, kind, payload in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(payload[: len(payload) - len(payload) % values.itemsize])
                    received.extend(values)
                else:
                    foreign = True
            if flags & socket.MSG_CTRUNC or foreign:
                _close_fds(fds + received)
                raise AdmissionFramingError("admission control data was truncated or foreign")
            if received and buffer:
                # Descriptors belong to a record's first byte; a later batch
                # could only be another record interleaved into this one.
                _close_fds(fds + received)
                raise AdmissionFramingError("admission descriptors split from their record")
            fds.extend(received)
            if not data:
                _close_fds(fds)
                if buffer:
                    raise AdmissionFramingError("admission stream ended inside a record")
                raise EOFError("admission channel closed")
            buffer.extend(data)
            newline = buffer.find(b"\n")
            if newline >= 0:
                if newline != len(buffer) - 1:
                    # The supervisor waits for each admission reply, so a
                    # second record can never already be in flight.
                    _close_fds(fds)
                    raise AdmissionFramingError("admission record followed by unsolicited data")
                return bytes(buffer[:newline]), fds
            if len(buffer) > MAX_ADMISSION_RECORD_BYTES:
                _close_fds(fds)
                raise AdmissionFramingError("admission record exceeds maximum size")

    async def write(self, value: Mapping[str, Any]) -> None:
        line = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        await asyncio.get_running_loop().sock_sendall(self.socket, line.encode("utf-8"))


def parse_admit(raw: bytes) -> dict[str, Any]:
    """Validate an admission record; descriptors are checked separately."""

    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AdmissionRejected("invalid_admit") from None
    if not isinstance(value, dict):
        raise AdmissionRejected("invalid_admit")
    if value.get("schema") == DRAIN_SCHEMA:
        if set(value) != {"schema"}:
            raise AdmissionRejected("invalid_admit")
        return value
    if set(value) != {"schema", "admission_id", "fds"} or value["schema"] != ADMIT_SCHEMA:
        raise AdmissionRejected("invalid_admit")
    admission_id = value["admission_id"]
    if (
        not isinstance(admission_id, str)
        or not admission_id
        or len(admission_id) > MAX_ADMISSION_ID_LENGTH
        or not admission_id.isascii()
        or not admission_id.isprintable()
    ):
        raise AdmissionRejected("invalid_admit")
    if value["fds"] != list(ADMIT_DESCRIPTORS):
        raise AdmissionRejected("invalid_admit")
    return value


@dataclass
class LeaseDescriptors:
    """A lease's descriptors, owned by the pool once admitted.

    The pipe write ends stay raw descriptors that the lease closes exactly once
    when it ends; its writers never close them.
    """

    control: socket.socket
    readiness: int
    receipt: int
    diagnostics: int
    listener: socket.socket
    identities: frozenset[tuple[Any, ...]]


def _inode_identity(fd: int) -> tuple[Any, ...] | None:
    status = os.fstat(fd)
    # macOS reports no inode for TCP sockets; those are identified by port.
    return ("inode", status.st_dev, status.st_ino) if status.st_ino else None


def _require_write_pipe(fd: int) -> None:
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        raise AdmissionRejected("invalid_descriptor")
    if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE not in (os.O_WRONLY, os.O_RDWR):
        raise AdmissionRejected("invalid_descriptor")


def adopt_descriptors(fds: list[int], in_use: set[tuple[Any, ...]]) -> LeaseDescriptors:
    """Take ownership of an admit's descriptors, or close them all and refuse.

    The control descriptor must be a connected Unix stream socket; readiness,
    receipt and diagnostics must be pipe write ends; the listener must be a
    listening 127.0.0.1 TCP socket. No descriptor may be one another live
    lease already holds, nor appear twice in the admit.
    """

    if len(fds) != len(ADMIT_DESCRIPTORS):
        _close_fds(fds)
        raise AdmissionRejected("invalid_admit")
    control_fd, readiness, receipt, diagnostics, listen_fd = fds
    # Each descriptor is closed exactly once on refusal: by its socket object
    # once one owns it, otherwise as a raw descriptor.
    raw = list(fds)
    owned: list[socket.socket] = []
    try:
        try:
            identities = [
                identity
                for identity in (
                    _inode_identity(fd) for fd in (control_fd, readiness, receipt, diagnostics)
                )
                if identity is not None
            ]
            control = socket.socket(fileno=control_fd)
            raw.remove(control_fd)
            owned.append(control)
            if control.family != socket.AF_UNIX or control.type != socket.SOCK_STREAM:
                raise AdmissionRejected("invalid_descriptor")
            for fd in (readiness, receipt, diagnostics):
                _require_write_pipe(fd)
            # The listener check closes the descriptor itself when it refuses.
            raw.remove(listen_fd)
            listener = _inherited_listener(listen_fd)
            owned.append(listener)
        except (OSError, ValueError):
            raise AdmissionRejected("invalid_descriptor") from None
        identities.append(("tcp", listener.getsockname()[1]))
        if len(set(identities)) != len(identities) or in_use & set(identities):
            raise AdmissionRejected("descriptor_reuse")
        return LeaseDescriptors(
            control=control,
            readiness=readiness,
            receipt=receipt,
            diagnostics=diagnostics,
            listener=listener,
            identities=frozenset(identities),
        )
    except BaseException:
        for sock in owned:
            sock.close()
        _close_fds(raw)
        raise


class _PipeProtocol(asyncio.streams.FlowControlMixin):
    """Flow control for a lease pipe, plus a future that resolves once it is closed."""

    def __init__(self) -> None:
        super().__init__()
        self.closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def connection_lost(self, exc: Exception | None) -> None:
        super().connection_lost(exc)
        if not self.closed.done():
            self.closed.set_result(None)


class _PipeWriter:
    """A non-blocking writer for one lease pipe.

    A supervisor that stops reading one lease's pipe stalls only that lease's
    writes (receipt and readiness wait for room), never the pool's event loop.
    """

    def __init__(
        self,
        transport: asyncio.WriteTransport,
        protocol: _PipeProtocol,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.transport = transport
        self.protocol = protocol
        self.writer = writer

    @classmethod
    async def open(cls, fd: int) -> _PipeWriter:
        loop = asyncio.get_running_loop()
        # The lease owns the descriptor and closes it after the writer.
        pipe = os.fdopen(fd, "wb", buffering=0, closefd=False)
        transport, protocol = await loop.connect_write_pipe(_PipeProtocol, pipe)
        return cls(transport, protocol, asyncio.StreamWriter(transport, protocol, None, loop))

    @staticmethod
    def _line(value: Mapping[str, Any]) -> bytes:
        return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    async def write(self, value: Mapping[str, Any]) -> None:
        if self.transport.is_closing():
            raise BrokenPipeError("lease pipe is closed")
        self.writer.write(self._line(value))
        await self.writer.drain()

    def write_nowait(self, value: Mapping[str, Any]) -> None:
        if (
            self.transport.is_closing()
            or self.transport.get_write_buffer_size() > _DIAGNOSTIC_BUFFER_LIMIT
        ):
            raise BrokenPipeError("lease diagnostics pipe is closed or full")
        self.transport.write(self._line(value))

    async def aclose(self) -> None:
        """Flush and close; a reader that never drains the pipe cannot hold it open.

        On return the descriptor is unregistered from the event loop, so the
        lease may close it.
        """

        if not self.transport.is_closing():
            self.transport.close()
        try:
            await asyncio.wait_for(asyncio.shield(self.protocol.closed), _PIPE_CLOSE_TIMEOUT)
        except asyncio.TimeoutError:
            self.transport.abort()
            await self.protocol.closed


class _AbandonedCompression(Exception):
    """A compression job whose waiter gave up before its worker started."""


@dataclass
class _CompressionJob:
    started: bool = False
    finished: bool = False
    abandoned: bool = False
    timed_out: bool = False


class _LeaseProxy:
    """The engine as one lease sees it: its own HTTP client and compression share.

    A lease runs at most ``concurrency`` compression jobs on the shared
    executor. A job's slot is held until its worker thread finishes, not until
    its waiter gives up: Python cannot stop a running worker, so releasing the
    slot on timeout would let a lease that keeps timing out occupy ever more
    executor threads.

    Timeout debt quarantines the lease, not the engine (whose engine-wide hold
    a pool turns off): while one of this lease's timed-out workers is still
    running, this lease's new compressions raise ``CompressionQuarantinedError``
    and take the forwarder's compression-failure policy, and other leases keep
    compressing. As on the engine, a fresh timeout arms the quarantine for at
    most the engine's quarantine cap, so a worker that never returns cannot
    quarantine the lease for good; its slot stays held regardless.
    """

    def __init__(self, engine: Any, client: httpx.AsyncClient, concurrency: int) -> None:
        self._engine = engine
        self.http_client = client
        self.http_client_h1 = client
        self._compression_slots = asyncio.Semaphore(concurrency)
        self._compression_lock = threading.Lock()
        # Jobs holding a slot (queued or running) and, of those, timed-out
        # jobs whose worker is still running.
        self.compression_jobs = 0
        self.compression_leaked = 0
        self._quarantine_deadline = 0.0

    def compression_snapshot(self) -> dict[str, int]:
        with self._compression_lock:
            return {"jobs": self.compression_jobs, "leaked": self.compression_leaked}

    def _quarantined(self) -> int:
        with self._compression_lock:
            if self.compression_leaked and time.monotonic() < self._quarantine_deadline:
                return self.compression_leaked
            return 0

    async def _run_compression_in_executor(self, fn: Callable[[], Any], *, timeout: float) -> Any:
        from headroom.proxy.server import CompressionQuarantinedError

        leaked = self._quarantined()
        if leaked:
            raise CompressionQuarantinedError(
                f"lease compression quarantined: {leaked} timed-out worker(s) still running"
            )
        # The wait for a share counts against the same compression budget.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await asyncio.wait_for(self._compression_slots.acquire(), timeout)
        job = _CompressionJob()
        with self._compression_lock:
            self.compression_jobs += 1

        def release_slot() -> None:
            self._compression_slots.release()

        def worker_finished() -> None:
            with self._compression_lock:
                job.finished = True
                self.compression_jobs -= 1
                if job.timed_out:
                    self.compression_leaked -= 1
            try:
                loop.call_soon_threadsafe(release_slot)
            except RuntimeError:
                # The pool's loop is gone; so is the lease and its semaphore.
                pass

        def tracked() -> Any:
            with self._compression_lock:
                if job.abandoned:
                    raise _AbandonedCompression
                job.started = True
            try:
                return fn()
            finally:
                worker_finished()

        try:
            result = await self._engine._run_compression_in_executor(
                tracked, timeout=max(0.0, deadline - loop.time())
            )
        except BaseException as error:
            with self._compression_lock:
                if not job.started:
                    # Never reached a worker (cancelled in the executor queue,
                    # or refused): nothing runs for it, so its slot is free now.
                    job.abandoned = True
                    self.compression_jobs -= 1
                    free_now = True
                else:
                    free_now = False
                    if not job.finished and isinstance(error, asyncio.TimeoutError):
                        # Still running past its deadline: timeout debt.
                        job.timed_out = True
                        self.compression_leaked += 1
                        self._quarantine_deadline = (
                            time.monotonic() + self._engine._compression_quarantine_max_seconds
                        )
            if free_now:
                release_slot()
            raise
        with self._compression_lock:
            # An engine that answered without running the job holds no worker.
            free_now = not job.started
            if free_now:
                job.abandoned = True
                self.compression_jobs -= 1
        if free_now:
            release_slot()
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)


class _LeaseContextMiddleware:
    def __init__(self, app: Any, *, context: LeaseContext) -> None:
        self.app = app
        self.context = context

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        token = current_lease.set(self.context)
        try:
            await self.app(scope, receive, send)
        finally:
            current_lease.reset(token)


class _LeaseServer(_ActivationGatedServer):
    """One lease's activation-gated server inside the pool's event loop."""

    def __init__(self, config: uvicorn.Config, *, startup_lock: asyncio.Lock) -> None:
        super().__init__(config)
        self._startup_lock = startup_lock

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        # The pool owns the process's signals; uvicorn's capture would make
        # every lease server restore and re-raise them.
        await self._serve(sockets)

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        # The activation gate swaps the loop's create_server for the length of
        # a startup; two interleaved startups would undo each other's swap.
        async with self._startup_lock:
            await super().startup(sockets=sockets)


def _error_record(category: str) -> dict[str, Any]:
    return {
        "schema": READY_SCHEMA,
        "status": "error",
        "error": {"category": category, "message": _POOL_ERRORS[category]},
    }


class _PoolLease:
    def __init__(self, pool: TransportPool, admission_id: str, fds: LeaseDescriptors) -> None:
        self.pool = pool
        self.admission_id = admission_id
        self.fds = fds
        self.server: _LeaseServer | None = None
        self.task: asyncio.Task[None] | None = None
        self.closing = False

    def stop(self) -> None:
        self.closing = True
        if self.server is not None:
            self.server.should_exit = True
        elif self.task is not None:
            # Not serving yet, e.g. still waiting for its acquire.
            self.task.cancel()

    async def run(self) -> None:
        pool = self.pool
        readiness: _PipeWriter | None = None
        receipts: _PipeWriter | None = None
        diagnostics_pipe: _PipeWriter | None = None
        client: httpx.AsyncClient | None = None
        monitor_tasks: list[asyncio.Task[None]] = []
        readiness_written = False
        lease_id: str | None = None
        replay: LeaseReplayState | None = None
        diagnostics: Diagnostics | None = None
        proxy: _LeaseProxy | None = None
        try:
            readiness = await _PipeWriter.open(self.fds.readiness)
            receipts = await _PipeWriter.open(self.fds.receipt)
            diagnostics_pipe = await _PipeWriter.open(self.fds.diagnostics)
            channel = _ControlChannel(self.fds.control)

            async def refuse(category: str) -> None:
                nonlocal readiness_written
                assert readiness is not None
                readiness_written = True
                await readiness.write(_error_record(category))
                logger.info(
                    "event=pool_lease_refused admission_id=%s category=%s",
                    self.admission_id,
                    category,
                )

            try:
                request = AcquireRequest.from_dict(await channel.read_record())
            except ProtocolError:
                await refuse("invalid_acquire")
                return
            if not request.activation_required:
                await refuse("activation_required")
                return
            if not pool.accept_scope(request):
                await refuse("pool_scope_mismatch")
                return
            if not await asyncio.to_thread(request.parent_process.matches_live_process):
                await refuse("stale_parent_process")
                return
            if self.closing:
                return

            identity = pool.identity
            assert identity is not None
            lease = TransportLease(f"lease-{uuid.uuid4()}")
            lease_id = lease.lease_id
            receipt_pipe = receipts

            async def write_receipt(value: dict[str, Any]) -> None:
                try:
                    await receipt_pipe.write(value)
                except Exception:
                    # A lease that cannot account for its requests serves no more.
                    logger.warning(
                        "event=pool_lease_receipt_channel_failed admission_id=%s",
                        self.admission_id,
                    )
                    if self.server is not None:
                        self.server.force_exit = True
                    self.stop()
                    raise

            writer = ReceiptWriter(request, write_receipt)
            diagnostics = Diagnostics(
                run_id=request.run_id,
                lease_id=lease.lease_id,
                sink=diagnostics_pipe.write_nowait,
                runtime_set_digest=request.runtime_set_digest,
            )
            client = pool.new_http_client()
            replay = LeaseReplayState(pool.engine.config)
            proxy = _LeaseProxy(pool.engine, client, pool.lease_compression_concurrency)
            drain = _ConnectionDrain()
            context = LeaseContext(lease.lease_id, request, writer)
            boundary = _TransportBoundaryMiddleware(
                pool.engine_app,
                receipt_writer=writer,
                binding=request,
                proxy=proxy,
                diagnostics=diagnostics,
                forward_messages=True,
                replay=replay,
            )
            app = ServerErrorMiddleware(
                cast(
                    ASGIApp,
                    _DrainMiddleware(
                        _LeaseContextMiddleware(boundary, context=context), drain=drain
                    ),
                )
            )
            port = int(self.fds.listener.getsockname()[1])
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                log_config=None,
                access_log=False,
                lifespan="off",
            )
            server = _LeaseServer(config, startup_lock=pool.startup_lock)
            self.server = server
            if self.closing:
                return
            serve_task = asyncio.create_task(server.serve(sockets=[self.fds.listener]))
            monitor_tasks.append(serve_task)
            await _wait_until_started(server, serve_task)

            diagnostics.boot = dict(pool.boot)
            diagnostics.emit("transport_boot", **diagnostics.boot)
            readiness_written = True
            await readiness.write(
                request.ready_record(
                    endpoint=f"http://127.0.0.1:{port}",
                    process_identity=identity,
                    lease_id=lease.lease_id,
                ).to_dict()
            )
            logger.info(
                "event=pool_lease_ready admission_id=%s lease_id=%s",
                self.admission_id,
                lease.lease_id,
            )
            monitor_tasks.remove(serve_task)
            monitor_tasks = [
                asyncio.create_task(
                    _monitor_control(channel, lease, server, diagnostics, drain, server.activate)
                ),
                asyncio.create_task(
                    _monitor_parent(request.parent_process, server, diagnostics=diagnostics)
                ),
                asyncio.create_task(diagnostics.sample_loop()),
            ]
            done, _ = await asyncio.wait(
                [serve_task, *monitor_tasks], return_when=asyncio.FIRST_COMPLETED
            )
            if serve_task not in done:
                server.should_exit = True
            await serve_task
            for task in done:
                if task is not serve_task:
                    task.result()
        except Exception:
            logger.exception(
                "event=pool_lease_failure admission_id=%s lease_id=%s readiness_written=%s",
                self.admission_id,
                lease_id,
                readiness_written,
            )
            if self.server is not None:
                self.server.force_exit = True
                self.server.should_exit = True
            if readiness is not None and not readiness_written:
                with suppress(Exception):
                    await readiness.write(_error_record("startup_failed"))
        finally:
            for task in monitor_tasks:
                task.cancel()
            for task in monitor_tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if proxy is not None and diagnostics is not None:
                # Workers cannot be stopped; the lease ends without waiting for
                # them, and their slots die with this lease's semaphore.
                stuck = proxy.compression_snapshot()
                if stuck["jobs"]:
                    diagnostics.emit("compression_work_abandoned", **stuck)
                    logger.warning(
                        "event=pool_lease_compression_abandoned admission_id=%s jobs=%d leaked=%d",
                        self.admission_id,
                        stuck["jobs"],
                        stuck["leaked"],
                    )
            if replay is not None:
                # The lease's conversations end with it; nothing carries over.
                held = replay.clear()
                if diagnostics is not None:
                    diagnostics.emit("replay_state_released", **held)
            if client is not None:
                with suppress(Exception):
                    await client.aclose()
            pipes = [pipe for pipe in (readiness, receipts, diagnostics_pipe) if pipe is not None]
            try:
                for pipe in pipes:
                    await pipe.aclose()
            finally:
                # Whatever interrupted the flush, no descriptor stays registered
                # with the loop once it is closed, and the lease always leaves
                # the table.
                for pipe in pipes:
                    if not pipe.protocol.closed.done():
                        with suppress(Exception):
                            pipe.transport.abort()
                _close_fds([self.fds.readiness, self.fds.receipt, self.fds.diagnostics])
                self.fds.control.close()
                self.fds.listener.close()
                logger.info(
                    "event=pool_lease_ended admission_id=%s lease_id=%s",
                    self.admission_id,
                    lease_id,
                )
                pool.lease_ended(self)


class TransportPool:
    """The lease table, admission loop and shared engine of one pool process."""

    def __init__(
        self,
        admission_socket: socket.socket,
        *,
        max_leases: int = MAX_POOL_LEASES,
        lease_compression_concurrency: int = DEFAULT_LEASE_COMPRESSION_CONCURRENCY,
    ) -> None:
        if not 1 <= max_leases <= MAX_POOL_LEASES:
            raise ValueError(f"max_leases must be between 1 and {MAX_POOL_LEASES}")
        if lease_compression_concurrency < 1:
            raise ValueError("lease_compression_concurrency must be positive")
        self.channel = _AdmissionChannel(admission_socket)
        self.max_leases = max_leases
        self.lease_compression_concurrency = lease_compression_concurrency
        self.leases: dict[str, _PoolLease] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.admission_ids: set[str] = set()
        self.scope_digest: str | None = None
        self.draining = False
        self.finished = asyncio.Event()
        self.startup_lock = asyncio.Lock()
        self.engine_app: Any = None
        self.engine: Any = None
        self.identity: ProcessIdentity | None = None
        self.boot: dict[str, Any] = {}
        self._client_options: tuple[bool, dict[str, Any]] | None = None

    # -- engine -----------------------------------------------------------

    def build_engine(self) -> None:
        from headroom.proxy.server import (
            HeadroomProxy,
            _provider_httpx_client_options,
            build_httpx_verify,
            create_app,
        )
        from headroom.telemetry.toin import TOINConfig, get_toin, reset_toin

        # Ledger's transport policy is always lossless; the engine's own
        # upstream is unbound (each lease forwards to its binding's), and the
        # read lifecycle, which replaces superseded reads with retrieval
        # markers and keeps their originals in a process-wide store, is off.
        config = replace(
            transport_proxy_config(
                port=0,
                upstream_url=UNBOUND_UPSTREAM,
                lossless=True,
                compression_required=True,
            ),
            read_lifecycle=False,
        )
        self.engine_app = create_app(config)
        self.engine = self.engine_app.state.proxy
        if HeadroomProxy.ANTHROPIC_API_URL != UNBOUND_UPSTREAM:
            raise RuntimeError("pool engine upstream is bound")
        self.engine.transport_receipt_writer = LeaseScopedReceiptWriter()
        # TOIN learns across requests; leases may not learn from each other.
        reset_toin()
        get_toin(TOINConfig(enabled=False, storage_path=""))
        # The pipeline's circuit breaker would let one lease's failing input
        # switch compression off for every lease; each request stands alone.
        for name in ("anthropic_pipeline", "openai_pipeline"):
            pipeline = getattr(self.engine, name)
            if not hasattr(pipeline, "_breaker_threshold"):
                raise RuntimeError("pool engine cannot isolate the pipeline breaker")
            pipeline._breaker_threshold = 0
        # Timeout debt quarantines the lease that owns it (_LeaseProxy), not
        # the engine: one lease's stuck worker must not stop compression for
        # every lease. There is no engine-wide backstop: a job that cannot get
        # an executor thread times out in the executor queue and is cancelled
        # before it starts, which adds no debt, and each lease holds at most
        # its own share of threads.
        if not hasattr(self.engine, "_compression_engine_quarantine"):
            raise RuntimeError("pool engine cannot scope the compression quarantine")
        self.engine._compression_engine_quarantine = False
        self._client_options = _provider_httpx_client_options(config, build_httpx_verify())

    def new_http_client(self) -> httpx.AsyncClient:
        """A lease's own upstream client: no connection is shared across leases."""

        assert self._client_options is not None
        http2, options = self._client_options
        client = httpx.AsyncClient(http2=http2, **options)
        client.event_hooks.setdefault("request", []).append(trace_request)
        return client

    # -- lease table ------------------------------------------------------

    def accept_scope(self, request: AcquireRequest) -> bool:
        if not request.compression.lossless:
            return False
        digest = canonical_digest(pool_scope(request))
        if self.scope_digest is None:
            self.scope_digest = digest
            logger.info("event=pool_scope_pinned scope_digest=%s", digest)
        return digest == self.scope_digest

    def in_use(self) -> set[tuple[Any, ...]]:
        identities: set[tuple[Any, ...]] = set()
        for lease in self.leases.values():
            identities |= lease.fds.identities
        identities.add(("inode",) + tuple(_admission_identity(self.channel.socket)))
        return identities

    def _lease_task_done(self, task: asyncio.Task[None]) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("event=pool_lease_task_failed", exc_info=task.exception())

    def lease_ended(self, lease: _PoolLease) -> None:
        if self.leases.get(lease.admission_id) is lease:
            del self.leases[lease.admission_id]
        self._maybe_finish()

    def drain(self, reason: str) -> None:
        if not self.draining:
            self.draining = True
            logger.info("event=pool_draining reason=%s leases=%d", reason, len(self.leases))
        self._maybe_finish()

    def stop(self, reason: str) -> None:
        """End every lease now, as a signal to the whole pool does."""

        self.drain(reason)
        for lease in list(self.leases.values()):
            lease.stop()

    def _maybe_finish(self) -> None:
        if self.draining and not self.leases:
            self.finished.set()

    # -- admission --------------------------------------------------------

    def admit(self, raw: bytes, fds: list[int]) -> dict[str, Any]:
        admission_id: str | None = None
        try:
            try:
                record = parse_admit(raw)
            except AdmissionRejected:
                _close_fds(fds)
                raise
            if record["schema"] == DRAIN_SCHEMA:
                if fds:
                    _close_fds(fds)
                    raise AdmissionRejected("invalid_admit")
                self.drain("drain_record")
                return {"schema": POOL_SCHEMA, "status": "draining", "leases": len(self.leases)}
            admission_id = record["admission_id"]
            if admission_id in self.admission_ids:
                _close_fds(fds)
                raise AdmissionRejected("duplicate_admission")
            self.admission_ids.add(admission_id)
            if self.draining:
                _close_fds(fds)
                raise AdmissionRejected("draining")
            if len(self.leases) >= self.max_leases:
                _close_fds(fds)
                raise AdmissionRejected("lease_cap")
            descriptors = adopt_descriptors(fds, self.in_use())
        except AdmissionRejected as rejected:
            logger.info(
                "event=pool_admission_rejected admission_id=%s category=%s",
                admission_id,
                rejected.category,
            )
            return {
                "schema": ADMISSION_SCHEMA,
                "admission_id": admission_id,
                "status": "rejected",
                "error": {"category": rejected.category},
            }
        lease = _PoolLease(self, admission_id, descriptors)
        self.leases[admission_id] = lease
        task = asyncio.create_task(lease.run())
        lease.task = task
        self.tasks.add(task)
        task.add_done_callback(self._lease_task_done)
        logger.info("event=pool_lease_admitted admission_id=%s", admission_id)
        return {
            "schema": ADMISSION_SCHEMA,
            "admission_id": admission_id,
            "status": "admitted",
            "leases": len(self.leases),
        }

    async def _admission_loop(self) -> None:
        while True:
            try:
                raw, fds = await self.channel.read()
            except EOFError:
                self.drain("admission_eof")
                return
            except AdmissionFramingError:
                logger.warning("event=pool_admission_framing_error")
                with suppress(OSError):
                    await self.channel.write(
                        {
                            "schema": ADMISSION_SCHEMA,
                            "admission_id": None,
                            "status": "rejected",
                            "error": {"category": "invalid_admission_framing"},
                        }
                    )
                self.drain("admission_framing_error")
                return
            reply = self.admit(raw, fds)
            try:
                await self.channel.write(reply)
            except OSError:
                self.drain("admission_write_failed")
                return

    # -- process ----------------------------------------------------------

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, self.stop, signal.Signals(signum).name.lower())
        admission_task: asyncio.Task[None] | None = None
        try:
            try:
                self.identity = await asyncio.to_thread(current_process_identity)
                if not self.identity.matches_live_process():
                    raise RuntimeError("pool process identity is not verifiable")
                self.build_engine()
            except Exception:
                logger.exception("event=pool_startup_failure")
                await self._write_pool_error()
                return 4
            async with self.engine_app.router.lifespan_context(self.engine_app):
                boot_diagnostics = Diagnostics(run_id="pool")
                self.boot = boot_diagnostics.capture_boot(self.identity)
                await self.channel.write(
                    {
                        "schema": POOL_SCHEMA,
                        "status": "ready",
                        "process_identity": self.identity.to_dict(),
                        "process_group_id": os.getpgrp(),
                        "max_leases": self.max_leases,
                    }
                )
                admission_task = asyncio.create_task(self._admission_loop())
                await self.finished.wait()
                admission_task.cancel()
                with suppress(asyncio.CancelledError):
                    await admission_task
                for task in list(self.tasks):
                    with suppress(asyncio.CancelledError, Exception):
                        await task
            logger.info("event=pool_exit reason=drained")
            return 0
        finally:
            if admission_task is not None and not admission_task.done():
                admission_task.cancel()
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)

    async def _write_pool_error(self) -> None:
        with suppress(OSError):
            await self.channel.write(
                {
                    "schema": POOL_SCHEMA,
                    "status": "error",
                    "error": {"category": "startup_failed"},
                }
            )


def _admission_identity(admission_socket: socket.socket) -> tuple[int, int]:
    status = os.fstat(admission_socket.fileno())
    return status.st_dev, status.st_ino


def _pathless_admission_socket(admission_fd: int) -> socket.socket:
    """The inherited admission socket: a connected, unnamed Unix stream socket."""

    admission = socket.socket(fileno=admission_fd)
    try:
        if admission.family != socket.AF_UNIX or admission.type != socket.SOCK_STREAM:
            raise ValueError("admission descriptor must be a Unix stream socket")
        if admission.getsockname() or admission.getpeername():
            # A bound path would let any local process that can reach it admit.
            raise ValueError("admission socket must not have a filesystem path")
    except OSError as exc:
        admission.close()
        if exc.errno == errno.ENOTCONN:
            raise ValueError("admission socket must be connected") from None
        raise
    except BaseException:
        admission.close()
        raise
    return admission


def serve_pool_fd(
    admission_fd: int,
    *,
    max_leases: int = MAX_POOL_LEASES,
    lease_compression_concurrency: int = DEFAULT_LEASE_COMPRESSION_CONCURRENCY,
) -> int:
    """Own the inherited admission socket and serve leases until drained."""

    admission = _pathless_admission_socket(admission_fd)
    try:
        logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
        logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
        logger.setLevel(logging.INFO)
        return asyncio.run(
            TransportPool(
                admission,
                max_leases=max_leases,
                lease_compression_concurrency=lease_compression_concurrency,
            ).run()
        )
    finally:
        admission.close()


__all__ = [
    "ADMISSION_SCHEMA",
    "ADMIT_DESCRIPTORS",
    "ADMIT_SCHEMA",
    "DRAIN_SCHEMA",
    "MAX_POOL_LEASES",
    "POOL_SCHEMA",
    "LeaseContext",
    "LeaseScopedReceiptWriter",
    "TransportPool",
    "current_lease",
    "pool_scope",
    "serve_pool_fd",
]
