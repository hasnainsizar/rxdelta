"""The updated banned words check. Words that describe what the code does
(agent, model, anthropic, system_prompt, tool_use) are allowed; dashes and text
where the code or the agent talks about being an AI, or about who wrote it, are
not."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DASHES = re.compile("[\u2013\u2014]")
SELF_TALK = re.compile(
    r"as an ai\b|as a language model|i am an ai\b|i'm an ai\b|chatgpt|"
    r"co-authored-by|generated (?:with|by) (?:claude|an ai|ai)|written by (?:claude|an ai|ai)",
    re.IGNORECASE,
)
# Code that detects these characters or phrases has to name them.
DASH_CODE = {"rxdelta/evals/grounding.py", "tests/test_grounding.py", "tests/test_report.py"}
SELF_TALK_CODE = {"rxdelta/agent/guards.py", "tests/test_prose.py", "tests/test_agent_loop.py"}
BINARY = (".pdf", ".png", ".db", ".woff2", ".zip", ".ico")


def _tracked() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [p for p in out.splitlines() if not p.endswith(BINARY) and (REPO / p).is_file()]


def _lines(pattern: re.Pattern[str], allowed: set[str]) -> list[str]:
    hits = []
    for path in _tracked():
        if path in allowed:
            continue
        try:
            text = (REPO / path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path}:{n}: {line.strip()[:120]}")
    return hits


def test_no_em_or_en_dashes() -> None:
    assert _lines(DASHES, DASH_CODE) == []


def test_no_ai_self_talk_or_authorship_notes() -> None:
    assert _lines(SELF_TALK, SELF_TALK_CODE) == []
