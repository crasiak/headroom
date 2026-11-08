# [LRN:api-drift] Audit the materialized npm bundle, not lock-only metadata

## What happened

Replacing npm with CRA-1534's verified patched archive through npm 11.19.1
`install --package-lock-only` retained 147 old bundled lock entries and still
reported 35 vulnerabilities. A fresh lock-only resolution then reported zero
but omitted all bundled entries. Actual script-disabled materialization added
all 193 real bundled entries; its complete audit also reported zero. Frozen
consumer `ci` then exposed another requirement: a dependency's own root overrides
are not inherited by the consumer. The outer npm-scoped profile must describe
glob14/minimatch11/ip-address10/undici6 too, while ordinary undici8 stays separate.

## Why wrong

An incomplete lock's zero count does not establish coverage. Neither a new
root version nor a successful resolver proves which bundled copies ship.
Silently removing old lock entries would hide evidence rather than repair it.

## What's correct

Preserve failed inputs/logs. Verify archive integrity, require its entire package
inventory to equal the independently audited build lock, materialize cleanly with
lifecycle scripts disabled, and audit the complete resulting lock. Confirm
frozen ci reproduces that inventory with the complete scoped consumer overrides.
Keep all original lock audits and the new
independent build-lock audit.

## Prevention rule

Run the artifact inventory guard and installed-inventory smoke test. Never use
lock-only zero vulnerabilities alone as evidence for a custom bundled artifact.

Tags: self-learning, api-drift, npm, security, audit, lockfile
