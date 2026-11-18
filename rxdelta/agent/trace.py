"""One item's run read back from a run folder, as plain text."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rxdelta.agent.loop import summarize_result
from rxdelta.types import RxdeltaError


def find_records(run_dir: Path, item_id: str) -> list[dict[str, Any]]:
    """Every results line for one item, one per repeat."""
    results = run_dir / "results.jsonl"
    if not results.is_file():
        raise RxdeltaError(f"{run_dir} is not an eval run folder (no results.jsonl).")
    records = [
        json.loads(line)
        for line in results.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("id") == item_id
    ]
    if not records:
        raise RxdeltaError(f"No item {item_id!r} in {results}.")
    return records


def _short(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render(record: dict[str, Any]) -> str:
    output = record["output"]
    score = record["score"]
    trace = record.get("trace") or {}
    settings = trace.get("settings") or {}
    lines = [
        f"{record['id']} ({record['category']}, {record['split']}"
        + (f", repeat {record['repeat']}" if "repeat" in record else "")
        + ")",
        f"Question: {record['question']}",
    ]
    if settings:
        lines.append(
            f"Model {settings.get('model')}, prompt {settings.get('prompt_version')}, "
            f"guards {settings.get('guards')}"
        )
    lines.append("")
    calls = iter(output.get("tool_calls") or [])
    for event in trace.get("events") or []:
        kind = event.get("type")
        if kind == "tool_call":
            call = next(calls, None)
            summary = summarize_result(call["result"]) if call else event.get("summary")
            cut = " (shortened for the model)" if event.get("shortened") else ""
            lines.append(f"  tool {event['name']} {json.dumps(event['args'], sort_keys=True)}")
            lines.append(f"       -> {summary}{cut}")
        elif kind == "guard":
            action = "retry" if event.get("retry") else "accepted anyway"
            lines.append(
                f"  guard {event['guard']} failed, {action}: {'; '.join(event['problems'])}"
            )
        elif kind == "reminder":
            lines.append(f"  reminder sent after a plain text stop ({event.get('stop_reason')})")
        elif kind == "accepted":
            lines.append(f"  answer accepted ({event.get('kind')})")
        elif kind == "stopped":
            lines.append(f"  stopped: {event.get('error')}")
    if not trace:
        for call in output.get("tool_calls") or []:
            lines.append(f"  tool {call['name']} {json.dumps(call['args'], sort_keys=True)}")
            lines.append(f"       -> {summarize_result(call['result'])}")
    drafts = trace.get("drafts") or []
    if len(drafts) > 1:
        lines += ["", f"First draft: {_short(drafts[0].get('final_text'))}"]
    lines += [
        "",
        f"Final text: {output.get('final_text') or '(none)'}",
        f"Got:      {_short(output.get('answer'))}",
        f"Expected: {_short(record.get('expected'))}",
    ]
    if record.get("alternative"):
        lines.append(f"Also accepted: {_short(record['alternative'])}")
    verdict = "right" if score.get("correct") else "wrong"
    extra = []
    if score.get("ungrounded_numbers"):
        extra.append(f"ungrounded {', '.join(score['ungrounded_numbers'])}")
    if output.get("error"):
        extra.append(f"error {output['error']}")
    if trace.get("guards_failed"):
        extra.append(f"guards failed {', '.join(trace['guards_failed'])}")
    lines.append(f"Scored {verdict}" + (f" ({'; '.join(extra)})" if extra else "") + ".")
    lines.append(
        f"{len(output.get('tool_calls') or [])} tool calls, {len(trace.get('turns') or [])} turns, "
        f"{output.get('input_tokens', 0)} input and {output.get('output_tokens', 0)} output "
        f"tokens, ${output.get('cost_usd', 0):.4f}, {output.get('latency_ms', 0)} ms."
    )
    return "\n".join(lines)
