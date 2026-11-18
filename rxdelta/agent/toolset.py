"""What the model sees: the tool list with `submit_answer`, and tool results
reduced to the fields it should read."""

from __future__ import annotations

import copy
import json
from typing import Any

from rxdelta.ask.answer import ANSWER_ADAPTER
from rxdelta.tools.registry import anthropic_tools

SUBMIT = "submit_answer"
# The fields of a ToolResult the model reads. Provenance is left out: it can be
# large and is meant for checks, not for reading.
MODEL_FIELDS = ("ok", "data", "total", "truncated", "note", "error")

SUBMIT_DESCRIPTION = (
    "Submit the final answer. This is the only way to finish; call it exactly once, after "
    "the tools have given you every fact the answer states. 'answer' is the structured "
    "answer and 'final_text' is what the person reads. Answer kinds: drug_list, a set of "
    "drugs by RXCUI (mark truncated and give total when a tool said the list was partial); "
    "count, a number of drugs (or formularies, plans or rows) taken from count_changes; "
    "change, the coverage changes for the drugs asked about, one item per RXCUI, formulary "
    "and change type with before and after exactly as get_changes gave them; coverage, one "
    "drug's tier and restrictions on one formulary in one month; cost_range, the low and high "
    "monthly change from estimate_cost_change. Use no_data when the question can be answered "
    "but the data says nothing: the drug is unknown, a month is not loaded, nothing of that "
    "kind changed, or the drug is not on that formulary. Use refusal for medical advice or "
    "questions outside formulary data. Use clarify when a drug name matches several distinct "
    "products and the question does not say which; list every candidate product with its "
    "RXCUIs. Every number in final_text must appear in a tool result."
)


def _flatten(node: Any, defs: dict[str, Any]) -> Any:
    """Inline references, drop titles, docstrings and the discriminator, and spell
    constants as enums."""
    if isinstance(node, list):
        return [_flatten(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
        target.pop("title", None)
        target.pop("description", None)
        return _flatten({**target, **{k: v for k, v in node.items() if k != "$ref"}}, defs)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("$defs", "title", "discriminator"):
            continue
        if key == "const":
            out["enum"] = [value]
            continue
        if key == "properties":
            out[key] = {name: _flatten(schema, defs) for name, schema in value.items()}
            continue
        out["anyOf" if key == "oneOf" else key] = _flatten(value, defs)
    any_of = out.get("anyOf")
    # An optional field whose default is null: omitting it already means null.
    if (
        isinstance(any_of, list)
        and len(any_of) == 2
        and {"type": "null"} in any_of
        and "default" in out
        and out["default"] is None
    ):
        branch = next(b for b in any_of if b != {"type": "null"})
        out = {**branch, **{k: v for k, v in out.items() if k not in ("anyOf", "default")}}
    return out


def answer_schema() -> dict[str, Any]:
    """The answer union as a flat schema in which every branch requires its kind."""
    raw = ANSWER_ADAPTER.json_schema()
    flat: dict[str, Any] = _flatten(raw, raw.get("$defs", {}))
    for branch in flat["anyOf"]:
        branch["required"] = sorted({"kind", *branch.get("required", [])})
        branch["properties"]["kind"].pop("default", None)
    return flat


def submit_tool() -> dict[str, Any]:
    return {
        "name": SUBMIT,
        "description": SUBMIT_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {
                "answer": answer_schema(),
                "final_text": {
                    "type": "string",
                    "description": "The answer in plain English for the person who asked.",
                },
            },
            "required": ["answer", "final_text"],
            "additionalProperties": False,
        },
    }


def model_tools() -> list[dict[str, Any]]:
    """The seven data tools, unchanged, then submit_answer."""
    return [*anthropic_tools(), submit_tool()]


def _longest_list(data: Any) -> tuple[Any, str] | None:
    """The dict and key holding the longest list anywhere inside data."""
    best: tuple[Any, str] | None = None
    best_len = 0
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, list) and len(value) > best_len:
                    best, best_len = (node, key), len(value)
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
    return best


def model_view(result: dict[str, Any], max_chars: int) -> tuple[str, bool]:
    """The JSON the model reads for one tool result, and whether it was shortened.

    Past max_chars the longest list is cut until the JSON fits; total is kept
    and the note says the result was shortened and how to narrow it."""
    view = {key: result.get(key) for key in MODEL_FIELDS}
    text = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return text, False
    view = copy.deepcopy(view)
    found = _longest_list(view.get("data"))
    if found is None:
        return text[:max_chars], True
    holder, key = found
    full = len(holder[key])
    keep = full
    while keep > 0:
        keep //= 2
        holder[key] = holder[key][:keep]
        shown = f"Shown {keep} of {full} entries in '{key}'"
        view["truncated"] = True
        view["note"] = (
            f"{shown}: the result was shortened to fit. Narrow the query (a formulary, a "
            "change type, a drug) or page with limit and offset; use count_changes for counts. "
            + (result.get("note") or "")
        ).strip()
        text = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
        if len(text) <= max_chars:
            break
    return text, True
