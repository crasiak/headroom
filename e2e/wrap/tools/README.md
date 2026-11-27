# Wrap E2E CLI dependencies

Codex **0.153.4** and OpenClaw **2026.10.7** remain pinned. CRA-1534's
qualified **npm 12.2.1-crasiak.1** source artifact is used both globally and
inside `/opt/wrap-tools`. Source publication does not change an installed user
npm, Headroom runtime or report scheduler.

[Build recipe, provenance, full inventory and audit contract](npm-security/README.md).
The public prerelease is `crasiak/headroom`, tag `npm-security-cra-1534-1`,
source component `23deae961c662919ce90db6302d38ec0102975a6`. The lock pins its
actual SRI; `npm-security/artifact.json` pins SHA256 and source/build identities.
Never replace existing asset bytes under the same version.

## Scoped security profile

Consumer overrides cannot repair npm's bundled copies. Its source artifact is
rebuilt first with glob14.0.3, minimatch11.0.4, ip-address10.7.5 and undici6.29.1.
The consumer's npm override must retain the **complete** recipe profile,
including `.` referencing `$npm`, so frozen ci does not demand old glob13.
OpenClaw's ordinary minimatch11.0.4, undici8.12.3 and zod4.6.7 remain separate:
never force undici8 into npm's6.x subtree. Keep the MCP security override too.

Registry npm12.2.2 is not equivalent: it still bundles vulnerable glob13 and
minimatch10. No advisories are accepted in `npm-audit-exceptions.json`; CI audits
all six maintained locks, every severity and development dependencies.

## Installation and qualification

Use verified **Node24.22.2 / bootstrap npm11.19.1** in an owned disposable
HOME/config/cache for metadata maintenance, then qualify with the **actual
patched npm12 CLI**. The image takes Node from a digest-pinned stage. Retained
OpenClaw requires Node>=24.22.1 on24.x. No ambient installer substitution.

The image allows only the explicit, integrity-bound root artifact URL with
`--allow-remote=root`; never allow arbitrary transitive URLs. `.npmrc` and
`--strict-allow-scripts` make an unreviewed lifecycle identity a hard failure.
Version-specific `allowScripts` entries require independent hook review;
no wildcard, blanket approval, fallback installer or script-policy bypass.

```sh
cd e2e/wrap/tools
npm ci --allow-remote=root --strict-allow-scripts --no-fund --no-audit
npm test
npm list --depth=0 @openai/codex openclaw npm
cd ../../..
docker build -f e2e/wrap/Dockerfile -t headroom-wrap-e2e .
docker run --rm headroom-wrap-e2e
```

`npm test` retains upstream Ajv URI, minimatch work-bound and Express health
regressions and checks all four bundled security floors with no exceptions.
Both image npm copies additionally run the complete independent inventory and
offline CLI suite. Local `npm ci` is not global installation. Offline CLI tests
alone do not qualify wrap/plugin behavior: run the real isolated fake-provider
E2E too; no live provider/model call is needed.

## Lock maintenance

Keep manifest and lock together and review the complete diff. Do not delete or
trim locks, force repair, omit dev dependencies, reduce severity, add ignore
lists or fallback installs. A lock verifies content; it cannot make the registry
or GitHub available during an outage.

`--package-lock-only` may retain stale bundled entries or omit them. Materialize
the integrity-verified artifact with hooks disabled in owned scratch custody,
require its entire saved inventory to equal the independently audited build
lock, then run patched frozen ci and prove the lock is unchanged. A lock-only
zero count is not full audit coverage. The independent artifact build lock is
part of its signed-off recipe; changing it requires a new reproducible artifact,
version and provenance, not an incidental consumer refresh.

These remain existing test tools with upstream licenses. jws maintains the
patched npm artifact until an upstream bundle passes the same complete packed,
installed and CLI/plugin compatibility gates. Retaining prior artifact evidence
does not qualify a newly merged consumer closure or Docker image.
