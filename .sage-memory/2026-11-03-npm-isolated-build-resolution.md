# [LRN:error-fix] Isolated npm configuration and cross-major hoisting

## What happened

CRA-1534's clean npm security-bundle build failed with both config paths at
`/dev/null`. After separate config files fixed that, npm 11.19.1's default
hoisted resolver repeatedly replaced the overridden root glob/minimatch nodes
until its owned subprocess budget expired. `$glob`/`$minimatch` references did
not help. The same pinned manifest resolved in 19 seconds with the nested strategy.

## Why wrong

npm rejects loading one physical file as two configuration layers. Hoisted
resolution of this particular cross-major override closure does not converge;
its log proves repeated replacement, not network slowness or a model/API issue.
Increasing a timeout or editing an installed tree would not establish a repair.

## What's correct

Use separate empty workdir-owned user/global npmrc files, a private HOME/cache,
and a recorded nested install strategy. Regenerate a complete reviewed lock and
reinstall cleanly with lifecycle scripts disabled. Qualify actual packed copies,
CLI consumers, reproducibility and all audits; resolver success alone is not
compatibility evidence. Keep failed logs and positively join owned timed-out tools.

## Prevention rule

Run `tests/test_build_security_npm.py` and follow the pinned bundle recipe; never
inherit native npmrc or silently change install strategy after lock generation.

Tags: self-learning, error-fix, npm, security, reproducibility
