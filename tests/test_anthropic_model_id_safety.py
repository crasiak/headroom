"""Model display suffixes must not turn untrusted model IDs into CPU work."""

from __future__ import annotations

import random
import re
import subprocess
import sys

from headroom.providers.anthropic import has_context_1m_suffix, sanitize_anthropic_model_id


def test_model_suffix_grammar_matches_existing_display_semantics():
    ansi = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    styles = re.compile(r"(?:\[[0-9;]*m\])+$")
    context = re.compile(r"(?:\[1m\])+$")
    rng = random.Random(1869)
    parts = ("claude-test", "[m]", "[1m]", "[1;32m]", "[0m]", "[xm]", "!", "\n", " ", "\x1b[0m")
    cases = [
        "claude-test[m][1;32m][1m]",
        "claude-test[1m]!",
        "claude-test[1m]\n",
        "claude-test[1m]\n[0m]",
        "[1m]claude-test",
        *["".join(rng.choices(parts, k=8)) for _ in range(300)],
    ]
    for value in cases:
        cleaned = ansi.sub("", value).strip()
        assert sanitize_anthropic_model_id(value) == styles.sub("", cleaned), repr(value)
        assert has_context_1m_suffix(value) == bool(context.search(cleaned)), repr(value)


def test_model_suffix_near_misses_complete_in_a_bounded_child():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from headroom.providers.anthropic import "
            "sanitize_anthropic_model_id as clean, has_context_1m_suffix as context; "
            "values = ['[m]' * 100000 + '!', '[1m]' * 100000 + '!']; "
            "assert all(clean(value) == value and not context(value) for value in values)",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
