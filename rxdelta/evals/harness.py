"""`rxdelta eval run` and `rxdelta eval compare`."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rxdelta.agent.budget import Ledger, SpendGuard
from rxdelta.agent.loop import ACCOUNT_ERROR, API_ERROR
from rxdelta.ask.answer import NoDataAnswer, NoDataReason, Runner, RunnerOutput
from rxdelta.config import CATEGORIES, Config
from rxdelta.evals import report
from rxdelta.evals.agent_metrics import agent_summary, percentile, rate, repeat_summary
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


def _round_robin(items: list[EvalItem]) -> list[EvalItem]:
    """Items reordered one category at a time, so any prefix is spread over categories."""
    queues = {c: [i for i in items if i.category == c] for c in CATEGORIES}
    ordered: list[EvalItem] = []
    while any(queues.values()):
        for category in CATEGORIES:
            if queues[category]:
                ordered.append(queues[category].pop(0))
    return ordered


def select(
    items: list[EvalItem],
    split: str,
    category: str | None,
    limit: int | None,
    stratified: bool = False,
) -> list[EvalItem]:
    chosen = [i for i in items if split == "all" or i.split == split]
    if category:
        if category not in CATEGORIES:
            raise EvalError(f"Unknown category {category!r}. Categories: {', '.join(CATEGORIES)}.")
        chosen = [i for i in chosen if i.category == category]
    if limit is not None:
        if stratified:
            keep = {i.id for i in _round_robin(chosen)[:limit]}
            chosen = [i for i in chosen if i.id in keep]
        else:
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


def _spread(values: list[float]) -> dict[str, float]:
    total = float(sum(values))
    return {
        "total": round(total, 6),
        "mean": round(total / len(values), 6) if values else 0.0,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
    }


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
            "accuracy": rate(sum(s.correct for s in members), len(members)),
            "mean_f1": round(sum(member_f1) / len(member_f1), 4) if member_f1 else None,
        }
    confusion: dict[str, dict[str, int]] = defaultdict(dict)
    for (expected, got), n in sorted(
        Counter((s.expected_kind, s.got_kind) for s in scores).items()
    ):
        confusion[expected][got] = n
    accuracy = rate(correct, total) or 0.0
    missed_rate = rate(sum(s.missed_refusal for s in refusals), len(refusals)) or 0.0
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
            "false_refusal_rate": rate(sum(s.false_refusal for s in answerable), len(answerable)),
            "out_of_scope": len(refusals),
            "missed_refusals": sum(s.missed_refusal for s in refusals),
            "missed_refusal_rate": missed_rate if refusals else None,
            "no_data_reason_match_rate": rate(
                sum(bool(s.reason_match) for s in no_data), len(no_data)
            ),
            "secondary_match_rate": rate(
                sum(bool(s.secondary_match) for s in secondary), len(secondary)
            ),
            "errors": sum(1 for s in scores if s.error),
        },
        "by_category": by_category,
        "kind_confusion": dict(confusion),
        "clarify_paths": dict(Counter(s.clarify_path for s in scores if s.clarify_path)),
        "over_cap_paths": dict(Counter(s.over_cap_path for s in scores if s.over_cap_path)),
        "tools": {
            "required_called_rate": rate(sum(s.required_tools_called for s in scores), total),
            "items_missing_required": sum(1 for s in scores if not s.required_tools_called),
            "mean_calls": round(sum(s.tool_calls for s in scores) / total, 4) if total else 0.0,
            "invalid_args_calls": sum(s.invalid_args_calls for s in scores),
            "repeated_calls": sum(s.repeated_calls for s in scores),
        },
        "grounding": {
            "items_with_ungrounded": with_ungrounded,
            "share_with_ungrounded": rate(with_ungrounded, total),
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


@dataclass(frozen=True)
class RunOptions:
    """What a paid model run adds to a plain run."""

    repeats: int = 1
    resume: Path | None = None
    settings: dict[str, Any] | None = None
    ledger: Ledger | None = None
    budget_cap: float | None = None
    model: str | None = None


def _record(item: EvalItem, repeat: int, output: RunnerOutput, score: ItemScore) -> dict[str, Any]:
    return {
        "id": item.id,
        "repeat": repeat,
        "category": item.category,
        "split": item.split,
        "question": item.question,
        "expected": item.expected.model_dump(mode="json"),
        "alternative": item.alternative.model_dump(mode="json") if item.alternative else None,
        "required_tools": item.required_tools,
        "output": output.model_dump(mode="json"),
        "score": score.model_dump(mode="json"),
        "trace": output.trace,
    }


def _read_records(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    if not path.is_file():
        return {}
    records = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[(str(record["id"]), int(record.get("repeat", 1)))] = record
    return records


def _says_nothing_about_the_runner(record: dict[str, Any]) -> bool:
    """An answer the API never let the runner give; a resume asks again."""
    error = record["output"].get("error") or ""
    return error.startswith((API_ERROR, ACCOUNT_ERROR))


def _restore(record: dict[str, Any]) -> tuple[RunnerOutput, ItemScore]:
    output = RunnerOutput.model_validate(record["output"])
    output.trace = record.get("trace")
    return output, ItemScore.model_validate(record["score"])


def _stop_note(stopped: bool, account: list[str], kept: int, wanted: int) -> str | None:
    if not stopped:
        return None
    if account:
        return (
            f"the API refused the account ({account[0][:160]}): {kept} of {wanted} answers "
            "finished; fix the account and rerun with --resume"
        )
    return f"budget reached: {kept} of {wanted} answers finished"


def scored_metrics(
    items: list[EvalItem],
    repeats: list[int],
    outputs: list[RunnerOutput],
    scores: list[ItemScore],
    config: Config,
    gates: dict[str, float],
) -> dict[str, Any]:
    """Every summary number that follows from the answers and their scores."""
    metrics: dict[str, Any] = {
        "repeats": repeat_summary([i.id for i in items], repeats, scores),
        "run_cost_usd": round(sum(o.cost_usd for o in outputs), 6),
        **summarize(items, scores, config, gates),
    }
    agent = agent_summary([i.question for i in items], outputs, scores)
    if agent is not None:
        metrics["agent"] = agent
    return metrics


def rescore(directory: Path, config: Config) -> tuple[dict[str, Any], dict[str, Any]]:
    """Score a saved run's answers again with the current scorer, with no runner
    and no API call. Writes summary.rescored.json; summary.json is left as it was."""
    summary_file = directory / "summary.json"
    if not summary_file.is_file():
        raise EvalError(f"{directory} is not an eval run directory (no summary.json).")
    old = json.loads(summary_file.read_text(encoding="utf-8"))
    eval_set = load_set(Path(old["eval_set"]))
    if eval_set.sha256 != old["eval_set_sha256"]:
        raise EvalError(f"{old['eval_set']} has changed since {directory} was scored.")
    by_id = {i.id: i for i in eval_set.items}
    records = list(_read_records(directory / "results.jsonl").values())
    items = [by_id[str(r["id"])] for r in records]
    outputs = [_restore(r)[0] for r in records]
    scores = [score_item(i, o, config) for i, o in zip(items, outputs, strict=True)]
    gates = {k: old["gates"][k] for k in ("min_accuracy", "max_missed_refusal_rate")}
    new = {
        **old,
        **scored_metrics(
            items, [int(r.get("repeat", 1)) for r in records], outputs, scores, config, gates
        ),
        "rescored_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rescored_commit": git_commit(Path(__file__).resolve().parents[2]),
    }
    (directory / "summary.rescored.json").write_text(
        json.dumps(new, indent=2) + "\n", encoding="utf-8"
    )
    return old, new


def planned_answers(
    set_path: Path,
    split: str,
    category: str | None,
    limit: int | None,
    stratified: bool,
    repeats: int,
    resume: Path | None,
) -> int:
    """Answers a run will ask for, less those a resumed run already has."""
    chosen = select(read_items(set_path), split, category, limit, stratified)
    done = _read_records(resume / "results.jsonl") if resume is not None else {}
    done = {k: v for k, v in done.items() if not _says_nothing_about_the_runner(v)}
    wanted = {(i.id, r) for r in range(1, repeats + 1) for i in chosen}
    return len(wanted - set(done))


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
    stratified: bool = False,
    options: RunOptions | None = None,
) -> RunResult:
    opts = options or RunOptions()
    eval_set = load_set(set_path)
    check_database(eval_set, db_path)
    chosen = select(eval_set.items, split, category, limit, stratified)
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
    if opts.resume is not None:
        directory = opts.resume
        if not directory.is_dir():
            raise EvalError(f"Cannot resume: {directory} is not a folder.")
    else:
        directory = _run_dir(out_root, runner_name, split, started)
    spec = {
        "runner": runner_name,
        "split": split,
        "category": category,
        "limit": limit,
        "stratified": stratified,
        "repeats": opts.repeats,
        "settings": opts.settings,
        "eval_set_sha256": eval_set.sha256,
    }
    spec_file = directory / "run.json"
    if opts.resume is not None:
        stored = json.loads(spec_file.read_text(encoding="utf-8")) if spec_file.is_file() else {}
        differing = sorted(k for k in spec if stored.get(k) != spec[k])
        if differing:
            raise EvalError(
                f"Cannot resume {directory}: these settings differ from the original run: "
                f"{', '.join(differing)}. Pass the same flags as the original run."
            )
    else:
        spec_file.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    results_file = directory / "results.jsonl"
    done = _read_records(results_file)
    if opts.resume is not None:
        done = {k: v for k, v in done.items() if not _says_nothing_about_the_runner(v)}
        results_file.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in done.values()),
            encoding="utf-8",
        )
    pairs = [(item, r) for r in range(1, opts.repeats + 1) for item in chosen]
    todo = [(item, r) for item, r in pairs if (item.id, r) not in done]
    guard = SpendGuard(opts.budget_cap) if opts.budget_cap is not None else None
    lock = threading.Lock()

    account_failure: list[str] = []

    def work(pair: tuple[EvalItem, int]) -> dict[str, Any] | None:
        item, repeat = pair
        if account_failure or (guard is not None and guard.exhausted):
            return None
        output = _answer(runner, item)
        if (output.error or "").startswith(ACCOUNT_ERROR):
            account_failure.append(output.error or "")
            return None
        if guard is not None:
            guard.add(output.cost_usd)
        record = _record(item, repeat, output, score_item(item, output, config))
        with lock, results_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    with ThreadPoolExecutor(max_workers=workers or config.eval.workers) as pool:
        fresh = [r for r in pool.map(work, todo) if r is not None]
    new_cost = sum(float(r["output"]["cost_usd"]) for r in fresh)
    if opts.ledger is not None and fresh:
        opts.ledger.append(
            run_dir=directory,
            model=opts.model or runner_name,
            items=len({r["id"] for r in fresh}),
            answers=len(fresh),
            cost=new_cost,
        )

    records = _read_records(results_file)
    kept = [(item, r) for item, r in pairs if (item.id, r) in records]
    ordered = [records[(item.id, r)] for item, r in kept]
    results_file.write_text(
        "".join(json.dumps(rec, ensure_ascii=False) + "\n" for rec in ordered), encoding="utf-8"
    )
    restored = [_restore(rec) for rec in ordered]
    items = [item for item, _ in kept]
    outputs = [o for o, _ in restored]
    scores = [sc for _, sc in restored]
    if not items:
        raise EvalError("No item was answered before the budget ran out.")
    gates = {
        "min_accuracy": config.eval.min_accuracy if min_accuracy is None else min_accuracy,
        "max_missed_refusal_rate": (
            config.eval.max_missed_refusal_rate
            if max_missed_refusal_rate is None
            else max_missed_refusal_rate
        ),
    }
    stopped = len(kept) < len(pairs)
    summary = {
        "runner": runner_name,
        "split": split,
        "category": category,
        "limit": limit,
        "stratified": stratified,
        "eval_set": str(set_path),
        "eval_set_sha256": eval_set.sha256,
        "git_commit": git_commit(Path(__file__).resolve().parents[2]),
        "config_sha256": config.sha256,
        "database": str(db_path),
        "started_at": started.isoformat(timespec="seconds"),
        "duration_s": round(time.perf_counter() - clock, 3),
        "settings": opts.settings,
        "stopped_early": _stop_note(stopped, account_failure, len(kept), len(pairs)),
        "this_session_cost_usd": round(new_cost, 6),
        **scored_metrics(items, [r for _, r in kept], outputs, scores, config, gates),
    }
    if opts.ledger is not None:
        summary["ledger_total_usd"] = opts.ledger.total()
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (directory / "summary.md").write_text(report.summary_markdown(summary), encoding="utf-8")
    (directory / "report.html").write_text(
        report.render_html(summary, items, outputs, scores, config), encoding="utf-8"
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
    settings_differ: dict[str, tuple[Any, Any]]


def _load_run(directory: Path) -> tuple[dict[str, Any], dict[str, bool]]:
    """The summary and, per item, whether it was right on most of its repeats."""
    summary_file = directory / "summary.json"
    results_file = directory / "results.jsonl"
    if not summary_file.is_file() or not results_file.is_file():
        raise EvalError(f"{directory} is not an eval run directory (no summary.json).")
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    votes: dict[str, list[bool]] = defaultdict(list)
    for line in results_file.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        votes[str(record["id"])].append(bool(record["score"]["correct"]))
    outcomes = {i: 2 * sum(v) > len(v) for i, v in votes.items()}
    return summary, outcomes


def outcomes(directory: Path) -> dict[str, bool]:
    """Per item, whether it was right on most of its repeats."""
    return _load_run(directory)[1]


_SETTING_KEYS = ("runner", "split", "category", "limit", "stratified")


def settings_view(summary: dict[str, Any]) -> dict[str, Any]:
    """The run settings two runs are compared on, flattened to one level."""
    view = {k: summary.get(k) for k in _SETTING_KEYS}
    view["repeats"] = (summary.get("repeats") or {}).get("repeats", 1)
    for key, value in (summary.get("settings") or {}).items():
        if isinstance(value, dict):
            view.update({f"{key}.{k}": v for k, v in value.items()})
        else:
            view[key] = value
    return view


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
    view_a, view_b = settings_view(summary_a), settings_view(summary_b)
    return Comparison(
        run_a=run_a,
        run_b=run_b,
        by_category=by_category,
        overall=overall,
        right_to_wrong=[i for i in shared if outcomes_a[i] and not outcomes_b[i]],
        wrong_to_right=[i for i in shared if not outcomes_a[i] and outcomes_b[i]],
        settings_differ={
            k: (view_a.get(k), view_b.get(k))
            for k in sorted(set(view_a) | set(view_b))
            if view_a.get(k) != view_b.get(k)
        },
    )
