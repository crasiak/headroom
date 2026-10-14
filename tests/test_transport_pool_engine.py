"""In-process checks of what a pool's leases share and what they must not."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
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
    lock = threading.Lock()
    release = threading.Event()

    def job(label: str):
        def work() -> str:
            with lock:
                running[label] += 1
                peak[label] = max(peak[label], running[label])
            release.wait(10)
            with lock:
                running[label] -= 1
            return label

        return work

    async def run(fn, *, timeout):
        return await asyncio.to_thread(fn)

    engine = SimpleNamespace(_run_compression_in_executor=run)
    first = _LeaseProxy(engine, client=None, concurrency=2)  # type: ignore[arg-type]
    second = _LeaseProxy(engine, client=None, concurrency=2)  # type: ignore[arg-type]
    try:
        flood = [
            asyncio.create_task(first._run_compression_in_executor(job("a"), timeout=5))
            for _ in range(6)
        ]
        await _until(lambda: running["a"] == 2)
        await asyncio.sleep(0.05)
        assert running == {"a": 2, "b": 0}
        # The other lease is not queued behind the flood.
        other = asyncio.create_task(second._run_compression_in_executor(job("b"), timeout=5))
        await _until(lambda: running["b"] == 1)
        assert running == {"a": 2, "b": 1}
        release.set()
        assert await other == "b"
        assert await asyncio.gather(*flood) == ["a"] * 6
        assert peak == {"a": 2, "b": 1}
    finally:
        release.set()


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


class _Blocker:
    """A compression job that runs until the test lets it go."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0

    def __call__(self) -> str:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            self.gate.wait(10)
            return "late"
        finally:
            with self.lock:
                self.running -= 1


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def test_one_leases_stuck_compression_quarantines_only_that_lease(pool) -> None:
    from headroom.proxy.server import CompressionQuarantinedError

    pool.build_engine()
    stuck_lease = _LeaseProxy(pool.engine, client=None, concurrency=2)  # type: ignore[arg-type]
    other_lease = _LeaseProxy(pool.engine, client=None, concurrency=2)  # type: ignore[arg-type]
    blocker = _Blocker()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await stuck_lease._run_compression_in_executor(blocker, timeout=0.1)
        assert stuck_lease.compression_snapshot() == {"jobs": 1, "leaked": 1}
        # The engine still accounts for the debt, engine-wide.
        assert pool.engine._compression_timed_out_in_flight == 1

        # The stuck lease is quarantined; the other lease compresses normally.
        with pytest.raises(CompressionQuarantinedError, match="lease compression quarantined"):
            await stuck_lease._run_compression_in_executor(lambda: "x", timeout=5)
        assert await other_lease._run_compression_in_executor(lambda: "ok", timeout=5) == "ok"
        assert pool.engine._compression_quarantine_activations == 0

        # Quarantined until the worker actually finishes, then it recovers.
        await asyncio.sleep(0.2)
        with pytest.raises(CompressionQuarantinedError):
            await stuck_lease._run_compression_in_executor(lambda: "x", timeout=5)
        blocker.gate.set()
        await _until(lambda: stuck_lease.compression_snapshot()["jobs"] == 0)
        assert await stuck_lease._run_compression_in_executor(lambda: "back", timeout=5) == "back"
        assert stuck_lease.compression_snapshot() == {"jobs": 0, "leaked": 0}
        await _until(lambda: pool.engine._compression_leaked_threads == 1)
    finally:
        blocker.gate.set()


async def test_a_timing_out_lease_never_holds_more_than_its_share_of_threads(pool) -> None:
    pool.build_engine()
    # No quarantine window: the slot rule alone must bound the lease.
    pool.engine._compression_quarantine_max_seconds = 0.0
    lease = _LeaseProxy(pool.engine, client=None, concurrency=2)  # type: ignore[arg-type]
    blocker = _Blocker()
    try:
        for _ in range(6):
            with pytest.raises(asyncio.TimeoutError):
                await lease._run_compression_in_executor(blocker, timeout=0.1)
        assert blocker.running == 2
        assert blocker.peak == 2
        assert lease.compression_snapshot() == {"jobs": 2, "leaked": 2}
        # Both slots stay with the stuck workers, not freed by the timeouts.
        assert lease._compression_slots.locked()

        blocker.gate.set()
        await _until(lambda: lease.compression_snapshot()["jobs"] == 0)
        # Both slots came back when the workers finished.
        results = await asyncio.gather(
            lease._run_compression_in_executor(lambda: 1, timeout=5),
            lease._run_compression_in_executor(lambda: 2, timeout=5),
        )
        assert results == [1, 2]
    finally:
        blocker.gate.set()


