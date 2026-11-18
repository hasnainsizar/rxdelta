"""Runners: things that answer eval questions. Add one class and one RUNNERS line.

`oracle` knows each item's gold query (through its constructor, never through
the question context) and answers by calling the tools the way a correct agent
would. Its structured answer and its text come from tool results only, so it
scores 100 percent exactly when the tools, the gold queries and the scorer
agree. `null` always says it found nothing, which gives the floor score.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from rxdelta.ask.answer import (
    ChangeAnswer,
    ChangeItem,
    ClarifyAnswer,
    ClarifyCandidate,
    CostRangeAnswer,
    CountAnswer,
    CoverageAnswer,
    DrugListAnswer,
    NoDataAnswer,
    NoDataReason,
    QuestionContext,
    RefusalAnswer,
    Runner,
    RunnerOutput,
    StructuredAnswer,
    ToolCallRecord,
)
from rxdelta.evals.items import EvalItem
from rxdelta.tools.context import ToolContext
from rxdelta.tools.envelope import ToolResult
from rxdelta.tools.registry import call_tool
from rxdelta.types import ChangeType

REFUSAL_TEXT = (
    "rxdelta reports formulary data and does not give medical advice. A doctor or pharmacist "
    "can help with that question."
)
_REASONS = {
    "unknown_drug": NoDataReason.unknown_drug,
    "unknown_plan": NoDataReason.no_matching_changes,
    "month_not_loaded": NoDataReason.month_not_loaded,
}


class _NoDataError(Exception):
    """A tool result that ends the run with a no data answer."""

    def __init__(self, reason: NoDataReason, note: str) -> None:
        super().__init__(note)
        self.reason = reason
        self.note = note


class _Session:
    """One question's worth of tool calls, recorded as they happen."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.calls: list[ToolCallRecord] = []

    def call(self, name: str, **args: Any) -> ToolResult[Any]:
        clean = {k: v for k, v in args.items() if v is not None}
        started = time.perf_counter()
        result = call_tool(name, clean, self.ctx)
        self.calls.append(
            ToolCallRecord(
                name=name,
                args=clean,
                result=result.model_dump(mode="json"),
                duration_ms=round(1000 * (time.perf_counter() - started)),
            )
        )
        return result

    def need(self, result: ToolResult[Any], empty: NoDataReason) -> Any:
        """The data of a successful result, or stop with the matching no data reason."""
        if result.ok:
            return result.data
        code = result.error.code.value if result.error else "internal"
        reason = _REASONS.get(code, empty)
        raise _NoDataError(reason, result.note or "No data.")

    def months(self, args: dict[str, Any]) -> tuple[str, str]:
        start, end = str(args["from"]), str(args["to"])
        if {start, end} & {"latest", "previous"}:
            loaded = [
                m.month
                for m in self.need(self.call("list_months"), NoDataReason.month_not_loaded).months
            ]
            named = {"latest": loaded[-1], "previous": loaded[-2]}
            start, end = named.get(start, start), named.get(end, end)
        return start, end

    def drug(self, mention: str) -> tuple[list[str], dict[str, str]]:
        """RXCUIs and display names for a drug as the question names it."""
        text = mention.strip()
        if text.upper().startswith("RXCUI"):
            rxcui = text.split()[-1]
            return [rxcui], {}
        data = self.need(self.call("find_drug", query=text, limit=25), NoDataReason.unknown_drug)
        names = {d.rxcui: d.display_name for p in data.products for d in p.drugs}
        return sorted(names), names

    def formulary(self, args: dict[str, Any], month: str) -> str | None:
        if args.get("plan_query"):
            data = self.need(
                self.call("find_plan", query=str(args["plan_query"]), month=month),
                NoDataReason.no_matching_changes,
            )
            return str(data.plans[0].formulary_id)
        if args.get("formulary_id"):
            return str(args["formulary_id"])
        return None

    def change_rows(self, **filters: Any) -> list[Any]:
        rows: list[Any] = []
        while True:
            result = self.call("get_changes", offset=len(rows), limit=100, **filters)
            data = self.need(result, NoDataReason.no_matching_changes)
            rows += data.rows
            if len(rows) >= (result.total or 0):
                return rows


