# Wrap E2E CLI dependencies

Codex **0.153.4** and OpenClaw **2026.10.7** remain pinned. CRA-1534 adds the
qualified **npm 12.2.1-crasiak.1** source artifact and keeps manifest/lock together.
The image uses the same verified npm distribution globally and in `/opt/wrap-tools`.
No installed user npm, Headroom runtime or report scheduler is changed by this source.

[Build recipe, provenance, full inventory and audit contract](npm-security/README.md).
The public prerelease is on `crasiak/headroom`, tag `npm-security-cra-1534-1`,
source component `23deae961c662919ce90db6302d38ec0102975a6`. The lock pins its
actual SRI; `npm-security/artifact.json` pins SHA256 and source/build identities.
Never replace existing asset bytes under the same version.

## Why the overrides are scoped

npm's bundled copies cannot be fixed by ordinary consumer overrides. Its artifact
is rebuilt first, with glob14/minimatch11 and patched ip-address/undici. Because
nested dependencies still declare their upstream ranges, the consumer's `npm`
override must preserve the **complete scoped security profile** from the recipe,
including the `.` reference to `$npm`. Without it, frozen `ci` correctly rejects
missing old glob13 requirements. An artifact-root override alone is insufficient.

OpenClaw's ordinary minimatch copy also moves to11.0.4. Its ordinary undici8.12.3
and zod4.6.7 overrides remain separate: do not force undici8 into npm's6.x subtree.
Glob/minimatch APIs changed across majors; qualification must cover actual
workspaces, install/ci/list/pack and the local plugin, not merely audit output.

## Installation and qualification

Use Node **24.22.2** for exact-source reproduction; the pinned OpenClaw needs
>=24.22.1 on the24.x line. npm12's remote policy is kept intact. The image's `ci`
opts in with **`--allow-remote=root`** for the single explicit, integrity-bound npm
artifact URL. It does not allow arbitrary transitive URLs or bypass script policy.

```sh
npm ci --allow-remote=root --no-fund --no-audit
npm list --prefix /opt/wrap-tools --depth=0 @openai/codex openclaw npm
```

`npm ci` is local, not global. The Docker build checks both actual npm inventories
against the separately audited complete build lock. Wrap/plugin/image behavior
still requires the real isolated fake-provider E2E; offline CLI fixtures alone
are not that evidence. No real provider/model call is needed.

## Lock maintenance

Keep the reviewed lock in source; do not delete or trim entries to conceal audit
findings. Use the verified Node24.22.2/npm11.19.1 bootstrap in an owned disposable
HOME/config/cache to maintain metadata, then qualify with the **actual patched
npm12 CLI**. The bootstrap and runtime need not have the same version; their
identities and roles are recorded rather than silently substituted.

`--package-lock-only` can retain stale bundled entries or omit them completely.
Materialize the artifact with lifecycle scripts disabled first, ensure every
bundled node is enumerated in the saved lock, and require the full inventory to
equal the independent build lock. Then run frozen `ci` with the patched CLI and
assert the lock does not change. A lock-only zero count is not full audit coverage.

Review the complete diff, preserve every existing floor/override and run all
**six** maintained all-severity npm audits. Do not use forced repair, omit-dev,
ignore lists, reduced severity or fallback installers. The image retains normal
reviewed install lifecycle behavior; controlled host qualification disables hooks.

The lock fixes resolution and checks content integrity; it cannot make GitHub or
the registry available during an outage. These remain existing test tools with
upstream licenses; jws owns the patched npm source artifact's maintenance until
an upstream bundle passes the same packed/installed inventory and compatibility
gates. Source publication is not merge, runtime activation or report recovery.
