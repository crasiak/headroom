"""In-process checks of what a pool's leases share and what they must not."""

from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

from headroom import paths
from headroom.proxy.outcome import RequestOutcome
from headroom.proxy.server import HeadroomProxy
from headroom.telemetry.toin import get_toin, reset_toin
from headroom.transport.pool import (
    UNBOUND_UPSTREAM,
    LeaseContext,
    LeaseScopedReceiptWriter,
    TransportPool,
    _LeaseProxy,
    current_lease,
    pool_scope,
)
from headroom.transport.protocol import AcquireRequest, ReceiptWriter, canonical_digest
from tests.test_transport_protocol import _acquire_payload


@pytest.fixture(autouse=True)
def _restore_process_globals(monkeypatch, tmp_path):
    """The engine sets process-wide state; later tests must not inherit it."""

    monkeypatch.setattr(paths, "_PROCESS_STATELESS", paths._PROCESS_STATELESS)
    monkeypatch.setattr(HeadroomProxy, "ANTHROPIC_API_URL", HeadroomProxy.ANTHROPIC_API_URL)
    monkeypatch.setenv("HEADROOM_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("HEADROOM_CONFIG_DIR", str(tmp_path / "config"))
    yield
    reset_toin()


@pytest.fixture
def pool():
    admission, peer = socket.socketpair()
    try:
        yield TransportPool(admission)
    finally:
        admission.close()
        peer.close()


def _request(label: str, **changes) -> AcquireRequest:
    payload = _acquire_payload()
    payload["run_id"] = f"run-{label}"
    payload["cwd"] = f"/private/tmp/{label}"
    payload.update(changes)
    payload["binding_digest"] = AcquireRequest.binding_digest_for(payload)
    return AcquireRequest.from_dict(payload)


def test_the_engine_shares_no_upstream_learning_or_breaker_across_leases(pool) -> None:
    pool.build_engine()
    engine = pool.engine

    # No process-wide upstream a pooled request could fall back to.
    assert HeadroomProxy.ANTHROPIC_API_URL == UNBOUND_UPSTREAM
    assert engine.config.anthropic_api_url == UNBOUND_UPSTREAM
    # Lossless, stateless, and no read lifecycle (it stores originals in a
    # process-wide store and replaces superseded reads with markers).
    assert engine.config.lossless is True
    assert engine.config.stateless is True
    assert engine.config.read_lifecycle is False
    # One lease's failing input cannot open a breaker for every lease.
    assert engine.anthropic_pipeline._breaker_threshold == 0
    assert engine.openai_pipeline._breaker_threshold == 0
    # TOIN is off: nothing one lease sends is learned for another.
    toin = get_toin()
    assert toin._config.enabled is False
    assert toin._config.storage_path == ""
    before = toin.get_stats()
    from headroom.telemetry.models import ToolSignature

    toin.record_compression(
        tool_signature=ToolSignature.from_items([{"id": 1, "name": "secret"}]),
        original_count=10,
        compressed_count=2,
        original_tokens=100,
        compressed_tokens=20,
        strategy="smart_crusher",
    )
    assert toin.get_stats() == before
    assert isinstance(engine.transport_receipt_writer, LeaseScopedReceiptWriter)


def test_each_lease_gets_its_own_http_client(pool) -> None:
    pool.build_engine()
    first = pool.new_http_client()
    second = pool.new_http_client()
    try:
        assert first is not second
        assert first._transport is not second._transport
        assert first is not pool.engine.http_client
    finally:
        asyncio.run(first.aclose())
        asyncio.run(second.aclose())


async def test_engine_receipts_resolve_to_the_requests_own_lease() -> None:
    sinks: dict[str, list[dict]] = {"a": [], "b": []}
    contexts = {}
    for label in ("a", "b"):
        request = _request(label)
        contexts[label] = LeaseContext(
            f"lease-{label}", request, ReceiptWriter(request, sinks[label].append)
        )
    shared = LeaseScopedReceiptWriter()
    outcome = RequestOutcome(
        request_id="r",
        provider="anthropic",
        model="m",
        original_tokens=1,
        optimized_tokens=1,
        output_tokens=0,
        tokens_saved=0,
        attempted_input_tokens=1,
        transforms_applied=(),
        total_latency_ms=1,
        status_code=200,
    )

    async def as_lease(label: str) -> None:
        token = current_lease.set(contexts[label])
        try:
            await asyncio.sleep(0)
            await shared.record(outcome)
            await shared.record_rejection(reason="r", bypass=False)
        finally:
            current_lease.reset(token)

    await asyncio.gather(as_lease("a"), as_lease("b"), as_lease("a"))
    assert [receipt["run_id"] for receipt in sinks["a"]] == ["run-a"] * 4
    assert [receipt["run_id"] for receipt in sinks["b"]] == ["run-b"] * 2
    with pytest.raises(RuntimeError, match="no transport lease"):
        await shared.record(outcome)


async def test_a_lease_holds_at_most_its_share_of_the_compression_executor() -> None:
    running = {"a": 0, "b": 0}
    peak = {"a": 0, "b": 0}
    release = asyncio.Event()

    async def run(fn, *, timeout):
        label = fn()
        running[label] += 1
        peak[label] = max(peak[label], running[label])
        try:
            await release.wait()
        finally:
            running[label] -= 1
        return label

    engine = SimpleNamespace(_run_compression_in_executor=run)
    first = _LeaseProxy(engine, client=None, concurrency=2)  # type: ignore[arg-type]
    second = _LeaseProxy(engine, client=None, concurrency=2)  # type: ignore[arg-type]
    flood = [
        asyncio.create_task(first._run_compression_in_executor(lambda: "a", timeout=5))
        for _ in range(6)
    ]
    await asyncio.sleep(0.05)
    assert running == {"a": 2, "b": 0}
    # The other lease is not queued behind the flood.
    other = asyncio.create_task(second._run_compression_in_executor(lambda: "b", timeout=5))
    await asyncio.sleep(0.05)
    assert running == {"a": 2, "b": 1}
    release.set()
    assert await other == "b"
    assert await asyncio.gather(*flood) == ["a"] * 6
    assert peak == {"a": 2, "b": 1}


async def test_waiting_for_a_compression_share_counts_against_the_timeout() -> None:
    blocked = asyncio.Event()

    async def run(fn, *, timeout):
        await blocked.wait()

    lease = _LeaseProxy(
        SimpleNamespace(_run_compression_in_executor=run),
        client=None,  # type: ignore[arg-type]
        concurrency=1,
    )
    holder = asyncio.create_task(lease._run_compression_in_executor(lambda: None, timeout=5))
    await asyncio.sleep(0)
    with pytest.raises(asyncio.TimeoutError):
        await lease._run_compression_in_executor(lambda: None, timeout=0.05)
    blocked.set()
    await holder


def test_the_first_acquire_pins_the_pool_scope(pool) -> None:
    first = _request("a")
    assert pool.accept_scope(first)
    # Per-lease fields differ freely.
    assert pool.accept_scope(_request("b", cwd="/private/tmp/elsewhere"))
    assert pool.scope_digest == canonical_digest(pool_scope(first))

    other_upstream = "https://upstream.example"
    for changes in (
        {"runtime_set_digest": canonical_digest({"label": "runtime-other"})},
        {"profile_revision": canonical_digest({"label": "profile-other"})},
        {"lock_hash": canonical_digest({"label": "lock-other"})},
        {
            "upstream": {
                "url": other_upstream,
                "digest": canonical_digest(
                    {"schema": "headroom.transport.upstream.v1", "url": other_upstream}
                ),
            }
        },
    ):
        assert not pool.accept_scope(_request("c", **changes)), changes
    account = dict(_acquire_payload()["account"], ref="account://claude/other")
    assert not pool.accept_scope(_request("d", account=account))
    assert pool.scope_digest == canonical_digest(pool_scope(first))


def test_pool_scope_covers_exactly_the_sharing_key() -> None:
    scope = pool_scope(_request("a"))
    assert set(scope) == {
        "schema",
        "account",
        "runtime_set_digest",
        "policy_digest",
        "upstream_digest",
        "profile_revision",
        "lock_hash",
    }


def _replay_turn(state, system: str, messages: list[dict]):
    from headroom.transport.replay import LeaseReplayState

    assert isinstance(state, LeaseReplayState)
    request = SimpleNamespace(headers={})
    body = {"model": "claude-test", "system": system, "messages": messages}
    turn = state.begin(request, "claude-test", body, messages)
    state.record_response(
        turn,
        forwarded=messages,
        original=messages,
        response={"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    )
    return turn


def test_replay_state_is_bounded_owned_and_cleared() -> None:
    from headroom.proxy.models import ProxyConfig
    from headroom.transport.replay import LeaseReplayState

    first = LeaseReplayState(ProxyConfig(), max_sessions=2)
    second = LeaseReplayState(ProxyConfig(), max_sessions=2)
    messages = [{"role": "user", "content": "hello"}]
    turn = _replay_turn(first, "one", messages)
    # The same conversation on another lease starts cold: nothing is shared.
    other = _replay_turn(second, "one", messages)
    assert other.session_id == turn.session_id
    assert other.tracker is not turn.tracker
    assert other.compression_cache is not turn.compression_cache
    assert (
        second.begin(
            SimpleNamespace(headers={}),
            "claude-test",
            {"system": "one"},
            [
                *messages,
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                {"role": "user", "content": "again"},
            ],
        ).previous_forwarded[: len(messages)]
        == messages
    )

    # Least recently used session ids go first, trackers included.
    _replay_turn(first, "two", messages)
    _replay_turn(first, "three", messages)
    assert first.stats() == {"sessions": 2, "trackers": 2}
    assert turn.session_id not in first.compression_caches
    assert all(not key.startswith(turn.session_id) for key in first.trackers._trackers)

    assert first.clear() == {"sessions": 2, "trackers": 2}
    assert first.stats() == {"sessions": 0, "trackers": 0}
    assert first.trackers._lineages == {}
    assert second.stats() == {"sessions": 1, "trackers": 1}
