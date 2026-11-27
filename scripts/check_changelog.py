#!/usr/bin/env python3
"""Reject manual changelog edits; permit an exact merged upstream copy."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def check_changelog(root: Path, base: str, head: str, upstream: str) -> str:
    for revision in (base, head, upstream):
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("expected an immutable Git commit SHA")
        _git(root, "cat-file", "-e", f"{revision}^{{commit}}")
    before = _git(root, "rev-parse", f"{base}:CHANGELOG.md")
    after = _git(root, "rev-parse", f"{head}:CHANGELOG.md")
    if before == after:
        return "OK: CHANGELOG.md is unchanged."
    merged = _git(root, "merge-base", head, upstream)
    imported = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", merged, base],
        check=False,
    ).returncode
    if imported not in (0, 1):
        raise RuntimeError("could not verify upstream ancestry")
    if imported == 0 or after != _git(root, "rev-parse", f"{merged}:CHANGELOG.md"):
        raise ValueError(
            "CHANGELOG.md must be unchanged or copied exactly from newly merged upstream. "
            "release-please generates local release entries."
        )
    return f"OK: CHANGELOG.md exactly matches merged upstream {merged} (tip {upstream})."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("upstream", help="fetched headroomlabs-ai/headroom main commit")
    args = parser.parse_args()
    try:
        print(check_changelog(Path.cwd(), args.base, args.head, args.upstream))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
