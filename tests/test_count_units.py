from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rxdelta.agent.guards import consistency_problems
from rxdelta.ask.answer import CountAnswer, QuestionContext
from rxdelta.config import Config
from rxdelta.evals import gold
from rxdelta.evals.items import EvalItem, GoldSpec
from rxdelta.evals.runners import OracleRunner
from rxdelta.tools.context import ToolContext
from rxdelta.tools.registry import call_tool

ARGS = {"from": "2026-01", "to": "2026-03", "change_types": ["tier_down", "prior_auth_added"]}
UNITS = ("drugs", "rows", "formularies", "plans")


@pytest.mark.parametrize("unit", UNITS)
def test_gold_counts_each_unit_like_count_changes(
    unit: str, ask_conn: sqlite3.Connection, ask_db: Path, config: Config
) -> None:
    answer = gold.count_drugs(ask_conn, {**ARGS, "unit": unit})
    assert isinstance(answer, CountAnswer) and answer.unit == unit
    totals = call_tool(
        "count_changes",
        {"from_month": "2026-01", "to_month": "2026-03", "change_types": ARGS["change_types"]},
        ToolContext(db_path=ask_db, config=config),
    ).data.totals  # type: ignore[union-attr]
    assert answer.count == getattr(totals, unit)


def test_gold_defaults_to_drugs_and_refuses_an_unknown_unit(ask_conn: sqlite3.Connection) -> None:
    assert gold.count_drugs(ask_conn, ARGS) == gold.count_drugs(ask_conn, {**ARGS, "unit": "drugs"})
    with pytest.raises(gold.GoldError, match="unknown count unit 'pills'"):
        gold.count_drugs(ask_conn, {**ARGS, "unit": "pills"})


@pytest.mark.parametrize("unit", UNITS)
def test_the_oracle_answers_in_the_asked_unit(
    unit: str, ask_conn: sqlite3.Connection, ask_db: Path, config: Config
) -> None:
    expected = gold.count_drugs(ask_conn, {**ARGS, "unit": unit})
    item = EvalItem(
        id="u",
        category="count",
        template_id="t",
        question=f"How many {unit}?",
        expected=expected,
        required_tools=["count_changes"],
        gold_query="count",
        gold=GoldSpec(query="count_drugs", args={**ARGS, "unit": unit}),
        difficulty="easy",
        split="dev",
        source="handwritten",
        seed=7,
    )
    out = OracleRunner([item], ToolContext(db_path=ask_db, config=config)).answer(
        item.question, QuestionContext()
    )
    assert out.answer == expected
    assert consistency_problems(out.answer, out.final_text) == []


@pytest.mark.parametrize(
    ("unit", "text", "ok"),
    [
        ("drugs", "12 drugs moved.", True),
        ("formularies", "12 formularies added it.", True),
        ("plans", "It reached 12 plans.", True),
        ("rows", "There were 12 changes.", True),
        ("plans", "12 moved.", False),
        ("formularies", "12 drugs moved.", False),
    ],
)
def test_a_count_must_name_its_unit(unit: str, text: str, ok: bool) -> None:
    answer = CountAnswer(count=12, unit=unit)  # type: ignore[arg-type]
    assert (consistency_problems(answer, text) == []) is ok
