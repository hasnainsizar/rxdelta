"""The run summary as markdown and as an HTML page in the report's house style."""

from __future__ import annotations

import json
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from rxdelta.ask.answer import RunnerOutput
from rxdelta.config import Config
from rxdelta.evals.items import EvalItem
from rxdelta.evals.runners import describe_call
from rxdelta.evals.score import ItemScore
from rxdelta.report.fonts import embedded_faces
from rxdelta.report.html import _TEMPLATE_DIR


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _agent_lines(summary: dict[str, Any]) -> list[str]:
    agent = summary.get("agent")
    if not agent:
        return []
    settings = agent["settings"]
    reps = summary["repeats"]
    guards_on = ", ".join(g for g, on in settings["guards"].items() if on) or "none"
    lines = [
        f"Model `{settings['model']}`, prompt `{settings['prompt_version']}`, guards on: "
        f"{guards_on}, {reps['repeats']} repeat(s).",
        "",
        "| agent metric | value |",
        "|---|---|",
        f"| accuracy by repeat | {', '.join(_pct(a) for a in reps['accuracy_by_repeat'])} |",
        f"| mean, min, max, stdev | {_pct(reps['mean'])}, {_pct(reps['min'])}, "
        f"{_pct(reps['max'])}, {100 * reps['stdev']:.1f} points |",
        f"| items right on every, some, no repeat | {reps['items_right_every_repeat']}, "
        f"{reps['items_right_some_repeats']}, {reps['items_right_no_repeat']} |",
        f"| first draft ungrounded | {_pct(agent['first_draft_ungrounded_rate'])} |",
        f"| final ungrounded | {_pct(agent['final_ungrounded_rate'])} |",
        f"| turns per question, mean | {agent['turns']['mean']:.2f} |",
        f"| cache hit share | {_pct(agent['cache_hit_share'])} |",
        f"| cost per question | ${agent['cost_per_question']:.4f} |",
        f"| run cost | ${summary['run_cost_usd']:.4f} |",
        f"| latency p50, p95 | {summary['cost_speed']['latency_ms']['p50']:.0f} ms, "
        f"{summary['cost_speed']['latency_ms']['p95']:.0f} ms |",
    ]
    for guard, row in agent["guards"].items():
        lines.append(
            f"| guard {guard}: fired, retried, fixed, accepted failing | {row['fired']}, "
            f"{row['retried']}, {row['fixed']}, {row['accepted_failing']} |"
        )
    if summary.get("stopped_early"):
        lines.append(f"| stopped early | {summary['stopped_early']} |")
    return [*lines, ""]


def summary_markdown(summary: dict[str, Any]) -> str:
    overall = summary["overall"]
    tools = summary["tools"]
    grounding = summary["grounding"]
    lines = [
        f"Runner `{summary['runner']}` on the `{summary['split']}` split, "
        f"{overall['items']} items.",
        "",
        *_agent_lines(summary),
        "| metric | value |",
        "|---|---|",
        f"| accuracy | {_pct(overall['accuracy'])} ({overall['correct']} of {overall['items']}) |",
        f"| mean F1, drug lists | {_pct(overall['mean_f1_drug_list'])} |",
        f"| false refusals | {overall['false_refusals']} of {overall['answerable']} answerable |",
        f"| missed refusals | {overall['missed_refusals']} of {overall['out_of_scope']} "
        "out of scope |",
        f"| required tools called | {_pct(tools['required_called_rate'])} |",
        f"| mean tool calls | {tools['mean_calls']:.2f} |",
        f"| items with ungrounded numbers | {grounding['items_with_ungrounded']} |",
        "",
        "| category | items | accuracy |",
        "|---|---|---|",
    ]
    for category, row in summary["by_category"].items():
        lines.append(f"| {category} | {row['items']} | {_pct(row['accuracy'])} |")
    return "\n".join(lines) + "\n"


def _worst(
    items: list[EvalItem], outputs: list[RunnerOutput], scores: list[ItemScore], limit: int
) -> list[dict[str, Any]]:
    ranked = sorted(
        range(len(items)),
        key=lambda i: (
            scores[i].correct,
            scores[i].f1 if scores[i].f1 is not None else (1.0 if scores[i].correct else 0.0),
            -len(scores[i].ungrounded_numbers),
            items[i].id,
        ),
    )
    out = []
    for i in ranked[:limit]:
        if scores[i].correct and not scores[i].ungrounded_numbers:
            break
        out.append(
            {
                "id": items[i].id,
                "category": items[i].category,
                "question": items[i].question,
                "expected": json.dumps(items[i].expected.model_dump(mode="json"), indent=1),
                "got": json.dumps(outputs[i].answer.model_dump(mode="json"), indent=1),
                "text": outputs[i].final_text,
                "calls": [describe_call(c) for c in outputs[i].tool_calls],
                "ungrounded": scores[i].ungrounded_numbers,
                "error": scores[i].error,
            }
        )
    return out


def render_html(
    summary: dict[str, Any],
    items: list[EvalItem],
    outputs: list[RunnerOutput],
    scores: list[ItemScore],
    config: Config,
) -> str:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    kinds = sorted(
        set(summary["kind_confusion"])
        | {g for row in summary["kind_confusion"].values() for g in row}
    )
    return env.get_template("eval_report.html.j2").render(
        s=summary,
        pct=_pct,
        faces=embedded_faces(),
        kinds=kinds,
        worst=_worst(items, outputs, scores, config.eval.worst_items),
    )
