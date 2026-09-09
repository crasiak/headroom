"""Strict wire values for an isolated Headroom transport process.

The protocol deliberately carries references and digests, never credential
contents.  Records are suitable for newline-delimited JSON over inherited file
descriptors; callers are expected to keep those descriptors private.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from headroom.proxy.outcome import RequestOutcome

ACQUIRE_SCHEMA = "headroom.transport.acquire.v1"
BINDING_SCHEMA = "headroom.transport.binding.v1"
POLICY_SCHEMA = "headroom.transport.policy.v1"
UPSTREAM_SCHEMA = "headroom.transport.upstream.v1"
READY_SCHEMA = "headroom.transport.ready.v1"
RECEIPT_SCHEMA = "headroom.transport.receipt.v1"

PROVIDER_MODES = frozenset(
    {
        "anthropic_oauth_passthrough",
        "aws_bedrock_backend",
        "bedrock_aperture_passthrough",
        "openai_oauth_passthrough",
        "openai_aperture_passthrough",
    }
)
LOCATOR_SCHEMES = frozenset({"aws-profile", "file", "keychain", "test", "http", "https"})
DIGEST_PREFIX = "sha256:"
DIGEST_LENGTH = len(DIGEST_PREFIX) + 64

TRANSPORT_SET_ENV = "ANTHROPIC_BASE_URL"
TRANSPORT_CLEAR_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_DEFAULT_PROFILE",
    "AWS_DEFAULT_REGION",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SECURITY_TOKEN",
    "AWS_SESSION_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_VERTEX",
)


def child_environment(mode: str, endpoint: str) -> ChildEnvironment:
    """The complete native environment delta for a typed provider binding."""
    if mode == "anthropic_oauth_passthrough":
        return ChildEnvironment(set={TRANSPORT_SET_ENV: endpoint}, clear=TRANSPORT_CLEAR_ENV)
    clear = set(TRANSPORT_CLEAR_ENV) | {
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
    }
    if mode in {"openai_oauth_passthrough", "openai_aperture_passthrough"}:
        # Codex's endpoint is rendered as a protected native config override by
        # Ledger; no native home or settings file is rewritten by Headroom.
        return ChildEnvironment(set={}, clear=tuple(sorted(clear)))
    if mode == "bedrock_aperture_passthrough":
        values = {
            "ANTHROPIC_BEDROCK_BASE_URL": endpoint,
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        }
        return ChildEnvironment(set=values, clear=tuple(sorted(clear - values.keys())))
    raise ProtocolError("unsupported transport provider mode")


class ProtocolError(ValueError):
    """The inherited transport record is invalid or inconsistent."""


def canonical_digest(value: Mapping[str, Any]) -> str:
    """Return a domain-separated SHA-256 digest of canonical JSON."""

    def normalize_numbers(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {key: normalize_numbers(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize_numbers(child) for child in item]
        if isinstance(item, float) and math.isfinite(item) and item.is_integer():
            return int(item)
        return item

    encoded = json.dumps(
        normalize_numbers(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return DIGEST_PREFIX + hashlib.sha256(encoded).hexdigest()


def _expect_object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{path} must be an object")
    return value


def _expect_fields(
    value: Mapping[str, Any],
    *,
    path: str,
    required: frozenset[str],
) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise ProtocolError(f"{path} missing field(s): {', '.join(missing)}")
    extra = sorted(value.keys() - required)
    if extra:
        raise ProtocolError(f"{path} has unexpected field(s): {', '.join(extra)}")


def _expect_string(value: Any, path: str, *, max_length: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ProtocolError(f"{path} must be a non-empty string of at most {max_length} bytes")
    return value


def _expect_digest(value: Any, path: str) -> str:
    digest = _expect_string(value, path, max_length=DIGEST_LENGTH)
    if len(digest) != DIGEST_LENGTH or not digest.startswith(DIGEST_PREFIX):
        raise ProtocolError(f"{path} must be a sha256 digest")
    try:
        int(digest[len(DIGEST_PREFIX) :], 16)
    except ValueError:
        raise ProtocolError(f"{path} must be a sha256 digest") from None
    return digest


def _expect_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{path} must be a boolean")
    return value


def _iso_utc(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    if normalized.microsecond == 0:
        return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_source: str
    start_time: float

    @classmethod
    def from_dict(cls, value: Any, path: str = "process") -> ProcessIdentity:
        obj = _expect_object(value, path)
        _expect_fields(
            obj,
            path=path,
            required=frozenset({"pid", "start_source", "start_time"}),
        )
        pid = obj["pid"]
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ProtocolError(f"{path}.pid must be a positive integer")
        start_source = _expect_string(obj["start_source"], f"{path}.start_source", max_length=32)
        start_time = obj["start_time"]
        if (
            not isinstance(start_time, int | float)
            or isinstance(start_time, bool)
            or not math.isfinite(float(start_time))
            or float(start_time) < 0
        ):
            raise ProtocolError(f"{path}.start_time must be a non-negative finite number")
        return cls(pid=pid, start_source=start_source, start_time=float(start_time))

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_source": self.start_source,
            "start_time": self.start_time,
        }

    def matches_live_process(self) -> bool:
        reader = {
            "psutil": _psutil_process_identity,
            "proc": _proc_process_identity,
            "ps": _ps_process_identity,
        }.get(self.start_source)
        if reader is None:
            return False
        current = reader(self.pid)
        if current is None:
            return False
        return current.start_time == self.start_time


def _psutil_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        import psutil  # type: ignore[import-untyped]

        return ProcessIdentity(
            pid=pid, start_source="psutil", start_time=psutil.Process(pid).create_time()
        )
    except Exception:
        return None


def _proc_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rpartition(b")")[2].split()
        return ProcessIdentity(pid=pid, start_source="proc", start_time=float(fields[19]))
    except (OSError, IndexError, ValueError):
        return None


def _ps_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        value = completed.stdout.strip()
        if not value:
            return None
        parsed = time.strptime(value, "%a %b %d %H:%M:%S %Y")
        return ProcessIdentity(pid=pid, start_source="ps", start_time=time.mktime(parsed))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def current_process_identity(pid: int | None = None) -> ProcessIdentity:
    """Read a PID plus stable start time, failing closed when unavailable."""

    target = os.getpid() if pid is None else pid
    for reader in (_psutil_process_identity, _proc_process_identity, _ps_process_identity):
        identity = reader(target)
        if identity is not None:
            return identity
    raise ProcessLookupError(f"cannot establish process identity for pid {target}")


@dataclass(frozen=True)
class AccountBinding:
    ref: str
    revision: str
    credential_locator: str
    provider_mode: str

    @classmethod
    def from_dict(cls, value: Any) -> AccountBinding:
        path = "account"
        obj = _expect_object(value, path)
        _expect_fields(
            obj,
            path=path,
            required=frozenset({"ref", "revision", "credential_locator", "provider_mode"}),
        )
        ref = _expect_string(obj["ref"], f"{path}.ref")
        revision = _expect_digest(obj["revision"], f"{path}.revision")
        locator = _expect_string(obj["credential_locator"], f"{path}.credential_locator")
        locator_parts = urlsplit(locator)
        if locator_parts.scheme not in LOCATOR_SCHEMES:
            raise ProtocolError(
                f"{path}.credential_locator must use a reference-only locator scheme"
            )
        if locator_parts.username is not None or locator_parts.password is not None:
            raise ProtocolError(f"{path}.credential_locator must not contain userinfo")
        provider_mode = _expect_string(obj["provider_mode"], f"{path}.provider_mode", max_length=64)
        if provider_mode not in PROVIDER_MODES:
            raise ProtocolError(f"{path}.provider_mode is unsupported")
        return cls(
            ref=ref,
            revision=revision,
            credential_locator=locator,
            provider_mode=provider_mode,
        )

    def to_binding_dict(self) -> dict[str, str]:
        return {
            "ref": self.ref,
            "revision": self.revision,
            "credential_locator": self.credential_locator,
            "provider_mode": self.provider_mode,
        }


@dataclass(frozen=True)
class CompressionPolicy:
    required: bool
    lossless: bool
    bypass: str
    transform_preset: str

    @classmethod
    def from_dict(cls, value: Any) -> CompressionPolicy:
        path = "compression"
        obj = _expect_object(value, path)
        _expect_fields(
            obj,
            path=path,
            required=frozenset({"required", "lossless", "bypass", "transform_preset"}),
        )
        required = _expect_bool(obj["required"], f"{path}.required")
        lossless = _expect_bool(obj["lossless"], f"{path}.lossless")
        bypass = _expect_string(obj["bypass"], f"{path}.bypass", max_length=16)
        if bypass != "forbid":
            raise ProtocolError(f"{path}.bypass must be 'forbid' in transport v1")
        preset = _expect_string(obj["transform_preset"], f"{path}.transform_preset", max_length=64)
        if required and not lossless:
            raise ProtocolError("transport v1 required compression must be lossless")
        return cls(required=required, lossless=lossless, bypass=bypass, transform_preset=preset)

    def to_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "lossless": self.lossless,
            "bypass": self.bypass,
            "transform_preset": self.transform_preset,
        }


@dataclass(frozen=True)
class UpstreamBinding:
    url: str
    digest: str

    @classmethod
    def from_dict(cls, value: Any) -> UpstreamBinding:
        path = "upstream"
        obj = _expect_object(value, path)
        _expect_fields(obj, path=path, required=frozenset({"url", "digest"}))
        url = _expect_string(obj["url"], f"{path}.url", max_length=2048).rstrip("/")
        parts = urlsplit(url)
        if parts.username is not None or parts.password is not None:
            raise ProtocolError(f"{path}.url must not contain userinfo")
        if parts.query or parts.fragment:
            raise ProtocolError(f"{path}.url must not contain a query or fragment")
        loopback = parts.hostname in {"127.0.0.1", "::1", "localhost"}
        if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
            raise ProtocolError(f"{path}.url must use HTTPS or loopback HTTP")
        if not parts.hostname:
            raise ProtocolError(f"{path}.url must include a host")
        digest = _expect_digest(obj["digest"], f"{path}.digest")
        computed = canonical_digest({"schema": UPSTREAM_SCHEMA, "url": url})
        if digest != computed:
            raise ProtocolError("upstream.digest mismatch")
        return cls(url=url, digest=digest)

    def to_dict(self) -> dict[str, str]:
        return {"url": self.url, "digest": self.digest}


@dataclass(frozen=True)
class ChildEnvironment:
    set: dict[str, str]
    clear: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"set": dict(self.set), "clear": list(self.clear)}


@dataclass(frozen=True)
class ReadyRecord:
    status: str
    endpoint: str
    process_identity: ProcessIdentity
    process_group_id: int
    lease_id: str
    runtime_set_digest: str
    account_revision: str
    policy_digest: str
    binding_digest: str
    receipt_cursor: int
    child_environment: ChildEnvironment

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": READY_SCHEMA,
            "status": self.status,
            "endpoint": self.endpoint,
            "process_identity": self.process_identity.to_dict(),
            "process_group_id": self.process_group_id,
            "lease_id": self.lease_id,
            "runtime_set_digest": self.runtime_set_digest,
            "account_revision": self.account_revision,
            "policy_digest": self.policy_digest,
            "binding_digest": self.binding_digest,
            "receipt_cursor": self.receipt_cursor,
            "child_environment": self.child_environment.to_dict(),
        }


@dataclass(frozen=True)
class AcquireRequest:
    run_id: str
    profile_revision: str
    lock_hash: str
    materialization_id: str
    harness: str
    venue: str
    cwd: str
    worktree_identity: str
    account: AccountBinding
    compression: CompressionPolicy
    runtime_set_digest: str
    policy_digest: str
    upstream: UpstreamBinding
    parent_process: ProcessIdentity
    binding_digest: str

    _FIELDS = frozenset(
        {
            "schema",
            "run_id",
            "profile_revision",
            "lock_hash",
            "materialization_id",
            "harness",
            "venue",
            "cwd",
            "worktree_identity",
            "account",
            "compression",
            "runtime_set_digest",
            "policy_digest",
            "upstream",
            "parent_process",
            "binding_digest",
        }
    )

    @classmethod
    def from_dict(cls, value: Any) -> AcquireRequest:
        obj = _expect_object(value, "acquire")
        _expect_fields(obj, path="acquire", required=cls._FIELDS)
        if obj["schema"] != ACQUIRE_SCHEMA:
            raise ProtocolError(f"acquire.schema must be {ACQUIRE_SCHEMA!r}")
        compression = CompressionPolicy.from_dict(obj["compression"])
        policy_digest = _expect_digest(obj["policy_digest"], "policy_digest")
        computed_policy = canonical_digest({"schema": POLICY_SCHEMA, **compression.to_dict()})
        if policy_digest != computed_policy:
            raise ProtocolError("policy_digest mismatch")
        upstream = UpstreamBinding.from_dict(obj["upstream"])
        request = cls(
            run_id=_expect_string(obj["run_id"], "run_id"),
            profile_revision=_expect_digest(obj["profile_revision"], "profile_revision"),
            lock_hash=_expect_digest(obj["lock_hash"], "lock_hash"),
            materialization_id=_expect_string(obj["materialization_id"], "materialization_id"),
            harness=_expect_string(obj["harness"], "harness", max_length=64),
            venue=_expect_string(obj["venue"], "venue", max_length=64),
            cwd=_expect_string(obj["cwd"], "cwd", max_length=4096),
            worktree_identity=_expect_digest(obj["worktree_identity"], "worktree_identity"),
            account=AccountBinding.from_dict(obj["account"]),
            compression=compression,
            runtime_set_digest=_expect_digest(obj["runtime_set_digest"], "runtime_set_digest"),
            policy_digest=policy_digest,
            upstream=upstream,
            parent_process=ProcessIdentity.from_dict(obj["parent_process"], "parent_process"),
            binding_digest=_expect_digest(obj["binding_digest"], "binding_digest"),
        )
        mode = request.account.provider_mode
        expected_harness = "codex" if mode.startswith("openai_") else "claude-code"
        if request.harness != expected_harness:
            raise ProtocolError("transport provider mode does not match harness")
        if mode == "aws_bedrock_backend":
            raise ProtocolError("transport serve does not support the raw AWS backend")
        locator = urlsplit(request.account.credential_locator)
        if locator.query or locator.fragment:
            raise ProtocolError("credential locator must not contain query or fragment")
        if mode == "anthropic_oauth_passthrough" and locator.scheme != "keychain":
            raise ProtocolError("Anthropic OAuth requires a keychain locator")
        if mode.startswith("openai_") and (
            locator.scheme != "file" or locator.netloc or not locator.path.startswith("/")
        ):
            raise ProtocolError("OpenAI transport requires an absolute local file locator")
        if mode == "bedrock_aperture_passthrough" and (
            not locator.hostname
            or locator.scheme not in {"https", "http"}
            or (
                locator.scheme == "http"
                and locator.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ProtocolError("Bedrock Aperture requires HTTPS or loopback HTTP")
        if request.binding_digest != request.computed_binding_digest():
            raise ProtocolError("binding_digest mismatch")
        return request

    @classmethod
    def binding_digest_for(cls, value: Mapping[str, Any]) -> str:
        fields = {
            key: value[key]
            for key in cls._FIELDS
            if key not in {"schema", "binding_digest"} and key in value
        }
        return canonical_digest({"schema": BINDING_SCHEMA, **fields})

    def computed_binding_digest(self) -> str:
        return canonical_digest(
            {
                "schema": BINDING_SCHEMA,
                "run_id": self.run_id,
                "profile_revision": self.profile_revision,
                "lock_hash": self.lock_hash,
                "materialization_id": self.materialization_id,
                "harness": self.harness,
                "venue": self.venue,
                "cwd": self.cwd,
                "worktree_identity": self.worktree_identity,
                "account": self.account.to_binding_dict(),
                "compression": self.compression.to_dict(),
                "runtime_set_digest": self.runtime_set_digest,
                "policy_digest": self.policy_digest,
                "upstream": self.upstream.to_dict(),
                "parent_process": self.parent_process.to_dict(),
            }
        )

    def ready_record(
        self,
        *,
        endpoint: str,
        process_identity: ProcessIdentity,
        lease_id: str,
    ) -> ReadyRecord:
        return ReadyRecord(
            status="ready",
            endpoint=endpoint,
            process_identity=process_identity,
            process_group_id=os.getpgrp(),
            lease_id=lease_id,
            runtime_set_digest=self.runtime_set_digest,
            account_revision=self.account.revision,
            policy_digest=self.policy_digest,
            binding_digest=self.binding_digest,
            receipt_cursor=0,
            child_environment=child_environment(self.account.provider_mode, endpoint),
        )


class ReceiptWriter:
    """Serialize a safe, run-bound receipt for every finalized outcome."""

    def __init__(
        self,
        request: AcquireRequest,
        sink: Callable[[dict[str, Any]], Any],
        *,
        receipt_id_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._request = request
        self._sink = sink
        self._receipt_id_factory = receipt_id_factory or (lambda: f"receipt-{uuid.uuid4()}")
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        self._cursor = 0

    @property
    def cursor(self) -> int:
        return self._cursor

    async def record(self, outcome: RequestOutcome, *, request_kind: str = "inference") -> None:
        if request_kind not in {"inference", "metadata"}:
            raise ValueError("invalid transport request kind")
        async with self._lock:
            self._cursor += 1
            ended = self._now_factory().astimezone(timezone.utc)
            started = ended - timedelta(milliseconds=max(0.0, outcome.total_latency_ms))
            bypass_reason = None
            if isinstance(outcome.tags, Mapping):
                candidate = outcome.tags.get("passthrough_reason")
                if isinstance(candidate, str) and candidate:
                    bypass_reason = candidate[:128]
            failed = outcome.status_code >= 400 or (
                self._request.compression.required and bypass_reason is not None
            )
            record = {
                "schema": RECEIPT_SCHEMA,
                "cursor": self._cursor,
                "receipt_id": self._receipt_id_factory(),
                "request_id": outcome.request_id,
                "run_id": self._request.run_id,
                "binding_digest": self._request.binding_digest,
                "runtime_set_digest": self._request.runtime_set_digest,
                "policy_digest": self._request.policy_digest,
                "account_revision": self._request.account.revision,
                "provider_mode": self._request.account.provider_mode,
                "model": outcome.model,
                "started_at": _iso_utc(started),
                "ended_at": _iso_utc(ended),
                "outcome": "failed" if failed else "succeeded",
                "upstream_reached": not outcome.from_response_cache,
                "accounting_status": "rejected" if bypass_reason is not None else "evaluated",
                "input_tokens_before": outcome.original_tokens,
                "input_tokens_after": outcome.optimized_tokens,
                "saved_tokens": outcome.tokens_saved,
                "transforms": list(outcome.transforms_applied),
                "bypass_reason": bypass_reason,
                "error_reason": f"upstream_status_{outcome.status_code}"
                if outcome.status_code >= 400
                else None,
            }
            if request_kind == "metadata":
                record["request_kind"] = "metadata"
            await self._emit(record)

    async def record_rejection(
        self, *, reason: str, bypass: bool, upstream_reached: bool = False
    ) -> None:
        """Record a boundary refusal without reading or retaining its body."""

        async with self._lock:
            self._cursor += 1
            timestamp = _iso_utc(self._now_factory().astimezone(timezone.utc))
            record = {
                "schema": RECEIPT_SCHEMA,
                "cursor": self._cursor,
                "receipt_id": self._receipt_id_factory(),
                "request_id": f"transport-rejected-{uuid.uuid4()}",
                "run_id": self._request.run_id,
                "binding_digest": self._request.binding_digest,
                "runtime_set_digest": self._request.runtime_set_digest,
                "policy_digest": self._request.policy_digest,
                "account_revision": self._request.account.revision,
                "provider_mode": self._request.account.provider_mode,
                "model": "unparsed",
                "started_at": timestamp,
                "ended_at": timestamp,
                "outcome": "failed",
                "upstream_reached": upstream_reached,
                "accounting_status": "rejected",
                "input_tokens_before": 0,
                "input_tokens_after": 0,
                "saved_tokens": 0,
                "transforms": [],
                "bypass_reason": reason if bypass else None,
                "error_reason": None if bypass else reason,
            }
            await self._emit(record)

    async def _emit(self, record: dict[str, Any]) -> None:
        result = self._sink(record)
        if inspect.isawaitable(result):
            await result
