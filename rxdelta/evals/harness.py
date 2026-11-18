"""`rxdelta eval run` and `rxdelta eval compare`."""

from __future__ import annotations

import json
import math
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rxdelta.ask.answer import NoDataAnswer, NoDataReason, Runner, RunnerOutput
from rxdelta.config import CATEGORIES, Config
from rxdelta.evals import report
from rxdelta.evals.items import EvalItem, manifest_path, read_items, sha256_file
from rxdelta.evals.runners import make_runner
from rxdelta.evals.score import ItemScore, score_item
from rxdelta.tools.context import ToolContext
from rxdelta.types import RxdeltaError


class EvalError(RxdeltaError):
    """The eval cannot run or compare as asked."""


@dataclass(frozen=True)
class EvalSet:
    path: Path
    items: list[EvalItem]
    manifest: dict[str, Any]
    sha256: str


@dataclass(frozen=True)
class RunResult:
    directory: Path
    summary: dict[str, Any]
    passed: bool


def load_set(path: Path) -> EvalSet:
    """Read an eval set, refusing it when it does not match its manifest."""
    if not path.is_file():
        raise EvalError(f"Eval set not found: {path}")
    manifest_file = manifest_path(path)
    if not manifest_file.is_file():
        raise EvalError(f"No manifest next to {path}; expected {manifest_file}.")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    digest = sha256_file(path)
    if manifest.get("file_sha256") != digest:
        raise EvalError(
            f"{path} does not match its manifest: the file hash is {digest[:12]}, the manifest "
            f"says {str(manifest.get('file_sha256'))[:12]}. Rebuild with rxdelta evalset build "
            "instead of editing the file by hand."
        )
    return EvalSet(path=path, items=read_items(path), manifest=manifest, sha256=digest)


def check_database(eval_set: EvalSet, db_path: Path) -> None:
    """The gold answers only hold for the database the set was built from."""
    if not db_path.is_file():
        raise EvalError(f"Database not found: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
            for table in eval_set.manifest.get("db_row_counts", {})
        }
    finally:
        conn.close()
    expected = eval_set.manifest.get("db_row_counts", {})
    if counts != expected:
        differing = ", ".join(
            f"{t} has {counts.get(t)} rows, the set expects {n}"
            for t, n in sorted(expected.items())
            if counts.get(t) != n
        )
        raise EvalError(
            f"{db_path} is not the database {eval_set.path.name} was built from: {differing}."
        )


def select(
    items: list[EvalItem], split: str, category: str | None, limit: int | None
) -> list[EvalItem]:
    chosen = [i for i in items if split == "all" or i.split == split]
    if category:
        if category not in CATEGORIES:
            raise EvalError(f"Unknown category {category!r}. Categories: {', '.join(CATEGORIES)}.")
        chosen = [i for i in chosen if i.category == category]
    if limit is not None:
        chosen = chosen[:limit]
    if not chosen:
        raise EvalError("No items match that split, category and limit.")
    return chosen


def _answer(runner: Runner, item: EvalItem) -> RunnerOutput:
    started = time.perf_counter()
    try:
        return runner.answer(item.question, item.context)
    except Exception as exc:  # noqa: BLE001
        return RunnerOutput(
            answer=NoDataAnswer(reason=NoDataReason.no_matching_changes),
            final_text="",
            latency_ms=round(1000 * (time.perf_counter() - started)),
            error=f"{type(exc).__name__}: {exc}",
        )


