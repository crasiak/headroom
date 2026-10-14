"""CRA-1001: the router must not re-run content detection on every turn.

``ContentRouter.apply`` detects every eligible string message (OpenAI chat
``role:"tool"`` messages, and the isolated transport's Responses
``function_call_output`` items) *before* it consults the compression cache.
With the native detector that is one Magika ONNX inference per message per
turn, so a 150-220k-token Responses context with ~70 eligible tool outputs
re-detected its whole history every turn even when every message was a cache
hit. On a contended host that took ``content_router`` to 25-77 s, past the
30 s compression budget (``503 compression_failed`` under
``compression_required``).

``_detect_content`` is now memoized on the exact probe bytes plus the detector
route. These tests pin that the warm pass does no detection work, that the
memo is exact and bounded, and that routed output is byte-identical with the
memo on or off.
"""

from __future__ import annotations

import json
import random
import time
from types import SimpleNamespace

import pytest

import headroom._ort as ort_runtime
from headroom.tokenizer import Tokenizer
from headroom.tokenizers.estimator import EstimatingTokenCounter
from headroom.transforms import content_router as cr
from headroom.transforms.content_detector import ContentType
from headroom.transforms.content_router import ContentRouter, ContentRouterConfig

# Per-call latency of the fake native detector. ORT inference on a loaded host
# was observed at hundreds of ms per call; 30 ms keeps the test quick while
# making a regression (re-detecting every warm message) cost seconds.
_SLOW_DETECT_SECONDS = 0.03
# The proxy's compression budget (COMPRESSION_TIMEOUT_SECONDS).
_COMPRESSION_BUDGET_SECONDS = 30.0


class _FakeNative:
    """Stand-in for ``headroom._core.detect_content_type`` that counts calls."""

    def __init__(self, content_type: str = "text", delay: float = 0.0) -> None:
        self.calls = 0
        self.content_type = content_type
        self.delay = delay
        self.fail_next = 0

    def __call__(self, content: str) -> SimpleNamespace:
        self.calls += 1
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("simulated native failure")
        if self.delay:
            time.sleep(self.delay)
        return SimpleNamespace(content_type=self.content_type, confidence=0.9)


@pytest.fixture(autouse=True)
def _native_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the native route with a mocked runtime, regardless of whether this
    # interpreter can load ORT (mirrors tests/test_transforms/test_detect_fallback_1123.py).
    monkeypatch.setenv("HEADROOM_DETECT_BACKEND", "rust")
    monkeypatch.setattr(ort_runtime, "rust_ort_runtime_compatible", lambda: True)
    monkeypatch.setattr(cr, "_detect_native_unhealthy", False)
    monkeypatch.setattr(cr, "_detect_panic_warned", False)
    monkeypatch.setattr(cr, "_detect_backend_warned", False)


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeNative) -> _FakeNative:
    # String target: other tests rebuild headroom._core, so never patch an alias.
    monkeypatch.setattr("headroom._core.detect_content_type", fake)
    return fake


# ---------------------------------------------------------------------------
# Synthetic transport-shaped context (no real content)
# ---------------------------------------------------------------------------


def _code(rng: random.Random, funcs: int) -> str:
    lines = []
    for _ in range(funcs):
        lines.append(f"def fn_{rng.randrange(10**6)}(a{rng.randrange(99)}, b{rng.randrange(99)}):")
        for k in range(rng.randrange(6, 12)):
            lines.append(f"    v{k}_{rng.randrange(10**5)} = op_{rng.randrange(10**5)}({k})")
        lines.append("    return None\n")
    return "\n".join(lines)


def _prose(rng: random.Random, sentences: int) -> str:
    words = ["lease", "router", "budget", "cache", "turn", "token", "verdict", "stream"]
    return " ".join(
        " ".join(rng.choice(words) + str(rng.randrange(100)) for _ in range(12)) + "."
        for _ in range(sentences)
    )


def _grep(rng: random.Random, lines: int) -> str:
    path = f"src/pkg/mod_{rng.randrange(9)}/deep/nested/file_{rng.randrange(9)}.py"
    return "\n".join(f"{path}:{i + 1}:    x = lookup_{rng.randrange(50)}()" for i in range(lines))


