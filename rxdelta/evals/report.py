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


def summary_markdown(summary: dict[str, Any]) -> str:
    overall = summary["overall"]
    tools = summary["tools"]
    grounding = summary["grounding"]
    lines = [
        f"Runner `{summary['runner']}` on the `{summary['split']}` split, "
        f"{overall['items']} items.",
        "",
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
