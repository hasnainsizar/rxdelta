from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rxdelta.agent.client import ModelError, Usage
from rxdelta.agent.loop import REMINDER, AgentRunner, AgentSettings
from rxdelta.agent.toolset import SUBMIT, answer_schema, model_tools, submit_tool
from rxdelta.ask.answer import QuestionContext, parse_answer
from rxdelta.config import Config
from rxdelta.evals.runners import RUNNERS
from rxdelta.evals.score import score_item
from rxdelta.tools.context import ToolContext
from tests.agent_script import (
    agent,
    item,
    oracle_script,
    submit,
    text_turn,
    tool_use,
    turn,
)

KIND_ITEMS = {
    "coverage": "rxd-v1-0001",
    "change": "rxd-v1-0021",
    "drug_list": "rxd-v1-0051",
    "count": "rxd-v1-0071",
    "cost_range": "rxd-v1-0101",
    "no_data": "rxd-v1-0141",
    "clarify": "rxd-v1-0161",
    "refusal": "rxd-v1-0171",
}
COUNT = {"kind": "count", "count": 2, "unit": "drugs"}


@pytest.fixture
def ctx(eval_db: Path, config: Config) -> ToolContext:
    return ToolContext(db_path=eval_db, config=config)


def _ask(runner: AgentRunner, question: str = "How many drugs changed?") -> Any:
    return runner.answer(question, QuestionContext())


