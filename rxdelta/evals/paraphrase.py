"""Casual paraphrases of eval questions: drafted by a model, approved by a person,
then built into their own eval set with the original gold answers."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from rxdelta.agent.client import ModelClient, ModelRequest, Usage
from rxdelta.evals.harness import EvalError, load_set, select
from rxdelta.evals.items import EvalItem, manifest_path, sha256_file, write_items

COLUMNS = ("item_id", "category", "original", "paraphrase", "approved")
_APPROVED = frozenset({"yes", "y", "true", "1", "x", "approved"})
INSTRUCTION = (
    "Rewrite the question below the way someone who does not know pharmacy or insurance terms "
    "would type it casually, for example in a chat box. Keep its meaning exactly. Keep every "
    "drug name, plan ID, formulary ID, number and month as written. Reply with the rewritten "
    "question only, on one line."
)


@dataclass(frozen=True)
class Drafted:
    rows: list[dict[str, str]]
    usage: Usage


def draft(
    set_path: Path, n: int, client: ModelClient, model: str, max_tokens: int, effort: str
) -> Drafted:
    """One paraphrase for each of n dev items, spread over categories."""
    items = select(load_set(set_path).items, "dev", None, n, stratified=True)
    rows = []
    usage = Usage()
    for item in items:
        response = client.create(
            ModelRequest(
                model=model,
                system=INSTRUCTION,
                messages=[{"role": "user", "content": item.question}],
                tools=[],
                max_tokens=max_tokens,
                effort=effort,
            )
        )
        usage = usage + response.usage
        rows.append(
            {
                "item_id": item.id,
                "category": item.category,
                "original": item.question,
                "paraphrase": " ".join(response.text().split()),
                "approved": "",
            }
        )
    return Drafted(rows=rows, usage=usage)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _approved_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    missing = set(COLUMNS) - set(rows[0] if rows else COLUMNS)
    if missing:
        raise EvalError(f"{csv_path} is missing columns: {', '.join(sorted(missing))}.")
    return [r for r in rows if r["approved"].strip().lower() in _APPROVED]


def build(set_path: Path, csv_path: Path, out: Path) -> list[EvalItem]:
    """The approved paraphrases as an eval set with the original gold answers."""
    source = load_set(set_path)
    by_id = {i.id: i for i in source.items}
    rows = _approved_rows(csv_path)
    if not rows:
        raise EvalError(f"No approved rows in {csv_path}. Mark rows approved with 'yes'.")
    items: list[EvalItem] = []
    seen: set[str] = set()
    for row in rows:
        original = by_id.get(row["item_id"])
        if original is None:
            raise EvalError(f"{row['item_id']} is not an item of {set_path}.")
        text = " ".join(row["paraphrase"].split())
        if not text or text == original.question or text in seen:
            raise EvalError(f"{row['item_id']}: the paraphrase is empty, unchanged or repeated.")
        seen.add(text)
        items.append(
            original.model_copy(
                update={
                    "id": f"{original.id}-para",
                    "question": text,
                    "tags": [*original.tags, "paraphrase"],
                }
            )
        )
    write_items(out, items)
    manifest = {
        "eval_set": out.stem,
        "file": out.name,
        "file_sha256": sha256_file(out),
        "items": len(items),
        "source_set": str(set_path),
        "source_set_sha256": source.sha256,
        "paraphrases_csv": str(csv_path),
        "paraphrases_csv_sha256": sha256_file(csv_path),
        "months": source.manifest.get("months"),
        "releases": source.manifest.get("releases"),
        "db_row_counts": source.manifest.get("db_row_counts"),
    }
    manifest_path(out).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return items
