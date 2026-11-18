"""Summary numbers for runners that keep a trace: drafts, guards, turns, cache,
and the spread across repeats."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

from rxdelta.ask.answer import RunnerOutput
from rxdelta.evals import grounding
from rxdelta.evals.score import ItemScore

GUARDS = ("schema", "grounding", "consistency")


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[max(1, math.ceil(share * len(ordered))) - 1])


def first_draft_ungrounded(question: str, output: RunnerOutput) -> list[str] | None:
    """Numbers in the first submitted draft that no tool result made before it holds."""
    drafts = (output.trace or {}).get("drafts") or []
    if not drafts:
        return None
    first = drafts[0]
    before = [c.result for c in output.tool_calls[: int(first.get("tool_calls_before", 0))]]
    text = first.get("final_text") if isinstance(first.get("final_text"), str) else ""
    return list(grounding.check(str(text), question, before).ungrounded)


def _guard_stats(outputs: list[RunnerOutput]) -> dict[str, dict[str, int]]:
    stats = {g: {"fired": 0, "retried": 0, "fixed": 0, "accepted_failing": 0} for g in GUARDS}
    for output in outputs:
        trace = output.trace or {}
        events = [e for e in trace.get("events") or [] if e.get("type") == "guard"]
        failed_final = set(trace.get("guards_failed") or [])
        for guard in GUARDS:
            mine = [e for e in events if e.get("guard") == guard]
            if not mine:
                continue
            stats[guard]["fired"] += 1
            if any(e.get("retry") for e in mine):
                stats[guard]["retried"] += 1
                if guard not in failed_final and output.error is None:
                    stats[guard]["fixed"] += 1
            if guard in failed_final:
                stats[guard]["accepted_failing"] += 1
    return stats


def agent_summary(
    questions: list[str], outputs: list[RunnerOutput], scores: list[ItemScore]
) -> dict[str, Any] | None:
    traced = [(q, o, s) for q, o, s in zip(questions, outputs, scores, strict=True) if o.trace]
    if not traced:
        return None
    drafts = [first_draft_ungrounded(q, o) for q, o, _ in traced]
    with_draft = [d for d in drafts if d is not None]
    turns = [float(len((o.trace or {}).get("turns") or [])) for _, o, _ in traced]
    usage: dict[str, int] = defaultdict(int)
    for _, o, _ in traced:
        for key, value in ((o.trace or {}).get("usage") or {}).items():
            usage[key] += int(value)
    prompt = usage["input_tokens"] + usage["cache_write_tokens"] + usage["cache_read_tokens"]
    settings = (traced[0][1].trace or {}).get("settings") or {}
    return {
        "settings": settings,
        "first_draft_ungrounded_rate": _rate(sum(1 for d in with_draft if d), len(with_draft)),
        "final_ungrounded_rate": _rate(
            sum(1 for _, _, s in traced if s.ungrounded_numbers), len(traced)
        ),
        "answers_with_a_draft": len(with_draft),
        "guards": _guard_stats([o for _, o, _ in traced]),
        "turns": {
            "mean": round(sum(turns) / len(turns), 3),
            "p50": _percentile(turns, 0.5),
            "max": max(turns),
        },
        "usage": dict(usage),
        "cache_hit_share": _rate(usage["cache_read_tokens"], prompt),
        "cost_per_question": round(sum(o.cost_usd for _, o, _ in traced) / len(traced), 6),
        "cost_per_correct": _per_correct(traced),
        "errors": sum(1 for _, o, _ in traced if o.error),
        "stopped_by": stop_reasons([o for _, o, _ in traced]),
    }


def _per_correct(traced: list[tuple[str, RunnerOutput, ItemScore]]) -> float | None:
    correct = sum(1 for _, _, s in traced if s.correct)
    total = sum(o.cost_usd for _, o, _ in traced)
    return round(total / correct, 6) if correct else None


def stop_reasons(outputs: list[RunnerOutput]) -> dict[str, int]:
    """Answers that ended with an error, by what ended them."""
    kinds = {
        "model turns": "turn_limit",
        "tool calls": "tool_call_limit",
        "per question cap": "cost_cap",
        "plain text": "plain_text",
        "api error": "api_error",
        "did not validate": "schema",
        "stop reason refusal": "model_refusal",
    }
    counts = dict.fromkeys([*kinds.values(), "other"], 0)
    for output in outputs:
        if output.error:
            found = next((v for k, v in kinds.items() if k in output.error), "other")
            counts[found] += 1
    return counts


def repeat_summary(ids: list[str], repeats: list[int], scores: list[ItemScore]) -> dict[str, Any]:
    by_repeat: dict[int, list[bool]] = defaultdict(list)
    by_item: dict[str, list[bool]] = defaultdict(list)
    for item_id, repeat, score in zip(ids, repeats, scores, strict=True):
        by_repeat[repeat].append(score.correct)
        by_item[item_id].append(score.correct)
    accuracies = [round(sum(v) / len(v), 4) for _, v in sorted(by_repeat.items())]
    return {
        "repeats": len(by_repeat),
        "accuracy_by_repeat": accuracies,
        "mean": round(statistics.fmean(accuracies), 4),
        "min": min(accuracies),
        "max": max(accuracies),
        "stdev": round(statistics.stdev(accuracies), 4) if len(accuracies) > 1 else 0.0,
        "items_right_every_repeat": sum(1 for v in by_item.values() if all(v)),
        "items_right_some_repeats": sum(1 for v in by_item.values() if any(v) and not all(v)),
        "items_right_no_repeat": sum(1 for v in by_item.values() if not any(v)),
    }
