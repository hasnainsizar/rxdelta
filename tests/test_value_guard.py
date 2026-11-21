from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rxdelta.agent.guards import value_problems
from rxdelta.ask.answer import QuestionContext, parse_answer
from rxdelta.config import Config
from rxdelta.tools.context import ToolContext
from tests.agent_script import agent, submit, tool_use, turn

ROW = {
    "rxcui": "1739776",
    "formulary_id": "00026109",
    "change_type": "drug_added",
    "before_value": None,
    "after_value": "tier 4",
    "after": {"tier": 4, "prior_auth": True, "quantity_limit": True},
}
RESULTS = [{"ok": True, "data": {"rows": [ROW]}}]
RECORD = json.dumps(ROW["after"])


def _change(before: str | None, after: str | None, change_type: str = "drug_added") -> Any:
    return parse_answer(
        {
            "kind": "change",
            "rxcuis": ["1739776"],
            "items": [
                {
                    "rxcui": "1739776",
                    "formulary_id": "00026109",
                    "change_type": change_type,
                    "before": before,
                    "after": after,
                }
            ],
        }
    )


def test_values_copied_from_get_changes_pass() -> None:
    assert value_problems(_change(None, "tier 4"), RESULTS) == []


def test_a_rewritten_value_is_named_with_the_tool_value() -> None:
    (problem,) = value_problems(_change(None, "tier 5"), RESULTS)
    assert "after 'tier 4'" in problem and "not before None, after 'tier 5'" in problem


def test_a_whole_coverage_record_in_place_of_a_value_fails() -> None:
    (problem,) = value_problems(_change(None, RECORD), RESULTS)
    assert "after_value exactly" in problem and "tier 4" in problem


def test_a_change_no_row_returned_fails() -> None:
    (problem,) = value_problems(_change("no", "yes", "prior_auth_added"), RESULTS)
    assert "no get_changes row has change type prior_auth_added" in problem


def test_answers_other_than_change_are_not_checked() -> None:
    assert value_problems(parse_answer({"kind": "count", "count": 1}), []) == []


@pytest.fixture
def ctx(eval_db: Path, config: Config) -> ToolContext:
    return ToolContext(db_path=eval_db, config=config)


def test_the_loop_sends_a_rewritten_value_back_once(ctx: ToolContext) -> None:
    from rxdelta.tools.registry import call_tool

    args = {"from_month": "2026-02", "to_month": "2026-03", "change_types": ["tier_down"]}
    rows = call_tool("get_changes", args, ctx).data.rows  # type: ignore[union-attr]
    row = rows[0]

    def answer(after: str | None) -> dict[str, Any]:
        return {
            "kind": "change",
            "rxcuis": [row.rxcui],
            "items": [
                {
                    "rxcui": row.rxcui,
                    "formulary_id": row.formulary_id,
                    "change_type": "tier_down",
                    "before": row.before_value,
                    "after": after,
                }
            ],
        }

    text = f"{row.display_name} moved down a tier."
    runner, client = agent(
        ctx,
        [
            turn(tool_use("get_changes", args, 1)),
            turn(submit(answer(f"tier {row.after_value}, no prior authorization"), text, 2)),
            turn(submit(answer(row.after_value), text, 3)),
        ],
    )
    out = runner.answer("Did anything move down a tier?", QuestionContext())
    assert out.error is None and out.trace is not None
    assert out.trace["drafts"][0]["value_problems"]
    assert out.trace["drafts"][1]["value_problems"] == []
    sent = client.requests[2].messages[-1]["content"][0]
    assert sent["is_error"] is True and "after_value exactly" in sent["content"]
    guard_events = [e for e in out.trace["events"] if e["type"] == "guard"]
    assert [e["guard"] for e in guard_events] == ["values"]