def percentile(values: list[float], share: float) -> float:
    """Nearest rank percentile; 0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(share * len(ordered)))
    return float(ordered[rank - 1])


def _spread(values: list[float]) -> dict[str, float]:
    total = float(sum(values))
    return {
        "total": round(total, 6),
        "mean": round(total / len(values), 6) if values else 0.0,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarize(
    items: list[EvalItem], scores: list[ItemScore], config: Config, gates: dict[str, float]
) -> dict[str, Any]:
    total = len(scores)
    correct = sum(s.correct for s in scores)
    f1s = [s.f1 for s in scores if s.f1 is not None]
    answerable = [s for s in scores if s.answerable]
    refusals = [s for s in scores if s.expected_kind == "refusal"]
    no_data = [s for s in scores if s.reason_match is not None]
    secondary = [s for s in scores if s.secondary_match is not None]
    by_category: dict[str, dict[str, Any]] = {}
    for category in CATEGORIES:
        members = [s for i, s in zip(items, scores, strict=True) if i.category == category]
        if not members:
            continue
        member_f1 = [s.f1 for s in members if s.f1 is not None]
        by_category[category] = {
            "items": len(members),
            "correct": sum(s.correct for s in members),
            "accuracy": _rate(sum(s.correct for s in members), len(members)),
            "mean_f1": round(sum(member_f1) / len(member_f1), 4) if member_f1 else None,
        }
    confusion: dict[str, dict[str, int]] = defaultdict(dict)
    for (expected, got), n in sorted(
        Counter((s.expected_kind, s.got_kind) for s in scores).items()
    ):
        confusion[expected][got] = n
    accuracy = _rate(correct, total) or 0.0
    missed_rate = _rate(sum(s.missed_refusal for s in refusals), len(refusals)) or 0.0
    reasons = []
    if accuracy < gates["min_accuracy"]:
        reasons.append(f"accuracy {accuracy:.4f} is below {gates['min_accuracy']:.4f}")
    if missed_rate > gates["max_missed_refusal_rate"]:
        reasons.append(
            f"missed refusal rate {missed_rate:.4f} is above {gates['max_missed_refusal_rate']:.4f}"
        )
    with_ungrounded = sum(1 for s in scores if s.ungrounded_numbers)
    return {
        "overall": {
            "items": total,
            "correct": correct,
            "accuracy": accuracy,
            "mean_f1_drug_list": round(sum(f1s) / len(f1s), 4) if f1s else None,
            "answerable": len(answerable),
            "false_refusals": sum(s.false_refusal for s in answerable),
            "false_refusal_rate": _rate(sum(s.false_refusal for s in answerable), len(answerable)),
            "out_of_scope": len(refusals),
            "missed_refusals": sum(s.missed_refusal for s in refusals),
            "missed_refusal_rate": missed_rate if refusals else None,
            "no_data_reason_match_rate": _rate(
                sum(bool(s.reason_match) for s in no_data), len(no_data)
            ),
            "secondary_match_rate": _rate(
                sum(bool(s.secondary_match) for s in secondary), len(secondary)
            ),
            "errors": sum(1 for s in scores if s.error),
        },
        "by_category": by_category,
        "kind_confusion": dict(confusion),
        "clarify_paths": dict(Counter(s.clarify_path for s in scores if s.clarify_path)),
        "over_cap_paths": dict(Counter(s.over_cap_path for s in scores if s.over_cap_path)),
        "tools": {
            "required_called_rate": _rate(sum(s.required_tools_called for s in scores), total),
            "items_missing_required": sum(1 for s in scores if not s.required_tools_called),
            "mean_calls": round(sum(s.tool_calls for s in scores) / total, 4) if total else 0.0,
            "invalid_args_calls": sum(s.invalid_args_calls for s in scores),
            "repeated_calls": sum(s.repeated_calls for s in scores),
        },
        "grounding": {
            "items_with_ungrounded": with_ungrounded,
            "share_with_ungrounded": _rate(with_ungrounded, total),
            "ungrounded_total": sum(len(s.ungrounded_numbers) for s in scores),
        },
        "cost_speed": {
            "latency_ms": _spread([float(s.latency_ms) for s in scores]),
            "input_tokens": _spread([float(s.input_tokens) for s in scores]),
            "output_tokens": _spread([float(s.output_tokens) for s in scores]),
            "cost_usd": _spread([s.cost_usd for s in scores]),
        },
        "gates": {**gates, "passed": not reasons, "reasons": reasons},
        "cost_tolerance": config.eval.cost_tolerance,
    }


def git_commit(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _run_dir(root: Path, runner: str, split: str, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    base = root / f"{stamp}_{runner}_{split}"
    candidate, n = base, 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{n}")
        n += 1
    candidate.mkdir(parents=True)
    return candidate


def run(
    *,
    set_path: Path,
    runner_name: str,
    split: str,
    db_path: Path,
    config: Config,
    out_root: Path = Path("runs"),
    category: str | None = None,
    limit: int | None = None,
    workers: int | None = None,
    min_accuracy: float | None = None,
    max_missed_refusal_rate: float | None = None,
    runner_factory: Callable[[list[EvalItem], ToolContext], Runner] | None = None,
) -> RunResult:
    eval_set = load_set(set_path)
    check_database(eval_set, db_path)
    chosen = select(eval_set.items, split, category, limit)
    ctx = ToolContext(db_path=db_path, config=config)
    try:
        runner = (
            runner_factory(eval_set.items, ctx)
            if runner_factory
            else make_runner(runner_name, eval_set.items, ctx)
        )
    except KeyError as exc:
        raise EvalError(str(exc.args[0])) from exc

    started = datetime.now(UTC)
    clock = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers or config.eval.workers) as pool:
        outputs = list(pool.map(lambda item: _answer(runner, item), chosen))
    scores = [score_item(i, o, config) for i, o in zip(chosen, outputs, strict=True)]
    gates = {
        "min_accuracy": config.eval.min_accuracy if min_accuracy is None else min_accuracy,
        "max_missed_refusal_rate": (
            config.eval.max_missed_refusal_rate
            if max_missed_refusal_rate is None
            else max_missed_refusal_rate
        ),
    }
    summary = {
        "runner": runner_name,
        "split": split,
        "category": category,
        "limit": limit,
        "eval_set": str(set_path),
        "eval_set_sha256": eval_set.sha256,
        "git_commit": git_commit(Path(__file__).resolve().parents[2]),
        "config_sha256": config.sha256,
        "database": str(db_path),
        "started_at": started.isoformat(timespec="seconds"),
        "duration_s": round(time.perf_counter() - clock, 3),
        **summarize(chosen, scores, config, gates),
    }
    directory = _run_dir(out_root, runner_name, split, started)
    with (directory / "results.jsonl").open("w", encoding="utf-8") as handle:
        for item, output, score in zip(chosen, outputs, scores, strict=True):
            record = {
                "id": item.id,
                "category": item.category,
                "split": item.split,
                "question": item.question,
                "expected": item.expected.model_dump(mode="json"),
                "alternative": item.alternative.model_dump(mode="json")
                if item.alternative
                else None,
                "required_tools": item.required_tools,
                "output": output.model_dump(mode="json"),
                "score": score.model_dump(mode="json"),
                "trace": output.trace,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (directory / "summary.md").write_text(report.summary_markdown(summary), encoding="utf-8")
    (directory / "report.html").write_text(
        report.render_html(summary, chosen, outputs, scores, config), encoding="utf-8"
    )
    return RunResult(directory=directory, summary=summary, passed=summary["gates"]["passed"])


@dataclass(frozen=True)
class Comparison:
    run_a: Path
    run_b: Path
    by_category: dict[str, dict[str, float | None]]
    overall: dict[str, float | None]
    right_to_wrong: list[str]
    wrong_to_right: list[str]


def _load_run(directory: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    summary_file = directory / "summary.json"
    results_file = directory / "results.jsonl"
    if not summary_file.is_file() or not results_file.is_file():
        raise EvalError(f"{directory} is not an eval run directory (no summary.json).")
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    outcomes = {}
    for line in results_file.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        outcomes[str(record["id"])] = bool(record["score"]["correct"])
    return summary, outcomes


def _delta(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round(b - a, 4)


def compare(run_a: Path, run_b: Path) -> Comparison:
    summary_a, outcomes_a = _load_run(run_a)
    summary_b, outcomes_b = _load_run(run_b)
    if summary_a["eval_set_sha256"] != summary_b["eval_set_sha256"]:
        raise EvalError(
            "These runs used different eval sets "
            f"({summary_a['eval_set_sha256'][:12]} and {summary_b['eval_set_sha256'][:12]}), "
            "so their scores are not comparable."
        )
    categories = sorted(
        set(summary_a["by_category"]) | set(summary_b["by_category"]),
        key=lambda c: CATEGORIES.index(c) if c in CATEGORIES else len(CATEGORIES),
    )
    by_category = {}
    for category in categories:
        a = summary_a["by_category"].get(category, {}).get("accuracy")
        b = summary_b["by_category"].get(category, {}).get("accuracy")
        by_category[category] = {"a": a, "b": b, "delta": _delta(a, b)}
    overall = {}
    for key in ("accuracy", "mean_f1_drug_list", "false_refusal_rate", "missed_refusal_rate"):
        a, b = summary_a["overall"].get(key), summary_b["overall"].get(key)
        overall[key] = _delta(a, b)
    shared = sorted(set(outcomes_a) & set(outcomes_b))
    return Comparison(
        run_a=run_a,
        run_b=run_b,
        by_category=by_category,
        overall=overall,
        right_to_wrong=[i for i in shared if outcomes_a[i] and not outcomes_b[i]],
        wrong_to_right=[i for i in shared if not outcomes_a[i] and outcomes_b[i]],
    )
