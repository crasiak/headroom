"""An upstream sync may import generated release history, not local edits."""

from __future__ import annotations

import subprocess

import pytest

from scripts.check_changelog import check_changelog


def test_changelog_guard_requires_new_upstream_ancestry_and_exact_bytes(tmp_path, monkeypatch):
    for key, value in {
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    def commit(text):
        (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8", newline="\n")
        git("add", "CHANGELOG.md")
        git("commit", "-qm", "fixture")
        return git("rev-parse", "HEAD")

    git("init", "-q", "-b", "fork")
    base = commit("original release\n")
    git("checkout", "-qb", "upstream")
    upstream = commit("generated upstream release\n")
    git("checkout", "-q", "fork")
    (tmp_path / "fork.txt").write_text("fork change\n", encoding="utf-8", newline="\n")
    git("add", "fork.txt")
    git("commit", "-qm", "fork")
    fork = git("rev-parse", "HEAD")
    assert "unchanged" in check_changelog(tmp_path, base, fork, upstream)

    git("merge", "--no-ff", "-qm", "upstream sync", upstream)
    merged = git("rev-parse", "HEAD")
    assert upstream in check_changelog(tmp_path, fork, merged, upstream)

    manual = commit("generated upstream release\nmanual entry\n")
    with pytest.raises(ValueError, match="copied exactly"):
        check_changelog(tmp_path, fork, manual, upstream)
    with pytest.raises(ValueError, match="copied exactly"):
        check_changelog(tmp_path, fork, merged, base)
    with pytest.raises(ValueError, match="copied exactly"):
        check_changelog(tmp_path, merged, manual, upstream)
    with pytest.raises(ValueError, match="immutable"):
        check_changelog(tmp_path, "HEAD", merged, upstream)

    git("checkout", "-q", "upstream")
    future = commit("future upstream release\n")
    git("checkout", "-q", "fork")
    copied = commit("future upstream release\n")
    # Matching bytes without upstream ancestry is insufficient.
    with pytest.raises(ValueError, match="copied exactly"):
        check_changelog(tmp_path, fork, copied, future)
