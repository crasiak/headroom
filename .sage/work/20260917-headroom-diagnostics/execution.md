---
status: in-progress
phase: implementation-verified
tracker: CRA-467
rollout_status: pending-parent-integration
---
# Headroom crash-attribution implementation

User approved the four plans and autonomous implementation while away. This work implements CRA-467 in the isolated `feat/crash-attribution` worktree from baseline `f3e5e69c`. No live Headroom process was restarted and no upstream provider was contacted. Parent integration/rollout remains outstanding, so this initiative is not marked complete.

## Decisions

- Keep transport v1 unchanged. The existing validated acquire `run_id` and `runtime_set_digest`, plus the generated lease ID, correlate diagnostic records. Diagnostics use an independent `headroom.transport.diagnostic.v1` schema in existing stderr logging.
- Follow the parent integration decision: existing Ledger per-run Headroom logs persist this stream. Do not create another retention directory or duplicate diagnostic file. Collector reads only exact-schema JSON and reconstructs an allowlist; ordinary mixed log text must never enter incident export.
- Preserve identity-or-None reader helpers and bool/None process-observation facade. Context-local reader error categories are transported to a frozen observation dataclass. Unknown or failed readers remain unavailable when kill(pid,0) succeeds; acquisition still requires positively verified identity. A secondary missing-PID probe retains the primary reader error.
- Keep parent inspection off-loop. Aggregate one unavailable episode with UUID, attempt/failure counts, cumulative read time, monotonic episode duration, expected/observed identity, initial event, no more than one summary/minute, and recovery/terminal/monitor-stop event. Existing legacy first-failure/recovery lines remain for compatibility.
- Instrument the ASGI lifetime once and install a context-local httpx trace hook on both existing HTTP clients. This covers Anthropic handlers and transport forwarder using their actual network trace boundaries without editing their compression, retry, response-byte or receipt policy. A trace is not a body/headers log. `streaming` means upstream headers have arrived and response body consumption is beginning, including buffered responses.
- Bound retained active request records to 128. Overflow retains counters only. Each tracked request emits at most 32 nonterminal transitions plus accepted/terminal events. No request bodies, header values, URLs, exception text or supplied attribution IDs are admitted. Streaming disconnect, raised error and cancellation all decrement active state in finally.
- Sample loop lag with one lifecycle-owned task at one-second intervals and emit an aggregate at most once/minute. Existing /health and /readyz expose read-only counters/lag, never run, lease, request or source identities. Diagnostic sink loss is counted and does not alter provider behavior.
- Capture boot once: package version, process start identity, verified runtime-set digest, launch disk manifest hash and separate hash of selected loaded Python function code. Mark full loaded package identity/dirty state unknown and source drift not checked. A disk manifest does not prove every imported/native dependency's loaded revision. Selected loaded function fingerprints stay stable after disk-only mutation.

## Verification

Interpreter: `/Users/jws/code/headroom-worktrees/headroom-hacking/.venv/bin/python`, with `PYTHONPATH=.` in this isolated worktree. Reused the already-built ABI3 core via ignored `headroom/_core.abi3.so` symlink to the hacking worktree; no dependency upgrades or live runtime changes. Existing real-proxy protection fixture remained enabled. The initial extension import failure was resolved by this local test-environment link.

TDD: first focused test run failed with eight missing-API/module errors, then implementation made the tests pass. Exact outputs are retained under [verification](verification/).

Commands and actual results:

- `python -m pytest tests/test_transport_diagnostics.py tests/test_transport_parent_monitor.py tests/test_transport_protocol.py tests/test_transport_runtime.py tests/test_transport_serve.py tests/test_transport_modes.py tests/test_transport_metadata.py tests/test_transport_count_tokens.py tests/test_transport_native_completion.py -q --tb=short` — **89 passed in 36.26s**. [Output](verification/transport-suite.txt).
- `python -m pytest tests/test_proxy_healthchecks.py tests/test_proxy_loop_exception_health.py tests/test_proxy_health.py -q --tb=short` — **31 passed in 7.03s**. [Output](verification/health-suite.txt).
- Additional diagnostics tests covering unknown readers, sink failure, and disk-only mutation: **24 passed in 7.19s**. [Output](verification/diagnostics-suite.txt).
- Additional hard transition cap and all three control shutdown causes: [output](verification/bounds-control-suite.txt).
- Ruff checks on changed transport production modules and diagnostics tests passed; `git diff --check` passed. The three-line health integration is covered by the wider health suite.

Fault injection covers timeout, permission/OS/parse errors, missing PID, reused PID, recovery, 1,000 failed observations, bounded repeated request transitions, upstream header timeout, status error, cancellation/disconnect, event-loop blockage/recovery, lease release, control EOF, invalid control record and disk mutation. Real subprocess loopback fake providers cover Claude/Anthropic and Codex/OpenAI route families, verify the emitted boot/stages/shutdown in stderr, and assert matching run/lease IDs. Both fake transports terminate and are reaped; lag/parent tasks are cancelled and awaited. Fake request/header canaries are independently absent from captured diagnostic JSON. No original provider payload or credential is retained in the sample events.

The examples [fake Claude](verification/fake-claude-events.json) and [fake Codex](verification/fake-codex-events.json) are reconstructed allowlisted diagnostic events from test processes that exited; fingerprints represent the tested source state when captured, not a subsequently assigned commit hash.

## Resource measurement

[Reproducible component benchmark](verification/benchmark.py) measures 10,000 six-stage requests with actual JSON encoding and flushing to a temporary local file (deleted afterward). With tracemalloc enabled, the diagnostic cost was **477.69 microseconds/request** versus **0.014 microseconds/request** for the disabled empty-loop baseline. Peak tracked allocation was **16,762 bytes**, retained allocation **12,494 bytes**, active requests at finish **0**. Six records occupied approximately **2,498 bytes/request**. [Raw result](verification/benchmark-result.json).

This is measured component overhead including instrumentation/profiling and synchronous local-file writes, not an end-to-end provider latency estimate or a storage-retention test. The parent Ledger change owns log retention and bounded collector reads. Slow/stalled stderr destinations can still delay synchronous logging; the independent process collector and loop lag evidence help expose that condition without introducing an unbounded logging queue.

## Remaining integration / rollback

Parent must integrate the Headroom commit with Ledger's collector parser and choose new-launch deployment. No daily rollout or long-running process replacement was performed. Existing loaded processes cannot acquire these diagnostics without being replaced; code verified in the isolated fake-provider canary is not proof of a live daily process's loaded revision.

Rollback this commit to remove added diagnostics while preserving the baseline tri-state parent watchdog repair. No migrations, strict-wire changes or persistent settings were made.