async def test_a_lease_whose_every_slot_is_stuck_fails_fast_past_the_cap(pool) -> None:
    from headroom.proxy.server import CompressionQuarantinedError

    pool.build_engine()
    # The quarantine cap has expired: only the stuck-slot rule applies.
    pool.engine._compression_quarantine_max_seconds = 0.0
    lease = _LeaseProxy(pool.engine, client=None, concurrency=2)  # type: ignore[arg-type]
    first, second = _Blocker(), _Blocker()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await lease._run_compression_in_executor(first, timeout=0.1)
        # One slot stuck, one free: the lease still compresses, on one slot only.
        assert await lease._run_compression_in_executor(lambda: "free", timeout=5) == "free"
        live = _Blocker()
        running = [
            asyncio.create_task(lease._run_compression_in_executor(live, timeout=5))
            for _ in range(2)
        ]
        await _until(lambda: live.running == 1)
        await asyncio.sleep(0.1)
        assert live.running == 1
        live.gate.set()
        assert await asyncio.gather(*running) == ["late", "late"]
        assert live.peak == 1

        with pytest.raises(asyncio.TimeoutError):
            await lease._run_compression_in_executor(second, timeout=0.1)
        assert lease.compression_snapshot() == {"jobs": 2, "leaked": 2}
        assert lease._compression_slots.locked()

        # Every slot stuck: fail now, not after the whole budget.
        started = time.monotonic()
        with pytest.raises(CompressionQuarantinedError):
            await lease._run_compression_in_executor(lambda: "never", timeout=3)
        assert time.monotonic() - started < 0.5

        # A stuck worker finishing frees its slot for the next compression.
        first.gate.set()
        await _until(lambda: lease.compression_snapshot()["leaked"] == 1)
        assert await lease._run_compression_in_executor(lambda: "again", timeout=5) == "again"
    finally:
        first.gate.set()
        second.gate.set()


async def test_slots_held_by_live_work_still_make_a_lease_wait(pool) -> None:
    pool.build_engine()
    lease = _LeaseProxy(pool.engine, client=None, concurrency=2)  # type: ignore[arg-type]
    live = _Blocker()
    try:
        holders = [
            asyncio.create_task(lease._run_compression_in_executor(live, timeout=5))
            for _ in range(2)
        ]
        await _until(lambda: live.running == 2)
        loop = asyncio.get_running_loop()
        loop.call_later(0.3, live.gate.set)
        started = time.monotonic()
        assert await lease._run_compression_in_executor(lambda: "waited", timeout=5) == "waited"
        assert time.monotonic() - started >= 0.25
        assert await asyncio.gather(*holders) == ["late", "late"]
    finally:
        live.gate.set()


async def test_leases_never_share_the_detection_memo(pool, monkeypatch) -> None:
    """CRA-1001 / CRA-970: the router's content-keyed detection memo is per lease.

    A memo hit is fast, so a shared memo would tell one lease that another sent
    the same bytes. Each lease's compressions run in that lease's own memo
    scope; work outside any lease scope skips the memo.
    """
    import headroom._ort as ort_runtime
    from headroom.transforms import content_router as cr

    calls = {"n": 0}

    def native(content):
        calls["n"] += 1
        return SimpleNamespace(content_type="text", confidence=0.9)

    monkeypatch.setenv("HEADROOM_DETECT_BACKEND", "rust")
    monkeypatch.setattr(ort_runtime, "rust_ort_runtime_compatible", lambda: True)
    monkeypatch.setattr(cr, "_detect_native_unhealthy", False)
    monkeypatch.setattr("headroom._core.detect_content_type", native)

    pool.build_engine()
    assert cr._detection_memo.scope_required is True
    first = _LeaseProxy(pool.engine, None, 2, lease_id="lease-a")  # type: ignore[arg-type]
    second = _LeaseProxy(pool.engine, None, 2, lease_id="lease-b")  # type: ignore[arg-type]
    unscoped = _LeaseProxy(pool.engine, client=None, concurrency=2)  # type: ignore[arg-type]
    messages = [
        {"role": "tool", "content": f"shared tool output line {i} " * 40, "tool_call_id": f"c{i}"}
        for i in range(5)
    ]

    def compress():
        return pool.engine.openai_pipeline.apply(
            messages=[dict(m) for m in messages],
            model="gpt-4o",
            model_limit=200_000,
            protect_recent=0,
        ).messages

    out_a = await first._run_compression_in_executor(compress, timeout=30)
    assert calls["n"] == 5
    assert await first._run_compression_in_executor(compress, timeout=30) == out_a
    assert calls["n"] == 5  # lease A's own warm pass hits its memo

    # Lease B's first pass over identical bytes detects; it never hits A's entries.
    assert await second._run_compression_in_executor(compress, timeout=30) == out_a
    assert calls["n"] == 10
    assert cr._detection_memo.scope_size("lease-a") == 5
    assert cr._detection_memo.scope_size("lease-b") == 5

    # Compression that is not bound to a lease never uses (or fills) the memo.
    await unscoped._run_compression_in_executor(compress, timeout=30)
    assert calls["n"] == 15
    assert cr._detection_memo.scope_size("") == 0

    # A lease's release drops its entries and only its entries.
    assert cr.release_detection_memo_scope("lease-a") == 5
    assert cr._detection_memo.scope_size("lease-a") == 0
    assert cr._detection_memo.scope_size("lease-b") == 5
