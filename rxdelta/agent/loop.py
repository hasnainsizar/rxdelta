"""The agent: a model answers a question by calling the tools, then submits a
structured answer that has to pass the guards."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rxdelta.agent import guards, prompts
from rxdelta.agent.client import (
    AnthropicClient,
    ModelClient,
    ModelError,
    ModelRequest,
    Usage,
    cost_usd,
)
from rxdelta.agent.keys import api_key
from rxdelta.agent.toolset import SUBMIT, model_tools, model_view
from rxdelta.ask.answer import (
    NoDataAnswer,
    NoDataReason,
    QuestionContext,
    RunnerOutput,
    StructuredAnswer,
    ToolCallRecord,
)
from rxdelta.config import Config, GuardsConfig, ModelPrices
from rxdelta.tools.context import ToolContext
from rxdelta.tools.registry import call_tool

GUARDS = ("schema", "grounding", "consistency")
REMINDER = (
    "You stopped without an answer. Finish by calling submit_answer with the structured "
    "answer and final_text."
)
Event = Callable[[str, dict[str, Any]], None]
CUT_OFF = (
    "Your response reached the output token limit, so this call was cut off and not run. "
    "Send it again in full; leave out text that is not needed."
)
API_ERROR = "api error"
ACCOUNT_ERROR = "account error"


@dataclass(frozen=True)
class AgentSettings:
    model: str
    prompt_version: str
    max_tokens: int
    effort: str
    max_turns: int
    max_tool_calls: int
    max_cost_per_question_usd: float
    max_tool_result_chars: int
    plain_text_reminders: int
    guards: GuardsConfig
    prices: ModelPrices

    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        model: str | None = None,
        prompt_version: str | None = None,
        guards_off: tuple[str, ...] = (),
    ) -> AgentSettings:
        agent = config.agent
        chosen = model or agent.default_model
        off = {("schema_" if g == "schema" else g): False for g in guards_off}
        return cls(
            model=chosen,
            prompt_version=prompt_version or agent.prompt_version,
            max_tokens=agent.max_tokens,
            effort=agent.effort,
            max_turns=agent.max_turns,
            max_tool_calls=agent.max_tool_calls,
            max_cost_per_question_usd=agent.max_cost_per_question_usd,
            max_tool_result_chars=agent.max_tool_result_chars,
            plain_text_reminders=agent.plain_text_reminders,
            guards=agent.guards.model_copy(update=off),
            prices=agent.prices(chosen),
        )

    def guard_on(self, name: str) -> bool:
        return bool(getattr(self.guards, "schema_" if name == "schema" else name))

    def retries(self, name: str) -> int:
        return int(getattr(self.guards, f"{name}_retries"))

    def describe(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "prompt_version": self.prompt_version,
            "effort": self.effort,
            "max_tokens": self.max_tokens,
            "guards": {g: self.guard_on(g) for g in GUARDS},
            "guard_retries": {g: self.retries(g) for g in GUARDS},
        }


class _StopError(Exception):
    """Ends one question's run with an error."""


@dataclass
class _Run:
    question: str
    started: float
    messages: list[dict[str, Any]] = field(default_factory=list)
    calls: list[ToolCallRecord] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    turns: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    drafts: list[dict[str, Any]] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=lambda: dict.fromkeys(GUARDS, 0))
    guards_failed: list[str] = field(default_factory=list)
    reminders: int = 0


def default_client(config: Config) -> ModelClient:
    return AnthropicClient(
        api_key=api_key(),
        timeout_seconds=config.agent.request_timeout_seconds,
        max_retries=config.agent.max_retries,
    )


def _first_message(question: str, context: QuestionContext) -> str:
    if context.default_from and context.default_to:
        return (
            f"{question}\n\nUnless the question says otherwise, compare {context.default_from} "
            f"with {context.default_to}."
        )
    return question


