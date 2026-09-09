from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

from headroom.proxy.outcome import RequestOutcome
from headroom.transport.protocol import (
    AcquireRequest,
    ProcessIdentity,
    ProtocolError,
    ReceiptWriter,
    canonical_digest,
    current_process_identity,
)


def _digest(label: str) -> str:
    return canonical_digest({"label": label})


def test_canonical_digest_normalizes_integral_floats_for_cross_language_clients() -> None:
    assert canonical_digest({"start_time": 1234.0}) == canonical_digest({"start_time": 1234})


def _acquire_payload(*, upstream_url: str = "http://127.0.0.1:18181") -> dict:
    compression = {
        "required": True,
        "lossless": True,
        "bypass": "forbid",
        "transform_preset": "lossless-v1",
    }
    upstream = {
        "url": upstream_url,
        "digest": canonical_digest(
            {"schema": "headroom.transport.upstream.v1", "url": upstream_url}
        ),
    }
    payload = {
        "schema": "headroom.transport.acquire.v1",
        "run_id": "run-01HZZZZZZZZZZZZZZZZZZZZZZZ",
        "profile_revision": _digest("profile"),
        "lock_hash": _digest("lock"),
        "materialization_id": "mat-01HZZZZZZZZZZZZZZZZZZZZZZZ",
        "harness": "claude-code",
        "venue": "terminal",
        "cwd": "/private/tmp/worktree-a",
        "worktree_identity": _digest("worktree-a"),
        "account": {
            "ref": "account://claude/personal",
            "revision": _digest("account-personal"),
            "credential_locator": "keychain://claude/personal",
            "provider_mode": "anthropic_oauth_passthrough",
        },
        "compression": compression,
        "runtime_set_digest": _digest("runtime-set"),
        "policy_digest": canonical_digest(
            {"schema": "headroom.transport.policy.v1", **compression}
        ),
        "upstream": upstream,
        "parent_process": current_process_identity().to_dict(),
    }
    payload["binding_digest"] = AcquireRequest.binding_digest_for(payload)
    return payload


def test_acquire_request_validates_exact_binding_and_keeps_secrets_as_locators() -> None:
    request = AcquireRequest.from_dict(_acquire_payload())

    assert request.account.provider_mode == "anthropic_oauth_passthrough"
    assert request.account.credential_locator == "keychain://claude/personal"
    assert request.binding_digest == request.computed_binding_digest()


def test_acquire_request_rejects_unknown_secret_shaped_fields() -> None:
    payload = _acquire_payload()
    payload["account"]["token"] = "must-not-be-accepted"

    with pytest.raises(ProtocolError, match=r"account.*unexpected field.*token"):
        AcquireRequest.from_dict(payload)


def test_acquire_request_rejects_non_loopback_cleartext_upstream() -> None:
    payload = _acquire_payload(upstream_url="http://provider.example/v1")

    with pytest.raises(ProtocolError, match="HTTPS or loopback HTTP"):
        AcquireRequest.from_dict(payload)


def test_acquire_request_rejects_digest_drift() -> None:
    payload = _acquire_payload()
    payload["compression"]["transform_preset"] = "changed-after-prepare"

    with pytest.raises(ProtocolError, match="policy_digest mismatch"):
        AcquireRequest.from_dict(payload)


def test_process_identity_detects_stale_pid_reuse(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = ProcessIdentity(pid=1234, start_source="psutil", start_time=1000.0)
    monkeypatch.setattr(
        "headroom.transport.protocol.current_process_identity",
        lambda pid=None: ProcessIdentity(pid=1234, start_source="psutil", start_time=2000.0),
    )

    assert recorded.matches_live_process() is False


def test_process_identity_rechecks_with_the_recorded_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = ProcessIdentity(pid=1234, start_source="ps", start_time=1000.0)
    monkeypatch.setattr(
        "headroom.transport.protocol._ps_process_identity",
        lambda pid: ProcessIdentity(pid=pid, start_source="ps", start_time=1000.0),
    )
    monkeypatch.setattr(
        "headroom.transport.protocol.current_process_identity",
        lambda pid=None: ProcessIdentity(pid=1234, start_source="psutil", start_time=1000.0),
    )

    assert recorded.matches_live_process() is True


def test_ready_record_contains_binding_not_account_locator() -> None:
    request = AcquireRequest.from_dict(_acquire_payload())
    record = request.ready_record(
        endpoint="http://127.0.0.1:49321",
        process_identity=current_process_identity(),
        lease_id="lease-01HZZZZZZZZZZZZZZZZZZZZZZZ",
    )

    encoded = json.dumps(record.to_dict(), sort_keys=True)
    assert record.status == "ready"
    assert record.binding_digest == request.binding_digest
    assert record.child_environment.set == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:49321"}
    assert record.child_environment.clear == (
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
    assert "keychain" not in encoded
    assert "credential" not in encoded


@pytest.mark.asyncio
async def test_zero_savings_receipt_is_routed_and_metadata_only() -> None:
    request = AcquireRequest.from_dict(_acquire_payload())
    records: list[dict] = []
    writer = ReceiptWriter(
        request,
        records.append,
        receipt_id_factory=lambda: "receipt-01",
        now_factory=lambda: datetime(2026, 9, 8, 12, 0, 0, 250000, tzinfo=timezone.utc),
    )

    await writer.record(
        RequestOutcome(
            request_id="request-01",
            provider="anthropic",
            model="claude-test",
            original_tokens=12,
            optimized_tokens=12,
            output_tokens=4,
            tokens_saved=0,
            attempted_input_tokens=12,
            total_latency_ms=250.0,
            transforms_applied=(),
            tags={},
        )
    )

    assert records == [
        {
            "schema": "headroom.transport.receipt.v1",
            "cursor": 1,
            "receipt_id": "receipt-01",
            "request_id": "request-01",
            "run_id": request.run_id,
            "binding_digest": request.binding_digest,
            "runtime_set_digest": request.runtime_set_digest,
            "policy_digest": request.policy_digest,
            "account_revision": request.account.revision,
            "provider_mode": "anthropic_oauth_passthrough",
            "model": "claude-test",
            "started_at": "2026-09-08T12:00:00Z",
            "ended_at": "2026-09-08T12:00:00.250000Z",
            "outcome": "succeeded",
            "upstream_reached": True,
            "accounting_status": "evaluated",
            "input_tokens_before": 12,
            "input_tokens_after": 12,
            "saved_tokens": 0,
            "transforms": [],
            "bypass_reason": None,
            "error_reason": None,
        }
    ]
    assert "authorization" not in json.dumps(records)


@pytest.mark.asyncio
async def test_required_compression_classifies_passthrough_as_failure() -> None:
    request = AcquireRequest.from_dict(_acquire_payload())
    records: list[dict] = []
    writer = ReceiptWriter(request, records.append, receipt_id_factory=lambda: "receipt-02")

    await writer.record(
        RequestOutcome(
            request_id="request-02",
            provider="anthropic",
            model="claude-test",
            original_tokens=100,
            optimized_tokens=100,
            output_tokens=0,
            tokens_saved=0,
            attempted_input_tokens=100,
            transforms_applied=(),
            tags={"passthrough_reason": "compression_timeout", "authorization": "secret"},
        )
    )

    assert records[0]["outcome"] == "failed"
    assert records[0]["accounting_status"] == "rejected"
    assert records[0]["bypass_reason"] == "compression_timeout"
    assert "secret" not in json.dumps(records[0])


def test_current_process_identity_matches_itself() -> None:
    identity = current_process_identity(os.getpid())

    assert identity.pid == os.getpid()
    assert identity.matches_live_process() is True
