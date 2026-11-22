"""Run the values guard over a saved eval run's answers, with no model calls.

Prints every answer the guard would have sent back, with the guard's message.
Usage: uv run python scripts/check_value_guard.py runs/<run folder>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from rxdelta.agent.guards import value_problems
from rxdelta.ask.answer import parse_answer


def flagged(run_dir: Path) -> list[tuple[str, int, bool, list[str]]]:
    """(item id, repeat, scored correct, problems) for each answer the guard rejects."""
    out = []
    for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        output = record["output"]
        changes = [c["result"] for c in output["tool_calls"] if c["name"] == "get_changes"]
        problems = value_problems(parse_answer(output["answer"]), changes)
        if problems:
            item = (record["id"], int(record.get("repeat", 1)), record["score"]["correct"])
            out.append((*item, problems))
    return out


def main() -> None:
    run_dir = Path(sys.argv[1])
    found = flagged(run_dir)
    for item_id, repeat, correct, problems in found:
        verdict = "scored right" if correct else "scored wrong"
        print(f"{item_id} repeat {repeat} ({verdict}): {' | '.join(problems)}")
    print(f"{len(found)} answer(s) flagged in {run_dir}.")


if __name__ == "__main__":
    main()
