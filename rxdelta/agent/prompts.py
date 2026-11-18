"""Versioned system prompts. A prompt file is never edited once a scored run has
used it; a change is a new version."""

from __future__ import annotations

import re
from pathlib import Path

from rxdelta.types import RxdeltaError

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def available() -> list[str]:
    found = (re.fullmatch(r"system_(v\d+)\.md", p.name) for p in PROMPT_DIR.iterdir())
    return sorted((m.group(1) for m in found if m), key=lambda v: int(v[1:]))


def load(version: str) -> str:
    path = PROMPT_DIR / f"system_{version}.md"
    if not path.is_file():
        raise RxdeltaError(f"No system prompt {version!r}. Versions: {', '.join(available())}.")
    return path.read_text(encoding="utf-8").strip()
