# CRA-1534: reproducible npm security distribution

This is source packaging for the personal fork, **not a user runtime installer**.
The owner approved maintaining a patched npm distribution instead of weakening
an audit or silently downgrading OpenClaw. OpenClaw/Codex versions stay pinned.

## Inputs and changes

- `recipe.json` pins the published npm 12.2.1 tarball SRI, upstream source revision,
  Node distribution SHA256s, bootstrap versions and this lock's SHA256.
- `package.json` is the reviewed build manifest. The builder derives it from the
  verified upstream manifest and requires exact equality before installation.
- `package-lock.json` covers the complete production dependency payload. It is
  independently included in the all-severity Security audit matrix.
- `scripts/build_security_npm.py` retains upstream CLI code, docs and license,
  discards **all** old bundled dependencies, then installs this reviewed lock
  cleanly with lifecycle scripts disabled. It never patches an installed npm.
- Honest fork version: `12.2.1-crasiak.1`. Target fixes: glob 14.0.3, minimatch
  11.0.4, ip-address 10.7.5 and npm's undici 6.29.1. The ordinary OpenClaw tree's
  undici 8.12.3 and zod 4.6.7 overrides are separate and must remain intact.

Minimatch 11 removes RegExp-based APIs; forcing it under glob 13 is not safe.
The bundle upgrades glob too. Tests exercise workspace discovery, local install,
clean `ci`, list/pack, traversal and bounded adversarial pattern preprocessing.

## Rebuild

Use Python 3.12 (or maintained 3.11 with tar extraction filters). Obtain the
matching official Node **24.22.2** archive from `https://nodejs.org/dist/v24.22.2/`.
Only the three archive names/digests in the recipe are accepted. Verify the
committed SHA256 **before extracting**; keep the archive for the builder's full
verification. Do not use an existing user-managed Node/npm tree.

From the repository root, with `node-v24.22.2-PLATFORM` freshly extracted under
`$tools`, and `$scratch` an owned temporary parent:

```sh
python scripts/build_security_npm.py \
  --work-dir "$scratch/build" --output-dir "$scratch/output" \
  --node "$tools/node-v24.22.2-PLATFORM/bin/node" \
  --npm-cli "$tools/node-v24.22.2-PLATFORM/lib/node_modules/npm/bin/npm-cli.js" \
  --node-distribution "$tools/node-v24.22.2-PLATFORM.tar.gz"
```

Work/output directories must be **new** and separate; retained evidence is not
overwritten. The optional `--source-archive` reuses an already downloaded npm
archive, but still verifies its SRI. The builder verifies every extracted Node
and bootstrap-npm file against the trusted distribution, rejects extra files,
uses private HOME/config/cache, and disables lifecycle hooks for both `ci` and
`pack`. It records recipe, builder, lock and upstream provenance inside the
payload. The external `artifact.json` also records the actual bootstrap archive
identity. The official bootstrap is a pinned build input, not a claim that its
own old dependency payload is patched or deployed.

Build twice into different new work/output directories and compare the `.tgz`
bytes. CI performs this on Linux and runs the offline CLI tests against a fresh
installation of the packed artifact. A matching version label alone is not
bootstrap custody. Native macOS evidence does not substitute for Linux checks.

Install only into an owned disposable project to test the artifact. Set
`SECURITY_NPM_PACKAGE` to its installed `node_modules/npm` and `SECURITY_NPM_LOCK`
to this directory's lock, then run:

```sh
node --test scripts/test_security_npm.mjs
```

The test verifies the **entire installed dependency inventory** against the
audited lock, not just the four known fixes. No provider, model or live gateway
is contacted. Frozen full wrap/plugin/image qualification remains an additional
gate; these small tests do not discharge it.

## Lock maintenance and audit coverage

`--prepare --work-dir NEW_PATH` creates disposable verified upstream source with
the patched manifest but does not build or publish anything. Use the pinned,
verified toolchain and the builder's isolated environment to regenerate its lock.
The recorded **nested** strategy avoids npm's nonconverging hoisted replacement
loop for this cross-major override closure. Preserve failed logs; do not increase
timeouts blindly or change strategy after generating the lock. Review the full
closure, update the committed manifest/lock/digest together, then rebuild/review.

Important npm metadata behavior: `--package-lock-only` may preserve old bundled
entries when replacing npm, or omit them in a fresh lock. A zero count from an
incomplete lock is **not** full bundle audit evidence. Materialize the verified
artifact with scripts disabled, require all bundled entries/inventory to match,
then audit the complete materialized lock. The independent build lock's full
audit remains required too. Never delete source lock entries to conceal findings.

All five original npm lock audits remain, plus this sixth lock. Keep every
severity and development dependencies; no ignores, force-fix, omitted scope or
baseline waiver. The builder rejects any shipped package inventory differing
from the complete audited build lock.

## Publication and retirement

Publish a qualified, content-hashed prerelease artifact on **crasiak/headroom**,
with source revision and the external manifest. Never replace an existing asset
under the same fork version. Consumers pin its integrity; source publication is
not runtime adoption. Keep repository credentials out of artifacts and fixtures.

Owner **jws** owns this fork's maintenance. Retire it only after an upstream
release's actual packed copies clear all audits and pass the same compatibility
and materialized-inventory checks. Upstream issue closure alone is not proof.

Python is a separate approved support change: this fork now requires **>=3.11**
for patched filelock >=4.2.1, alongside LiteLLM >=1.101.3 security constraints.
No vulnerable Python 3.10 marker fallback is permitted. The existing conservative
static-analysis dialect and native ABI floor do not grant Python 3.10 support.