def _money(value: float) -> str:
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _change_line(row: Any) -> str:
    label = ChangeType(row.change_type).label.lower()
    before = row.before_value or "not covered"
    after = row.after_value or "not covered"
    return f"{row.display_name} on formulary {row.formulary_id}: {label}, {before} to {after}."


class OracleRunner:
    name = "oracle"

    def __init__(self, items: list[EvalItem], ctx: ToolContext) -> None:
        self._ctx = ctx
        self._gold: dict[str, EvalItem] = {}
        for item in items:
            if item.question in self._gold and self._gold[item.question].gold != item.gold:
                raise ValueError(f"Two items ask {item.question!r} with different gold queries")
            self._gold[item.question] = item

    def answer(self, question: str, context: QuestionContext) -> RunnerOutput:
        del context  # the gold query already names the months
        started = time.perf_counter()
        session = _Session(self._ctx)
        item = self._gold[question]
        try:
            answer, text = self._solve(session, item.gold.query, dict(item.gold.args))
        except _NoDataError as stop:
            answer, text = NoDataAnswer(reason=stop.reason), f"No answer in the data: {stop.note}"
        return RunnerOutput(
            answer=answer,
            final_text=text,
            tool_calls=session.calls,
            latency_ms=round(1000 * (time.perf_counter() - started)),
        )

    def _solve(self, s: _Session, query: str, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        handler: Callable[[_Session, dict[str, Any]], tuple[StructuredAnswer, str]] = getattr(
            self, f"_{query}"
        )
        return handler(s, args)

    def _refusal(self, _s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        return RefusalAnswer(reason=args.get("reason", "medical_advice")), REFUSAL_TEXT

    def _unknown_drug(self, s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        # find_drug normally stops here with unknown_drug. If it matches a name
        # that should not exist, answer with the match so the miss is scored.
        rxcuis, names = s.drug(str(args["drug"]))
        return DrugListAnswer(rxcuis=rxcuis), "Matched: " + "; ".join(names.values()) + "."

    def _month_not_loaded(self, s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        data = s.need(s.call("list_months"), NoDataReason.month_not_loaded)
        loaded = {m.month for m in data.months}
        missing = [m for m in args["months"] if m not in loaded]
        if not missing:
            raise _NoDataError(
                NoDataReason.no_matching_changes, "Every month asked about is loaded."
            )
        listed = ", ".join(m.month for m in data.months)
        return (
            NoDataAnswer(reason=NoDataReason.month_not_loaded),
            f"That month is not loaded. The loaded months are {listed}.",
        )

    def _coverage(self, s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        rxcuis, _ = s.drug(str(args["drug"]))
        result = s.call(
            "get_coverage",
            rxcui=rxcuis[0],
            month=args["month"],
            formulary_id=args.get("formulary_id"),
            plan_id=args.get("plan_id"),
        )
        data = s.need(result, NoDataReason.not_on_formulary)
        row = data.rows[0]
        c = row.coverage
        answer = CoverageAnswer(
            rxcui=data.rxcui,
            month=data.month,
            formulary_id=row.formulary_id,
            tier=c.tier,
            prior_auth=c.prior_auth,
            step_therapy=c.step_therapy,
            quantity_limit=c.quantity_limit,
            quantity_limit_amount=c.quantity_limit_amount,
            quantity_limit_days=c.quantity_limit_days,
        )
        limit = (
            f"a quantity limit of {c.quantity_limit_amount:g} per {c.quantity_limit_days} days"
            if c.quantity_limit and c.quantity_limit_amount is not None
            else ("a quantity limit" if c.quantity_limit else "no quantity limit")
        )
        prior_auth = "prior authorization" if c.prior_auth else "no prior authorization"
        step = "step therapy" if c.step_therapy else "no step therapy"
        text = (
            f"{data.display_name} is on tier {c.tier} of formulary {row.formulary_id} in "
            f"{data.month}, with {prior_auth}, {step} and {limit}."
        )
        return answer, text

    def _drug_change(self, s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        start, end = s.months(args)
        rxcuis, _ = s.drug(str(args["drug"]))
        formulary = s.formulary(args, end)
        rows = s.change_rows(
            from_month=start,
            to_month=end,
            rxcuis=rxcuis,
            change_types=args.get("change_types"),
            formulary_ids=[formulary] if formulary else None,
        )
        items = sorted(
            {
                ChangeItem(
                    rxcui=r.rxcui,
                    formulary_id=r.formulary_id,
                    change_type=r.change_type,
                    before=r.before_value,
                    after=r.after_value,
                )
                for r in rows
            },
            key=lambda i: (i.rxcui, i.formulary_id, i.change_type.value),
        )
        return ChangeAnswer(rxcuis=rxcuis, items=items), " ".join(_change_line(r) for r in rows)

    def _list_filters(self, s: _Session, args: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        start, end = s.months(args)
        formulary = s.formulary(args, end)
        return (
            start,
            end,
            {
                "from_month": start,
                "to_month": end,
                "change_types": args.get("change_types"),
                "formulary_ids": [formulary] if formulary else None,
                "tier_from": args.get("tier_from"),
                "tier_to": args.get("tier_to"),
            },
        )

    def _filtered_list(self, s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        _, _, filters = self._list_filters(s, args)
        rows = s.change_rows(**filters)
        return _drug_list(rows)

    def _count_drugs(self, s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        start, end, filters = self._list_filters(s, args)
        data = s.need(
            s.call("count_changes", group_by="change_type", **filters),
            NoDataReason.no_matching_changes,
        )
        drugs = data.totals.drugs
        return CountAnswer(count=drugs, unit="drugs"), (
            f"{drugs} drugs match between {start} and {end}."
        )

    def _cost_change(self, s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        rxcuis, _ = s.drug(str(args["drug"]))
        data = s.need(
            s.call(
                "estimate_cost_change",
                rxcui=rxcuis[0],
                from_month=args["from"],
                to_month=args["to"],
                plan_id=args.get("plan_id"),
                formulary_id=args.get("formulary_id"),
            ),
            NoDataReason.not_on_formulary,
        )
        if data.change is None:
            raise _NoDataError(NoDataReason.no_matching_changes, "The change is not priced.")
        low, high = data.change.low, data.change.high
        text = (
            f"Estimated change in monthly cost sharing for {data.display_name}: "
            f"{_money(low)} to {_money(high)}. This is a range, not a prediction."
        )
        return CostRangeAnswer(rxcui=data.rxcui, low=low, high=high), text

    def _class_changes(self, s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        start, end = s.months(args)
        members, _ = s.drug(str(args["class_token"]))
        formulary = s.formulary(args, end)
        rows = s.change_rows(
            from_month=start,
            to_month=end,
            rxcuis=members,
            change_types=args.get("change_types"),
            formulary_ids=[formulary] if formulary else None,
        )
        answer, text = _drug_list(rows)
        if not args.get("with_tiers"):
            return answer, text
        assert isinstance(answer, DrugListAnswer) and formulary is not None
        tiers: dict[str, int] = {}
        lines = []
        for rxcui in answer.rxcuis:
            result = s.call("get_coverage", rxcui=rxcui, month=end, formulary_id=formulary)
            if result.ok and result.data is not None:
                tiers[rxcui] = result.data.rows[0].coverage.tier
                lines.append(f"{result.data.display_name} is on tier {tiers[rxcui]} now.")
        return answer.model_copy(update={"tiers": tiers}), " ".join([text, *lines])

    def _changes_where_coverage(
        self, s: _Session, args: dict[str, Any]
    ) -> tuple[StructuredAnswer, str]:
        _, end, filters = self._list_filters(s, args)
        rows = s.change_rows(**filters)
        target = str(args.get("coverage_formulary_id") or filters["formulary_ids"][0])
        test: dict[str, Any] = dict(args["coverage"])
        keep = []
        for rxcui in sorted({r.rxcui for r in rows}):
            result = s.call("get_coverage", rxcui=rxcui, month=end, formulary_id=target)
            covered = result.ok and result.data is not None
            if "covered" in test and covered != bool(test["covered"]):
                continue
            checks = {k: v for k, v in test.items() if k != "covered"}
            if checks and not covered:
                continue
            if covered and checks:
                coverage = result.data.rows[0].coverage  # type: ignore[union-attr]
                if any(getattr(coverage, k) != v for k, v in checks.items()):
                    continue
            keep.append(rxcui)
        if not keep:
            raise _NoDataError(NoDataReason.no_matching_changes, "No drug passes both tests.")
        return _drug_list([r for r in rows if r.rxcui in keep])

    def _changes_intersection(
        self, s: _Session, args: dict[str, Any]
    ) -> tuple[StructuredAnswer, str]:
        start, end = s.months(args)
        sets = []
        rows: list[Any] = []
        for change_type in args["all_of"]:
            found = s.change_rows(
                from_month=start,
                to_month=end,
                change_types=[change_type],
                formulary_ids=[args["formulary_id"]],
            )
            rows += found
            sets.append({r.rxcui for r in found})
        both = set.intersection(*sets)
        if not both:
            raise _NoDataError(NoDataReason.no_matching_changes, "No drug has every change.")
        return _drug_list([r for r in rows if r.rxcui in both])

    def _clarify(self, s: _Session, args: dict[str, Any]) -> tuple[StructuredAnswer, str]:
        data = s.need(
            s.call("find_drug", query=str(args["drug"]), limit=25), NoDataReason.unknown_drug
        )
        candidates = [ClarifyCandidate(product=p.product, rxcuis=p.rxcuis) for p in data.products]
        listed = "; ".join(f"{p.product} (RXCUI {', '.join(p.rxcuis)})" for p in data.products)
        return ClarifyAnswer(candidates=candidates), (
            f"{args['drug']} matches more than one drug: {listed}. Which one do you mean?"
        )


def _drug_list(rows: list[Any]) -> tuple[StructuredAnswer, str]:
    names = {r.rxcui: r.display_name for r in rows}
    ordered = sorted(names)
    answer = DrugListAnswer(rxcuis=ordered, display_names=[names[r] for r in ordered])
    return answer, "Matching drugs: " + "; ".join(names[r] for r in ordered) + "."


class NullRunner:
    name = "null"

    def __init__(self, items: list[EvalItem], ctx: ToolContext) -> None:
        del items, ctx

    def answer(self, question: str, context: QuestionContext) -> RunnerOutput:
        del question, context
        return RunnerOutput(
            answer=NoDataAnswer(reason=NoDataReason.no_matching_changes),
            final_text="I could not find an answer in the data.",
            latency_ms=0,
        )


RunnerFactory = Callable[[list[EvalItem], ToolContext], Runner]

RUNNERS: dict[str, RunnerFactory] = {
    "oracle": OracleRunner,
    "null": NullRunner,
}


def make_runner(name: str, items: list[EvalItem], ctx: ToolContext) -> Runner:
    if name not in RUNNERS:
        raise KeyError(f"No runner named {name!r}. Runners: {', '.join(RUNNERS)}.")
    return RUNNERS[name](items, ctx)


def describe_call(call: ToolCallRecord) -> str:
    error = (call.result.get("error") or {}).get("code")
    status = "ok" if call.result.get("ok") else f"error {error}"
    return f"{call.name}({json.dumps(call.args, sort_keys=True)}) {status}"
