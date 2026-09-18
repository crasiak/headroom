---
status: completed
phase: implementation-plan
tracker: CRA-467
execution_status: implementation-verified
rollout_status: pending-parent-integration
---
# Plan: Headroom diagnostic fields

**Project:** Headroom; tracked with Ledger integration  
**Date:** 2026-09-17  
**Status:** Approved; implementation verified; parent integration pending  
**Priority:** P1; medium effort  
**Parent:** [Crash-attribution evaluation](ledger-crash-attribution-2026-09-17.md)

## Context and evaluation

Major: repaired `headroom/transport/runtime.py:310` monitors off-loop and distinguishes unavailable observation from confirmed death. It logs the first unavailable/recovery event but omits reader category, attempt count and elapsed duration. `protocol.py:176–276` collapses timeout, OS error and parse failure to None. The neutral `proxy_shutdown reason=lifespan_exit` now truthfully says only that shutdown occurred. Preserve these repaired semantics.

## Goals

- [ ] Explain observation failure and recovery without per-retry log storms.
- [ ] Correlate loaded code, lease/run and bounded request-stage diagnostics.
- [ ] Measure event-loop responsiveness and active work without collecting payloads.

## Approach

Recommend a typed internal observation result with state (live/dead/unavailable) separate from reason (match, missing_pid, identity_mismatch, timeout, os_error, permission_denied, parse_error, unsupported_reader). Keep a compatibility bool/None facade for existing callers. A failed reader followed by successful kill(pid,0) remains unavailable, never live-identity-confirmed. Preserve the underlying reader error even when a secondary probe confirms missing PID. Acquisition still requires positively verified identity.

Aggregate each observation episode: event ID, reader, expected and observed start identity when available, attempt_count, failed_attempt_count, read_elapsed_ms, episode_duration_ms, recovery or terminal outcome. Emit start, bounded periodic summary (at most once/minute), recovery and termination summary. Use monotonic duration and reset counters per episode.

Track locally generated request ID, route enum, start time, status and stage (accepted, validating, upstream_connect, awaiting_headers, streaming, completed, failed, cancelled). A stream of tokens is not a stream of diagnostic records: only stage transitions. Cap active request records; retain overflow count. Add one periodic lag task and counts for active requests/leases/proxies. Health reporting is read-only; it does not start transports. Do not add correlation IDs as metric labels.

Boot evidence records loaded package version, build/source fingerprint, immutable runtime-set digest when supplied, dirty/source identity quality and PID/start identity. An editable source-tree hash read later is not proof of code loaded by a live Python process; capture a launch-time manifest and mark drift/unknown. Reuse CRA-459's approved runtime identity design.

Alternative: log full reader exceptions and request objects. Rejected because it loses stable categories, risks bodies/secrets and creates noise under stress. Preserve existing application logging separately; the incident export admits only allowlisted diagnostic fields.

## Implementation Steps

1. **Tests then typed observation:** modify `headroom/transport/protocol.py`, `tests/test_transport_protocol.py`; add focused timeout/parse/permission/missing/reuse fixtures before implementation. Do not change strict wire fields in place.
2. **Episode diagnostics:** modify `headroom/transport/runtime.py` and `tests/test_transport_parent_monitor.py`. Inject clocks/readers; propagate run/lease context available from the existing binding. Verify nonblocking monitor, bounded retry, complete episode summary, no per-retry logging. Depends on 1.
3. **Request tracking:** proposed `headroom/transport/diagnostics.py` plus `tests/test_transport_diagnostics.py`; instrument `runtime.py` boundary middleware and `headroom/transport/forwarder.py` for actual stages. Inventory `proxy/server.py` Anthropic route separately so both forwarding paths are covered. Counters must decrement in finally for streaming disconnect/cancellation. Track only metadata.
4. **Boot/lag/health:** connect lifecycle-managed metrics task to `runtime.py`; add fingerprint and observation timestamps. Test lag with controlled clock scheduling and verify task teardown. Use existing health handlers after source inspection; do not invent unsupported endpoints or expose private IDs on a public metrics endpoint.
5. **Compatibility and integration:** parse events alongside item 1's journal; link by existing run/lease identity. If the wire protocol needs new fields, define negotiated/versioned compatibility first. Old producer events remain readable and visibly less complete.
6. **Verify then canary:** run focused protocol/parent/transport tests and loop-lag/cancellation tests with fake upstreams; retain output and sample incident. Test in the long-running `headroom-hacking` worktree, preserve unrelated edits, record loaded revision on a named canary before daily rollout.

## Verification and acceptance

| Injected scenario | Required evidence / assertion |
| --- | --- |
| Transient ps timeout, then success | One episode, category=timeout, correct attempts/duration/recovery; no shutdown or request stall |
| OS/permission/parse error | Distinct bounded category; unavailable persists, not confirmed death |
| Missing parent/PID reuse | Confirmed terminal observation with identity evidence and shutdown cause |
| Lease release/control EOF/invalid record | Distinct cause; no raw control body or credentials; monitor tasks stop |
| Upstream timeout/status error/stream cancel | Last known real stage, timing/status; active count returns to zero |
| Event-loop blockage in fixture | Lag rises/recovery observed; sampling task never overlaps or leaks |
| Old loaded process versus changed source tree | Boot fingerprint remains immutable; drift/unknown explicit |

Use local fake HTTP upstreams and synthetic bodies only; deny outbound provider network in fixtures. Independently scan every captured diagnostic and incident-rendered output for secret/body canaries. Assert episode logging remains bounded over 1,000 failed observations. Test both native-wrapper families plus transport forwarder paths. Do not disable the existing real-proxy protection fixture; the prior Windows cleanup test requires deliberate mock coverage because that fixture masks it.

## Dependencies, risks and rollback

Shared event schema from item 1; basic typed reader tests can begin independently. Critical risk is accidentally converting unavailable back into dead or double-counting active streaming requests. Revert diagnostics while preserving the tri-state watchdog repair. Proposed lag interval: 1 second; validate steady-state overhead against an enabled/disabled baseline and cap tracking at 128 active stage records with overflow visibility. Acceptance requires actual measurements, not estimates.

## Open Questions

Review fingerprint propagation under the existing strict transport contract and exact health-field visibility. No prompt/response retention and no paid provider requests are necessary for this verification.

Approved by user, including subsequent autonomous execution and decision recording.
