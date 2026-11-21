"""Builders for scripted model responses, shared by the agent tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rxdelta.agent.client import ModelResponse, ScriptedClient, Usage
from rxdelta.agent.loop import AgentRunner, AgentSettings
from rxdelta.ask.answer import RunnerOutput
from rxdelta.config import Config
from rxdelta.evals.items import EvalItem, read_items
from rxdelta.evals.runners import OracleRunner
from rxdelta.tools.context import ToolContext

REPO = Path(__file__).resolve().parents[1]
FIXTURE_SET = REPO / "evals" / "rxdelta_fixture_v1.jsonl"
SMALL = Usage(input_tokens=100, output_tokens=20, cache_write_tokens=0, cache_read_tokens=0)


def tool_use(name: str, args: dict[str, Any], n: int) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{n:03d}", "name": name, "input": args}


def submit(answer: dict[str, Any], text: str, n: int = 900) -> dict[str, Any]:
    return tool_use("submit_answer", {"answer": answer, "final_text": text}, n)


def turn(*blocks: dict[str, Any], usage: Usage = SMALL) -> ModelResponse:
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return ModelResponse(content=list(blocks), stop_reason=stop, usage=usage)


def text_turn(text: str) -> ModelResponse:
    return turn({"type": "text", "text": text})


def fixture_items() -> list[EvalItem]:
    return read_items(FIXTURE_SET)


def item(item_id: str) -> EvalItem:
    return next(i for i in fixture_items() if i.id == item_id)


def oracle_script(it: EvalItem, ctx: ToolContext) -> tuple[list[ModelResponse], RunnerOutput]:
    """The oracle's tool calls, one per turn, then its answer through submit_answer."""
    oracle = OracleRunner(fixture_items(), ctx).answer(it.question, it.context)
    responses = [turn(tool_use(c.name, c.args, n)) for n, c in enumerate(oracle.tool_calls)]
    responses.append(turn(submit(oracle.answer.model_dump(mode="json"), oracle.final_text)))
    return responses, oracle


def agent(
    ctx: ToolContext, responses: list[ModelResponse | Exception], **settings: Any
) -> tuple[AgentRunner, ScriptedClient]:
    client = ScriptedClient(responses)
    base = AgentSettings.from_config(ctx.config)
    chosen = base if not settings else _replace(base, settings)
    return AgentRunner([], ctx, client=client, settings=chosen), client


def _replace(base: AgentSettings, changes: dict[str, Any]) -> AgentSettings:
    from dataclasses import replace

    return replace(base, **changes)


def context(db: Path, config: Config) -> ToolContext:
    return ToolContext(db_path=db, config=config)
