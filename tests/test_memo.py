from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from rxdelta.config import Config
from rxdelta.memo import LruMemo
from rxdelta.tools import changes_core
from rxdelta.tools.context import ToolContext


def test_lru_memo_keeps_the_most_recent_and_drops_the_oldest() -> None:
    memo: LruMemo[int] = LruMemo()
    calls: list[str] = []

    def make(key: str) -> int:
        calls.append(key)
        return len(calls)

    assert memo.get("a", lambda: make("a"), size=2) == 1
    assert memo.get("b", lambda: make("b"), size=2) == 2
    assert memo.get("a", lambda: make("a"), size=2) == 1  # hit; a is now newest
    assert memo.get("c", lambda: make("c"), size=2) == 3  # drops b
    assert len(memo) == 2
    assert memo.get("a", lambda: make("a"), size=2) == 1
    assert memo.get("b", lambda: make("b"), size=2) == 4  # b was dropped
    assert calls == ["a", "b", "c", "b"]


def test_the_change_memo_stays_within_its_configured_size(
    ask_db: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = replace(config, tools=config.tools.model_copy(update={"diff_memo_pairs": 1}))
    ctx = ToolContext(db_path=ask_db, config=small)
    fresh: LruMemo[list[changes_core.ChangeRow]] = LruMemo()
    monkeypatch.setattr(changes_core, "_memo", fresh)
    conn = ctx.connect()
    for pair in (("2026-01", "2026-02"), ("2026-02", "2026-03"), ("2026-01", "2026-03")):
        assert changes_core.all_rows(conn, ask_db, small, *pair)
        assert len(fresh) == 1
    conn.close()
