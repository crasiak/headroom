"""Bounded, metadata-only local diagnostics for isolated transports.

Independent of transport v1 receipt/control records; never accepts request
objects, exception text, URLs, header values, or bodies into an event.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import marshal
import sys
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from headroom import __version__

SCHEMA = "headroom.transport.diagnostic.v1"
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
ROUTES = frozenset(
    {
        "anthropic_messages",
        "anthropic_count_tokens",
        "openai_responses",
        "openai_models",
        "bedrock_invoke",
        "bedrock_metadata",
        "unsupported",
    }
)
STAGES = frozenset(
    {
        "accepted",
        "validating",
        "upstream_connect",
        "awaiting_headers",
        "streaming",
        "completed",
        "failed",
        "cancelled",
    }
)
current_request: ContextVar[RequestDiagnostic | None] = ContextVar(
    "transport_request_diagnostic", default=None
)


def log_event(event: dict[str, Any]) -> None:
    logger.info("%s", json.dumps(event, separators=(",", ":"), sort_keys=True))


def route_name(path: str, mode: str) -> str:
    if mode == "anthropic_oauth_passthrough":
        return {
            "/v1/messages": "anthropic_messages",
            "/v1/messages/count_tokens": "anthropic_count_tokens",
        }.get(path, "unsupported")
    if mode.startswith("openai_"):
        if path in {"/responses", "/v1/responses", "/responses/compact", "/v1/responses/compact"}:
            return "openai_responses"
        return "openai_models" if path in {"/models", "/v1/models"} else "unsupported"
    if path.startswith("/model/"):
        return "bedrock_metadata" if path.endswith("/count-tokens") else "bedrock_invoke"
    return "bedrock_metadata" if path.startswith("/inference-profiles") else "unsupported"


class RequestDiagnostic:
    def __init__(self, owner: Diagnostics, route: str, tracked: bool):
        self.owner, self.route, self.tracked = owner, route, tracked
        self.request_id = str(uuid.uuid4()) if tracked else None
        self.started = owner.clock()
        self.last_stage = "accepted"
        self.status: int | None = None
        self.finished = False
        self.transitions = 0
        if self.request_id is not None:
            owner.active[self.request_id] = self
            self._emit(None)

    def _emit(self, previous: str | None) -> None:
        self.owner.emit(
            "request_stage",
            request_id=self.request_id,
            route=self.route,
            stage=self.last_stage,
            previous_stage=previous,
            status=self.status,
            elapsed_ms=max(0, (self.owner.clock() - self.started) * 1000),
        )

    def stage(self, stage: str, *, status: int | None = None) -> None:
        if stage not in STAGES:
            raise ValueError("unsupported diagnostic stage")
        if self.finished:
            return
        if status is not None and isinstance(status, int) and 100 <= status <= 599:
            self.status = status
        if stage == self.last_stage:
            return
        previous, self.last_stage = self.last_stage, stage
        # Cap repeated connect/retry transitions as well as active storage.
        self.transitions += 1
        if self.tracked and (
            self.transitions <= 32 or stage in {"completed", "failed", "cancelled"}
        ):
            self._emit(previous)

    def finish(self, stage: str) -> None:
        if self.finished:
            return
        self.stage(stage)
        self.finished = True
        if self.request_id is not None:
            self.owner.active.pop(self.request_id, None)
        self.owner.active_count -= 1


class Diagnostics:
    def __init__(
        self,
        *,
        run_id: str,
        lease_id: str | None = None,
        sink: Callable[[dict[str, Any]], None] = log_event,
        runtime_set_digest: str | None = None,
        capacity: int = 128,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.run_id, self.lease_id, self.sink = run_id, lease_id, sink
        self.runtime_set_digest = runtime_set_digest
        self.capacity, self.clock = capacity, clock
        self.active: dict[str, RequestDiagnostic] = {}
        self.active_count = self.overflow_count = 0
        self.loop_lag_ms = self.loop_lag_max_ms = 0.0
        self.sampled_at: float | None = None
        self.boot: dict[str, Any] | None = None
        self.sink_errors = 0

    def emit(self, event: str, **fields: Any) -> None:
        value = dict(
            schema=SCHEMA,
            event=event,
            event_id=str(uuid.uuid4()),
            observed_at_unix_ms=time.time() * 1000,
            monotonic_ms=self.clock() * 1000,
            run_id=self.run_id,
            lease_id=self.lease_id,
            **fields,
        )
        try:
            self.sink(value)
        except Exception:
            # Observability may not turn a successful provider response into a
            # failure. Count sink loss; never log exception text or recurse.
            self.sink_errors += 1

    def begin(self, route: str) -> RequestDiagnostic:
        if route not in ROUTES:
            route = "unsupported"
        tracked = len(self.active) < self.capacity
        self.active_count += 1
        if not tracked:
            self.overflow_count += 1
        return RequestDiagnostic(self, route, tracked)

    def snapshot(self) -> dict[str, Any]:
        # Safe for public health: no run/lease/request IDs or filesystem paths.
        return {
            "active_requests": self.active_count,
            "tracked_requests": len(self.active),
            "overflow_count": self.overflow_count,
            "loop_lag_ms": self.loop_lag_ms,
            "loop_lag_max_ms": self.loop_lag_max_ms,
            "sampled_at_unix_ms": self.sampled_at,
            "active_leases": int(self.lease_id is not None),
            "active_proxies": 1,
            "diagnostic_sink_errors": self.sink_errors,
        }

    async def sample_loop(self, *, interval: float = 1.0) -> None:
        report_at = self.clock()
        while True:
            deadline = self.clock() + interval
            await asyncio.sleep(interval)
            now = self.clock()
            self.loop_lag_ms = max(0, (now - deadline) * 1000)
            self.loop_lag_max_ms = max(self.loop_lag_max_ms, self.loop_lag_ms)
            self.sampled_at = time.time() * 1000
            if now >= report_at:
                self.emit("runtime_sample", **self.snapshot())
                report_at = now + 60

    def capture_boot(self, identity: Any) -> dict[str, Any]:
        if self.boot is None:
            # A one-time launch manifest is evidence about disk at launch, not
            # proof of every Python module loaded. Never relabel later disk
            # contents as the running process's code revision.
            root = Path(__file__).resolve().parents[1]
            manifest: dict[str, str | None] = {}
            for relative in (
                "transport/diagnostics.py",
                "transport/protocol.py",
                "transport/runtime.py",
                "transport/forwarder.py",
                "proxy/server.py",
                "proxy/handlers/anthropic.py",
            ):
                try:
                    manifest[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
                except OSError:
                    manifest[relative] = None
            loaded = {}
            for relative in manifest:
                module_name = "headroom." + relative.removesuffix(".py").replace("/", ".")
                module = sys.modules.get(module_name)
                functions = {}
                for name, value in vars(module).items() if module else ():
                    if getattr(value, "__module__", None) != module_name:
                        continue
                    candidates = vars(value).items() if isinstance(value, type) else [(name, value)]
                    for member_name, member in candidates:
                        member = getattr(member, "__func__", member)
                        code = getattr(member, "__code__", None)
                        if code is not None:
                            functions[name + "." + member_name] = hashlib.sha256(
                                marshal.dumps(code)
                            ).hexdigest()
                loaded[module_name] = functions
            self.boot = {
                "loaded_function_fingerprint": "sha256:"
                + hashlib.sha256(json.dumps(loaded, sort_keys=True).encode()).hexdigest(),
                "loaded_function_identity_quality": "selected_loaded_python_functions",
                "package_version": __version__,
                "process": identity.to_dict(),
                "source_fingerprint": "sha256:"
                + hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
                "source_manifest": manifest,
                "source_identity_quality": "launch_disk_manifest",
                "loaded_code_identity_quality": "unknown",
                "dirty": "unknown",
                "source_drift": "not_checked",
                "runtime_set_digest": self.runtime_set_digest,
            }
            self.emit("transport_boot", **self.boot)
        return self.boot.copy()


async def trace_request(request: Any) -> None:
    """httpx request hook shared by both isolated Anthropic and forwarder paths."""
    record = current_request.get()
    if record is None:
        return
    previous = request.extensions.get("trace")

    async def trace(name: str, info: dict[str, Any]) -> None:
        if name.endswith(
            ("connect_tcp.started", "connect_unix_socket.started", "start_tls.started")
        ):
            record.stage("upstream_connect")
        elif name.endswith("receive_response_headers.started"):
            record.stage("awaiting_headers")
        elif name.endswith("receive_response_headers.complete"):
            value = info.get("return_value")
            status = value[1] if isinstance(value, tuple) and len(value) > 1 else None
            record.stage("streaming", status=status)
        if previous:
            await previous(name, info)

    request.extensions["trace"] = trace
    # HTTP transport operation begins here; trace moves to connection or
    # awaiting headers only when httpcore actually reaches that stage.
    record.stage("upstream_connect")


class ParentEpisode:
    """One bounded unavailable episode, including its recovery/terminal read."""

    def __init__(self, diagnostics: Diagnostics, pid: int):
        self.diagnostics, self.pid = diagnostics, pid
        self.started: float | None = None
        self.last_report = 0.0
        self.episode_id: str | None = None
        self.attempts = self.failed = 0
        self.read_elapsed_ms = 0.0
        self.last_observation: dict[str, Any] = {}

    def observe(self, observation: Any) -> None:
        from dataclasses import asdict

        now = self.diagnostics.clock()
        self.last_observation = asdict(observation)
        if self.started is None:
            if observation.state == "live":
                return
            self.started, self.last_report = now, now
            self.episode_id = str(uuid.uuid4())
            self.attempts = self.failed = 0
            self.read_elapsed_ms = 0
        self.attempts += 1
        self.failed += int(observation.state == "unavailable")
        self.read_elapsed_ms += observation.read_elapsed_ms
        if observation.state != "unavailable":
            self.report("recovered" if observation.state == "live" else "terminal")
            self.started = None
        elif self.attempts == 1:
            self.report("started")
        elif now - self.last_report >= 60:
            self.report("summary")

    def report(self, outcome: str) -> None:
        if self.started is None:
            return
        now = self.diagnostics.clock()
        self.diagnostics.emit(
            "parent_observation",
            pid=self.pid,
            episode_id=self.episode_id,
            outcome=outcome,
            attempt_count=self.attempts,
            failed_attempt_count=self.failed,
            episode_duration_ms=max(0, (now - self.started) * 1000),
            cumulative_read_elapsed_ms=self.read_elapsed_ms,
            **self.last_observation,
        )
        self.last_report = now