def summarize_result(result: dict[str, Any]) -> str:
    """One line about a tool result, for traces and --show-tools."""
    if not result.get("ok"):
        error = result.get("error") or {}
        return f"error {error.get('code')}: {error.get('message')}"
    parts = ["ok"]
    if result.get("total") is not None:
        parts.append(f"total {result['total']}")
    if result.get("truncated"):
        parts.append("truncated")
    note = result.get("note")
    if note:
        parts.append(str(note)[:120])
    return ", ".join(parts)


class AgentRunner:
    name = "agent"

    def __init__(
        self,
        items: Any,
        ctx: ToolContext,
        *,
        client: ModelClient | None = None,
        settings: AgentSettings | None = None,
        on_event: Event | None = None,
    ) -> None:
        del items  # they carry gold answers; an agent must never read them
        self._ctx = ctx
        self.settings = settings or AgentSettings.from_config(ctx.config)
        self._system = prompts.load(self.settings.prompt_version)
        self._tools = model_tools()
        self._client = client or default_client(ctx.config)
        self._on_event = on_event

    def _emit(self, run: _Run, kind: str, payload: dict[str, Any]) -> None:
        run.events.append({"type": kind, **payload})
        if self._on_event is not None:
            self._on_event(kind, payload)

    def answer(self, question: str, context: QuestionContext) -> RunnerOutput:
        run = _Run(question=question, started=time.perf_counter())
        run.messages.append({"role": "user", "content": _first_message(question, context)})
        try:
            answer, text = self._loop(run)
            error = None
        except _StopError as stop:
            answer, text, error = (
                NoDataAnswer(reason=NoDataReason.no_matching_changes),
                "",
                str(stop),
            )
            self._emit(run, "stopped", {"error": error})
        return RunnerOutput(
            answer=answer,
            final_text=text,
            tool_calls=run.calls,
            input_tokens=run.usage.prompt_tokens,
            output_tokens=run.usage.output_tokens,
            cost_usd=round(run.cost, 8),
            latency_ms=round(1000 * (time.perf_counter() - run.started)),
            error=error,
            trace=self._trace(run),
        )

    def _trace(self, run: _Run) -> dict[str, Any]:
        return {
            "settings": self.settings.describe(),
            "messages": run.messages,
            "turns": run.turns,
            "events": run.events,
            "drafts": run.drafts,
            "guards_failed": run.guards_failed,
            "usage": run.usage.model_dump(),
        }

    def _loop(self, run: _Run) -> tuple[StructuredAnswer, str]:
        s = self.settings
        while True:
            if len(run.turns) >= s.max_turns:
                raise _StopError(
                    f"limit: reached {s.max_turns} model turns without an accepted answer"
                )
            request = ModelRequest(
                model=s.model,
                system=self._system,
                messages=run.messages,
                tools=self._tools,
                max_tokens=s.max_tokens,
                effort=s.effort,
            )
            try:
                response = self._client.create(request)
            except ModelError as exc:
                prefix = ACCOUNT_ERROR if exc.account else API_ERROR
                raise _StopError(f"{prefix} {exc}") from exc
            run.usage = run.usage + response.usage
            run.cost += cost_usd(response.usage, s.prices)
            run.turns.append(
                {"stop_reason": response.stop_reason, "usage": response.usage.model_dump()}
            )
            run.messages.append({"role": "assistant", "content": response.content})
            if run.cost > s.max_cost_per_question_usd:
                raise _StopError(
                    f"limit: spent ${run.cost:.4f}, over the ${s.max_cost_per_question_usd} "
                    "per question cap"
                )
            uses = response.tool_uses()
            if uses and response.stop_reason == "max_tokens":
                self._emit(run, "cut_off", {"max_tokens": s.max_tokens})
                run.messages.append(
                    {"role": "user", "content": [_error_result(u["id"], CUT_OFF) for u in uses]}
                )
                continue
            if not uses:
                self._plain_stop(run, response.stop_reason)
                continue
            accepted, results = self._handle_uses(run, uses)
            if accepted is not None:
                return accepted
            run.messages.append({"role": "user", "content": results})

    def _plain_stop(self, run: _Run, stop_reason: str | None) -> None:
        if stop_reason == "refusal":
            raise _StopError("the model declined to answer (stop reason refusal)")
        if run.reminders >= self.settings.plain_text_reminders:
            raise _StopError("stopped with plain text instead of submit_answer, after a reminder")
        run.reminders += 1
        self._emit(run, "reminder", {"stop_reason": stop_reason})
        run.messages.append({"role": "user", "content": REMINDER})

    def _handle_uses(
        self, run: _Run, uses: list[dict[str, Any]]
    ) -> tuple[tuple[StructuredAnswer, str] | None, list[dict[str, Any]]]:
        results: list[dict[str, Any]] = []
        submits = [u for u in uses if u.get("name") == SUBMIT]
        for use in uses:
            if use.get("name") == SUBMIT:
                continue
            if len(run.calls) >= self.settings.max_tool_calls:
                raise _StopError(f"limit: reached {self.settings.max_tool_calls} tool calls")
            results.append(self._call(run, use))
        for extra in submits[1:]:
            results.append(_error_result(extra["id"], "Call submit_answer once per turn."))
        if not submits:
            return None, results
        verdict, message = self._check(run, submits[0].get("input") or {})
        if verdict is not None:
            return verdict, results
        results.append(_error_result(submits[0]["id"], message))
        return None, results

    def _call(self, run: _Run, use: dict[str, Any]) -> dict[str, Any]:
        name = str(use.get("name"))
        raw = use.get("input")
        args: dict[str, Any] = raw if isinstance(raw, dict) else {}
        started = time.perf_counter()
        result = call_tool(name, args, self._ctx).model_dump(mode="json")
        run.calls.append(
            ToolCallRecord(
                name=name,
                args=args,
                result=result,
                duration_ms=round(1000 * (time.perf_counter() - started)),
            )
        )
        view, cut = model_view(result, self.settings.max_tool_result_chars)
        self._emit(
            run,
            "tool_call",
            {"name": name, "args": args, "summary": summarize_result(result), "shortened": cut},
        )
        return {
            "type": "tool_result",
            "tool_use_id": use["id"],
            "content": view,
            **({"is_error": True} if not result.get("ok") else {}),
        }

    def _reject(self, run: _Run, guard: str, problems: list[str]) -> str | None:
        """The message to send back, or None when this guard has no retries left."""
        run.failures[guard] += 1
        on = self.settings.guard_on(guard)
        retry = on and run.failures[guard] <= self.settings.retries(guard)
        self._emit(
            run, "guard", {"guard": guard, "passed": False, "retry": retry, "problems": problems}
        )
        if retry:
            return "The answer was not accepted. " + " ".join(problems)
        if on:
            run.guards_failed.append(guard)
        return None

    def _check(
        self, run: _Run, raw: dict[str, Any]
    ) -> tuple[tuple[StructuredAnswer, str] | None, str]:
        text = raw.get("final_text") if isinstance(raw.get("final_text"), str) else ""
        answer, schema = guards.schema_problems(raw.get("answer"))
        ungrounded = guards.ungrounded_numbers(
            str(text), run.question, [c.result for c in run.calls]
        )
        consistency = guards.consistency_problems(answer, str(text)) if answer is not None else []
        run.drafts.append(
            {
                "answer": raw.get("answer"),
                "final_text": text,
                "tool_calls_before": len(run.calls),
                "schema_problems": schema,
                "ungrounded": ungrounded,
                "consistency_problems": consistency,
            }
        )
        if schema:
            message = self._reject(run, "schema", schema)
            if message is not None:
                return None, message
            raise _StopError("submit_answer did not validate: " + "; ".join(schema))
        assert answer is not None
        checks = (
            ("grounding", [guards.grounding_message(ungrounded)] if ungrounded else []),
            ("consistency", consistency),
        )
        for guard, problems in checks:
            if not problems:
                continue
            message = self._reject(run, guard, problems)
            if message is not None:
                return None, message
        self._emit(run, "accepted", {"kind": answer.kind})
        return (answer, str(text)), ""


def _error_result(tool_use_id: str, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": message, "is_error": True}