def _error(rng: random.Random) -> str:
    frames = "\n".join(
        f'  File "/srv/m{rng.randrange(30)}.py", line {rng.randrange(900)}, in f{rng.randrange(999)}'
        for _ in range(10)
    )
    return f"Traceback (most recent call last):\n{frames}\nValueError: bad input — command failed"


def _transport_messages(*, seed: int = 1001, scale: float = 1.0) -> list[dict[str, str]]:
    """The forwarder's Responses → router mapping: one ``role:"tool"`` per output.

    Route mix mirrors the CRA-1001 episode (109 msgs: 28 small, 13 error
    outputs, 62 poorly-compressible, 6 compressible).
    """
    rng = random.Random(seed)
    kinds = ["small"] * 28 + ["error"] * 13 + ["code"] * 40 + ["prose"] * 22 + ["grep"] * 6
    rng.shuffle(kinds)
    build = {
        "small": lambda: f"ok {rng.randrange(1000)}",
        "error": lambda: _error(rng),
        "code": lambda: _code(rng, max(1, int(rng.randrange(14, 30) * scale))),
        "prose": lambda: _prose(rng, max(1, int(rng.randrange(80, 160) * scale))),
        "grep": lambda: _grep(rng, max(1, int(400 * scale))),
    }
    return [
        {"role": "tool", "content": build[kind](), "tool_call_id": f"call_{i:04d}"}
        for i, kind in enumerate(kinds)
    ]


def _apply(router: ContentRouter, messages: list[dict[str, str]]):
    return router.apply(
        [dict(m) for m in messages],
        Tokenizer(EstimatingTokenCounter()),
        model_limit=200_000,
        protect_recent=0,
    )


def _fingerprint(result) -> str:
    return json.dumps(
        {"messages": result.messages, "transforms": result.transforms_applied},
        sort_keys=True,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Regression guard: the large-context warm pass
# ---------------------------------------------------------------------------


def test_warm_pass_on_large_context_does_no_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeNative(delay=_SLOW_DETECT_SECONDS))
    messages = _transport_messages(scale=1.0)
    tokens = sum(EstimatingTokenCounter().count_text(m["content"]) for m in messages)
    assert tokens >= 150_000, "the guard must exercise a CRA-1001-sized context"

    router = ContentRouter(ContentRouterConfig(lossless=True))
    _apply(router, messages)
    cold_calls = fake.calls
    # Every eligible message was detected once on the cold pass.
    assert cold_calls >= 60

    fake.calls = 0
    started = time.perf_counter()
    _apply(router, messages)
    warm_seconds = time.perf_counter() - started

    # Before the memo the warm pass re-detected every eligible message
    # (cold_calls × the per-call cost); now it does none.
    assert fake.calls == 0
    # Well under the budget, and under the detection cost it no longer pays.
    assert warm_seconds < _COMPRESSION_BUDGET_SECONDS / 10
    assert warm_seconds < cold_calls * _SLOW_DETECT_SECONDS


def test_new_turn_detects_only_new_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeNative())
    messages = _transport_messages(scale=0.2)
    router = ContentRouter(ContentRouterConfig(lossless=True))
    _apply(router, messages)

    rng = random.Random(7)
    appended = messages + [
        {"role": "tool", "content": _prose(rng, 60), "tool_call_id": "call_new_1"},
        {"role": "tool", "content": _code(rng, 10), "tool_call_id": "call_new_2"},
    ]
    fake.calls = 0
    _apply(router, appended)
    assert fake.calls == 2