def _sent(request_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tool_result blocks of the last user message of one request."""
    last = request_messages[-1]
    assert last["role"] == "user"
    return [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]


# Happy paths


@pytest.mark.parametrize("kind", sorted(KIND_ITEMS))
def test_happy_path_for_each_answer_kind(kind: str, ctx: ToolContext, config: Config) -> None:
    it = item(KIND_ITEMS[kind])
    responses, oracle = oracle_script(it, ctx)
    runner, client = agent(ctx, list(responses))
    out = runner.answer(it.question, it.context)
    assert out.error is None, out.error
    assert out.answer.kind == kind
    assert out.answer == oracle.answer
    assert [c.name for c in out.tool_calls] == [c.name for c in oracle.tool_calls]
    score = score_item(it, out, config)
    assert score.correct and not score.ungrounded_numbers
    assert out.trace is not None and len(out.trace["drafts"]) == 1
    assert len(client.requests) == len(responses)
    assert out.input_tokens == 100 * len(responses)
    assert out.cost_usd > 0 and out.latency_ms >= 0


def test_parallel_tool_calls_come_back_in_one_message(ctx: ToolContext) -> None:
    runner, client = agent(
        ctx,
        [
            turn(
                tool_use("list_months", {}, 1),
                tool_use("find_drug", {"query": "brilinta"}, 2),
            ),
            turn(submit({"kind": "refusal", "reason": "out_of_scope"}, "Out of scope.")),
        ],
    )
    out = _ask(runner)
    assert out.error is None
    results = _sent(client.requests[1].messages)
    assert [r["tool_use_id"] for r in results] == ["toolu_001", "toolu_002"]
    assert [c.name for c in out.tool_calls] == ["list_months", "find_drug"]


def test_every_model_tool_is_flat_and_submit_answer_takes_every_kind() -> None:
    tools = model_tools()
    assert [t["name"] for t in tools][-1] == SUBMIT
    text = json.dumps(tools)
    assert "$ref" not in text and "$defs" not in text and "oneOf" not in text
    branches = answer_schema()["anyOf"]
    kinds = {b["properties"]["kind"]["enum"][0] for b in branches}
    assert kinds == set(KIND_ITEMS)
    assert all("kind" in b["required"] for b in branches)
    assert submit_tool()["input_schema"]["required"] == ["answer", "final_text"]


@pytest.mark.parametrize("kind", sorted(KIND_ITEMS))
def test_every_kind_example_validates(kind: str) -> None:
    assert parse_answer(item(KIND_ITEMS[kind]).expected.model_dump(mode="json")).kind == kind


# Guards


def test_schema_guard_retry_succeeds(ctx: ToolContext) -> None:
    runner, client = agent(
        ctx,
        [
            turn(submit({"kind": "count", "count": -1}, "None.")),
            turn(submit({"kind": "refusal"}, "Out of scope.")),
        ],
    )
    out = _ask(runner)
    assert out.error is None and out.answer.kind == "refusal"
    sent = _sent(client.requests[1].messages)
    assert sent[0]["is_error"] is True and "answer.count" in sent[0]["content"]
    assert out.trace is not None
    assert len(out.trace["drafts"]) == 2
    assert out.trace["drafts"][0]["schema_problems"]


def test_schema_guard_that_runs_out_ends_with_error(ctx: ToolContext) -> None:
    bad = turn(submit({"kind": "count"}, "None."))
    runner, _ = agent(ctx, [bad, bad])
    out = _ask(runner)
    assert out.error is not None and "did not validate" in out.error


def _count_turns(text_first: str, text_second: str) -> list[Any]:
    call = tool_use(
        "count_changes",
        {"from_month": "2026-01", "to_month": "2026-02", "change_types": ["tier_down"]},
        1,
    )
    return [turn(call), turn(submit(COUNT, text_first, 2)), turn(submit(COUNT, text_second, 3))]


def _tier_down_drugs(ctx: ToolContext) -> int:
    from rxdelta.tools.registry import call_tool

    result = call_tool(
        "count_changes",
        {"from_month": "2026-01", "to_month": "2026-02", "change_types": ["tier_down"]},
        ctx,
    )
    assert result.data is not None
    return int(result.data.totals.drugs)


def test_grounding_guard_retry_succeeds_and_keeps_the_first_draft(ctx: ToolContext) -> None:
    n = _tier_down_drugs(ctx)
    count = {**COUNT, "count": n}
    call = tool_use(
        "count_changes",
        {"from_month": "2026-01", "to_month": "2026-02", "change_types": ["tier_down"]},
        1,
    )
    runner, client = agent(
        ctx,
        [
            turn(call),
            turn(submit(count, f"{n} drugs moved down, about 4417 plans.", 2)),
            turn(submit(count, f"{n} drugs moved down a tier.", 3)),
        ],
    )
    out = _ask(runner)
    assert out.error is None
    assert out.trace is not None
    assert out.trace["drafts"][0]["ungrounded"] == ["4417"]
    assert out.trace["drafts"][1]["ungrounded"] == []
    assert out.trace["guards_failed"] == []
    sent = _sent(client.requests[2].messages)
    assert "4417" in sent[0]["content"] and "count_changes" in sent[0]["content"]


def test_grounding_guard_that_runs_out_accepts_and_records_the_failure(ctx: ToolContext) -> None:
    n = _tier_down_drugs(ctx)
    count = {**COUNT, "count": n}
    call = tool_use(
        "count_changes",
        {"from_month": "2026-01", "to_month": "2026-02", "change_types": ["tier_down"]},
        1,
    )
    runner, _ = agent(
        ctx,
        [
            turn(call),
            turn(submit(count, f"{n} drugs, 4417 plans.", 2)),
            turn(submit(count, f"{n} drugs, 5517 plans.", 3)),
        ],
    )
    out = _ask(runner)
    assert out.error is None and "5517" in out.final_text
    assert out.trace is not None
    assert out.trace["guards_failed"] == ["grounding"]
    assert out.trace["drafts"][0]["ungrounded"] == ["4417"]


def test_a_guard_turned_off_records_but_never_retries(ctx: ToolContext) -> None:
    runner, client = agent(
        ctx,
        [turn(submit(COUNT, "2 drugs and 4417 plans."))],
        guards=AgentSettings.from_config(ctx.config, guards_off=("grounding",)).guards,
    )
    out = _ask(runner)
    assert out.error is None and len(client.requests) == 1
    assert out.trace is not None
    assert out.trace["guards_failed"] == []
    assert "4417" in out.trace["drafts"][0]["ungrounded"]


def test_consistency_guard_catches_a_count_missing_from_the_text(ctx: ToolContext) -> None:
    runner, client = agent(
        ctx,
        [
            turn(submit({**COUNT, "count": 0}, "Several drugs changed.")),
            turn(submit({**COUNT, "count": 0}, "No drugs changed: 0.")),
        ],
        guards=AgentSettings.from_config(ctx.config, guards_off=("grounding",)).guards,
    )
    out = _ask(runner)
    assert out.error is None
    sent = _sent(client.requests[1].messages)
    assert "does not state 0" in sent[0]["content"]


def test_consistency_guard_wants_a_partial_list_said_to_be_partial(ctx: ToolContext) -> None:
    from rxdelta.agent.guards import consistency_problems

    answer = parse_answer({"kind": "drug_list", "rxcuis": ["1"], "truncated": True, "total": 40})
    assert len(consistency_problems(answer, "Drug A.")) == 2
    assert consistency_problems(answer, "40 drugs match; this is a partial list: Drug A.") == []
    no_total = parse_answer({"kind": "drug_list", "rxcuis": ["1"], "truncated": True})
    assert any("no total" in p for p in consistency_problems(no_total, "a partial list"))
    clarify = parse_answer(
        {"kind": "clarify", "candidates": [{"product": "Ozempic", "rxcuis": ["1"]}]}
    )
    assert consistency_problems(clarify, "Which one?") != []
    assert consistency_problems(clarify, "Which Ozempic?") == []
    refusal = parse_answer({"kind": "refusal"})
    assert consistency_problems(refusal, "As an AI I cannot say.") != []


# Stops and limits


def test_plain_text_stop_gets_one_reminder(ctx: ToolContext) -> None:
    runner, client = agent(
        ctx, [text_turn("Thinking."), turn(submit({"kind": "refusal"}, "Out of scope."))]
    )
    out = _ask(runner)
    assert out.error is None
    assert client.requests[1].messages[-1] == {"role": "user", "content": REMINDER}


def test_plain_text_twice_ends_with_error(ctx: ToolContext) -> None:
    runner, _ = agent(ctx, [text_turn("One."), text_turn("Two.")])
    out = _ask(runner)
    assert out.error is not None and "plain text" in out.error
    assert out.output_tokens == 40


def test_max_turns_ends_with_error(ctx: ToolContext) -> None:
    runner, _ = agent(ctx, [turn(tool_use("list_months", {}, n)) for n in range(5)], max_turns=3)
    out = _ask(runner)
    assert out.error is not None and "3 model turns" in out.error
    assert len(out.tool_calls) == 3


def test_max_tool_calls_ends_with_error(ctx: ToolContext) -> None:
    runner, _ = agent(
        ctx,
        [turn(*(tool_use("list_months", {}, n) for n in range(4)))],
        max_tool_calls=3,
    )
    out = _ask(runner)
    assert out.error is not None and "3 tool calls" in out.error
    assert len(out.tool_calls) == 3


def test_cost_cap_ends_with_error(ctx: ToolContext) -> None:
    expensive = Usage(input_tokens=10_000_000, output_tokens=0)
    runner, _ = agent(ctx, [turn(tool_use("list_months", {}, 1), usage=expensive)])
    out = _ask(runner)
    assert out.error is not None and "per question cap" in out.error
    assert out.cost_usd == pytest.approx(1.0)


def test_an_api_error_ends_the_item_with_its_type(ctx: ToolContext) -> None:
    runner, _ = agent(ctx, [ModelError("RateLimitError", "HTTP 429: slow down")])
    out = _ask(runner)
    assert out.error is not None and out.error.startswith("api error RateLimitError")
    assert out.latency_ms >= 0


def test_an_api_error_does_not_stop_the_eval(eval_db: Path, config: Config, tmp_path: Path) -> None:
    from rxdelta.evals import harness

    it = item("rxd-v1-0171")
    responses: list[Any] = [
        ModelError("InternalServerError", "HTTP 500"),
        turn(submit({"kind": "refusal", "reason": "medical_advice"}, "Ask a pharmacist.")),
    ]
    ctx = ToolContext(db_path=eval_db, config=config)
    runner, _ = agent(ctx, responses)
    result = harness.run(
        set_path=Path("evals/rxdelta_fixture_v1.jsonl"),
        runner_name="agent",
        split="all",
        db_path=eval_db,
        config=config,
        out_root=tmp_path,
        category="out_of_scope",
        limit=2,
        workers=1,
        runner_factory=lambda items, c: runner,
    )
    assert result.summary["overall"]["items"] == 2
    assert result.summary["overall"]["errors"] == 1
    from typer.testing import CliRunner

    from rxdelta.cli import app

    ids = [json.loads(line)["id"] for line in (result.directory / "results.jsonl").open()]
    shown = CliRunner().invoke(app, ["eval", "show", str(result.directory), ids[0]])
    assert shown.exit_code == 0, shown.output
    assert "stopped: api error InternalServerError" in shown.output
    assert "Expected:" in shown.output and "Scored wrong" in shown.output
    shown = CliRunner().invoke(app, ["eval", "show", str(result.directory), ids[1]])
    assert "answer accepted (refusal)" in shown.output and "Scored right" in shown.output
    missing = CliRunner().invoke(app, ["eval", "show", str(result.directory), "nope"])
    assert missing.exit_code != 0
    del it


# What the model reads


def test_provenance_never_reaches_the_model(ctx: ToolContext) -> None:
    it = item(KIND_ITEMS["count"])
    responses, _ = oracle_script(it, ctx)
    runner, client = agent(ctx, list(responses))
    out = runner.answer(it.question, it.context)
    assert any(c.result.get("provenance") for c in out.tool_calls)
    for request in client.requests:
        sent = json.dumps(request.model_dump(mode="json"))
        assert "provenance" not in sent and "row_keys" not in sent


def test_oversized_results_are_cut_with_a_note(ctx: ToolContext) -> None:
    call = tool_use("get_changes", {"from_month": "2026-01", "to_month": "2026-03"}, 1)
    runner, client = agent(
        ctx,
        [turn(call), turn(submit({"kind": "refusal"}, "Out of scope."))],
        max_tool_result_chars=1500,
    )
    out = _ask(runner)
    full = out.tool_calls[0].result
    sent = _sent(client.requests[1].messages)[0]["content"]
    assert len(sent) <= 1500
    view = json.loads(sent)
    assert view["truncated"] is True and view["total"] == full["total"]
    assert "shortened" in view["note"]
    assert len(view["data"]["rows"]) < len(full["data"]["rows"])
    assert out.trace is not None
    assert out.trace["events"][0]["shortened"] is True


def test_failed_tool_calls_are_recorded_and_flagged(ctx: ToolContext) -> None:
    runner, client = agent(
        ctx,
        [
            turn(tool_use("get_coverage", {"rxcui": "x"}, 1)),
            turn(submit({"kind": "refusal"}, "Out of scope.")),
        ],
    )
    out = _ask(runner)
    assert out.tool_calls[0].result["ok"] is False
    assert _sent(client.requests[1].messages)[0]["is_error"] is True


# Gold isolation


class _NoTouch(list[Any]):
    def _refuse(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the agent runner read the eval items")

    __iter__ = __getitem__ = __len__ = __contains__ = __bool__ = _refuse


def test_the_agent_runner_never_reads_gold(ctx: ToolContext) -> None:
    from rxdelta.agent.client import ScriptedClient

    client = ScriptedClient([turn(submit({"kind": "refusal"}, "Out of scope."))])
    factory = RUNNERS["agent"]
    assert factory is AgentRunner
    runner = AgentRunner(_NoTouch(), ctx, client=client)
    assert runner.answer("Should I switch?", QuestionContext()).error is None


def test_context_months_reach_the_model(ctx: ToolContext) -> None:
    runner, client = agent(ctx, [turn(submit({"kind": "refusal"}, "Out of scope."))])
    runner.answer("Did it change?", QuestionContext(default_from="2026-01", default_to="2026-02"))
    first = client.requests[0].messages[0]["content"]
    assert "2026-01" in first and "2026-02" in first
