---
status: completed
phase: runtime-candidate
activation_status: activated-new-launches
source_revision: 584f22a342c19dd3320d4f4055f9a82320c9f5fd
---
# Dedicated runtime candidate

Prepared `/Users/jws/code/headroom-worktrees/crash-attribution/.venv/bin/headroom` for new-launch qualification. No global executable link, deployed worktree, old virtualenv or running process was changed.

## Provenance and changes

- APFS clone: `cp -cR /Users/jws/code/headroom-worktrees/headroom-hacking/.venv /Users/jws/code/headroom-worktrees/crash-attribution/.venv`.
- Rewrote 46 Python console-script shebangs in the new `bin/` directory to the new absolute interpreter. Changed only the clone's `headroom_ai.pth` from `cc-headroom-hack` to `crash-attribution` and its pyvenv prompt. Original venv and original editable path remain untouched.
- Replaced this candidate's ignored native extension symlink with an independent copied file. SHA-256: `82bf8d75ba316dd35a1e1be8c7f2ae940069ca355cf880927d5a54635fc58966`.
- All 144 installed distribution METADATA hashes match the source clone. No pip/uv install, dependency upgrade or network dependency resolution was performed.
- The interpreter remains a symlink to the existing uv-managed CPython 3.11.15 installation. Ledger's runtime-set qualification hashes its exact resolved interpreter, Python dependency roots, package tree and virtualenv metadata.

The deployed console script's shebang still refers to `cc-headroom-hack/.venv/bin/python`; `headroom-hacking/.venv/bin/python -I -B` also imports `cc-headroom-hack/headroom`. This is why merely redirecting the global console link to a script using that shared venv would not activate the new code.

## Verification

`candidate/.venv/bin/python -I -B` reports:

- Interpreter: `/Users/jws/code/headroom-worktrees/crash-attribution/.venv/bin/python`
- Package: `/Users/jws/code/headroom-worktrees/crash-attribution/headroom/__init__.py`
- Prefix: `/Users/jws/code/headroom-worktrees/crash-attribution/.venv`

`LEDGER_TEST_HEADROOM_EXECUTABLE=/Users/jws/code/headroom-worktrees/crash-attribution/.venv/bin/headroom go test ./cmd/ledger -run '^TestActualHeadroomTransportTwoAccountFakeProviderGate$' -count=1 -v` in the Ledger crash-attribution worktree:

> --- PASS: TestActualHeadroomTransportTwoAccountFakeProviderGate (18.51s)
> PASS
> ok github.com/jws/ledger/cmd/ledger 19.261s

[Exact output](verification/crossrepo-qualified-runtime.txt). The test uses two concurrent local fake providers/native fixtures, qualifies the complete runtime set and preserves separate bindings/account headers/compression evidence. One `lifecycle evidence unavailable` warning exposed transient shared-budget-lock contention in concurrent journal creation; this was reported to the parent for correction before activation. The provider gate does not establish that both supervisor journals were complete.

## Deployed-source comparison

Actual `cc-headroom-hack` HEAD is baseline `f3e5e69c002f8d5ac5b621fef339f045781501b9`. Its dirty tracked file is `.serena/project.yml`; its untracked files are rendered preview documentation. Comparing bytes over the union of tracked `headroom/`, `pyproject.toml` and `uv.lock` identifies only intended candidate changes:

- `headroom/proxy/server.py`
- `headroom/transport/protocol.py`
- `headroom/transport/runtime.py`
- added `headroom/transport/diagnostics.py`

Activating this candidate would not remove unrelated deployed Python or dependency changes.

## Activation caveats and rollback

This is a dedicated, independently copied runtime closure with editable package source, not a read-only wheel installation. Freeze edits to its production package tree while it is used for qualified launches. Use the absolute `bin/headroom` path; inherited activation-shell helper text was not repointed because deployment uses console-script shebangs, not shell activation.

The persistent Ledger daemon runtime-set cache uses the unresolved input executable path and a ten-minute TTL. Swapping the same `~/.local/bin/headroom` symlink can continue producing the old cached resolved runtime until expiry. Parent must wait for cache expiry or deliberately invalidate it, then inspect a freshly prepared run's Headroom component path/digest. Existing process leases remain untouched; rollback is restoring the previous link for subsequent launches, with the same cache consideration.

Global activation is pending the parent rollout step and bounded Headroom log-retention integration.

## Activation verification, September 18 UTC

Parent activated both `~/.local/bin/headroom` (02:59 UTC) and the actual Ledger daemon-resolved `~/.headroom/bin/headroom` (03:05 UTC). Atomic symlink replacements retain both previous targets in `/Users/jws/.ledger/backups/crash-attribution-20260917/`; no Nix-store file or existing loaded process changed. This is a local launcher override, not declarative Home Manager promotion.

After the resident daemon runtime cache expired, installed personal/work Codex and Claude version-only launches all returned exit0 with complete normal21-event Ledger timelines, no gaps, and typed transport boot/shutdown. The six-file launch source manifest matches this candidate exactly; selected loaded-function and runtime-set fingerprints are retained. No inference call was made. The initial post-cache verification parser missed the logger prefix; rereading the same retained run with the corrected parser recovered the events.

[Four-binding receipt](/Users/jws/code/attic-worktrees/cra-465-crash-attribution/.sage/work/20260917-ledger-supervisor-journal/verification/installed-four-binding-verification.json). The installed collector subsequently sealed and checksum-verified an actual smoke run bundle containing23 exact-run lifecycle/Headroom events, including boot, shutdown, nativewait, signals and daemonfinish. [Bundle evidence verification](/Users/jws/code/attic-worktrees/cra-465-crash-attribution/.sage/work/20260917-independent-collector/installed-live-run-evidence-verification.json).

Current report: `/Users/jws/corelight/labs/artifacts/jws/reports/ledger/ledger-crash-attribution-implementation-2026-09-17.md`. Existing loaded transports retain their old code. Long-canary/actual-resume acceptance remains tracked by CRA-465/468/469; this successful version-only smoke does not prove paid inference or native resume.