# ---------------------------------------------------------------------------
# Correctness: routed output is unchanged by the memo
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["native", "python"])
def test_routed_output_byte_identical_with_and_without_memo(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    if backend == "python":
        monkeypatch.setenv("HEADROOM_DETECT_BACKEND", "python")
    else:
        _install(monkeypatch, _FakeNative(content_type="text"))
    messages = _transport_messages(seed=42, scale=0.2)

    def _three_passes(memo_entries: str) -> list[str]:
        monkeypatch.setenv(cr._DETECT_MEMO_ENTRIES_ENV, memo_entries)
        cr._detection_memo.clear()
        router = ContentRouter(ContentRouterConfig(lossless=True))
        return [_fingerprint(_apply(router, messages)) for _ in range(3)]

    without_memo = _three_passes("0")
    with_memo = _three_passes("4096")

    assert with_memo == without_memo
    # The fixture compresses something, so "identical" is not trivially "noop".
    assert any("router:" in fp and "router:noop" not in fp for fp in with_memo)


def test_disabled_memo_detects_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeNative())
    monkeypatch.setenv(cr._DETECT_MEMO_ENTRIES_ENV, "0")
    for _ in range(3):
        cr._detect_content("plain text that is long enough to detect")
    assert fake.calls == 3
    assert len(cr._detection_memo) == 0


# ---------------------------------------------------------------------------
# Memo exactness and bounds
# ---------------------------------------------------------------------------


def test_memo_is_keyed_on_exact_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeNative())
    cr._detect_content("alpha content")
    cr._detect_content("alpha content")
    cr._detect_content("alpha content ")  # one trailing byte differs
    cr._detect_content("alpha content\ud800")  # lone surrogate is a distinct key
    assert fake.calls == 3


def test_native_verdict_not_served_after_degrading_to_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fake claims SOURCE_CODE; the regex detector calls this JSON.
    _install(monkeypatch, _FakeNative(content_type="source_code"))
    content = '[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}, {"id": 3, "name": "c"}]'
    assert cr._detect_content(content).content_type is ContentType.SOURCE_CODE

    # The circuit breaker trips (#575): later calls must use the regex
    # detector, exactly as without the memo.
    monkeypatch.setattr(cr, "_detect_native_unhealthy", True)
    expected = cr._regex_detect_content_type(content).content_type
    assert expected is not ContentType.SOURCE_CODE
    assert cr._detect_content(content).content_type is expected


def test_fallback_verdicts_are_not_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeNative(content_type="source_code"))
    fake.fail_next = 1
    content = "some content the native detector fails on once"

    first = cr._detect_content(content)  # degraded to regex
    assert first.content_type is not ContentType.SOURCE_CODE
    second = cr._detect_content(content)  # native retried, as without the memo
    assert second.content_type is ContentType.SOURCE_CODE
    assert fake.calls == 2


def test_failed_ort_preflight_is_not_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeNative(content_type="source_code"))
    monkeypatch.setattr(ort_runtime, "rust_ort_runtime_compatible", lambda: False)
    cr._detect_content("content seen while ORT is incompatible")
    assert cr._detect_native_unhealthy is True
    assert len(cr._detection_memo) == 0


def test_memo_is_bounded_lru(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeNative())
    monkeypatch.setenv(cr._DETECT_MEMO_ENTRIES_ENV, "2")
    cr._detect_content("one")
    cr._detect_content("two")
    cr._detect_content("one")  # refreshes "one"
    cr._detect_content("three")  # evicts "two", the least recently used
    assert len(cr._detection_memo) == 2

    fake.calls = 0
    cr._detect_content("one")
    cr._detect_content("three")
    assert fake.calls == 0
    cr._detect_content("two")
    assert fake.calls == 1


def test_memo_hits_are_isolated_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEADROOM_DETECT_BACKEND", "python")
    content = json.dumps([{"id": i, "v": "x"} for i in range(20)])
    miss = cr._detect_content(content)
    miss.metadata["poisoned_by_miss"] = True
    hit = cr._detect_content(content)
    hit.metadata["poisoned_by_hit"] = True
    again = cr._detect_content(content)
    assert again == cr._regex_detect_content_type(content)
    assert "poisoned_by_miss" not in again.metadata
    assert "poisoned_by_hit" not in again.metadata


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", 4096), ("  ", 4096), ("abc", 4096), ("-1", 4096), ("0", 0), ("17", 17)],
)
def test_memo_capacity_env(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    monkeypatch.setenv(cr._DETECT_MEMO_ENTRIES_ENV, raw)
    assert cr._detect_memo_capacity() == expected
