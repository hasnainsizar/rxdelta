"""Command line surface. Thin: parse options, call a layer, render the result."""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from functools import wraps
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from rxdelta import __version__
from rxdelta.agent import ask as ask_support
from rxdelta.agent import trace as agent_trace
from rxdelta.agent.budget import Ledger
from rxdelta.agent.budget import check as check_budget
from rxdelta.agent.budget import estimate as estimate_spend
from rxdelta.agent.client import ModelClient, ReplayClient, cost_usd
from rxdelta.agent.loop import AgentRunner, AgentSettings, default_client
from rxdelta.ask.answer import QuestionContext
from rxdelta.config import CATEGORIES, Config, get_config
from rxdelta.db import DEFAULT_DB_PATH, connect, queries
from rxdelta.diff import impact
from rxdelta.diff.cost_direction import CostDirection
from rxdelta.diff.engine import DiffResult, diff_snapshots
from rxdelta.diff.impact import ChangeGroup
from rxdelta.evals import builder as evalset_builder
from rxdelta.evals import harness as eval_harness
from rxdelta.evals import paraphrase
from rxdelta.ingest import download as cms_download
from rxdelta.ingest.loader import load_month
from rxdelta.limitations import ESTIMATE_NOTE, LIMITATIONS, LIMITATIONS_TITLE, OPEN_ENDED_NOTE
from rxdelta.names import fetch as names_fetch_module
from rxdelta.names import fixture as names_fixture
from rxdelta.names import rrf as names_rrf
from rxdelta.names import rxnav
from rxdelta.names import store as names_store
from rxdelta.names.cache import load_cache, read_csv, write_csv
from rxdelta.report import format as fmt
from rxdelta.report import html
from rxdelta.tools import registry as tool_registry
from rxdelta.tools.context import ToolContext
from rxdelta.types import ChangeType, RxdeltaError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Track what CMS Part D formulary changes do to a member's out of pocket cost.",
)
console = Console()
err_console = Console(stderr=True)

F = TypeVar("F", bound=Callable[..., Any])

DbOption = Annotated[Path, typer.Option("--db", help="SQLite database file.")]
ConfigOption = Annotated[
    Path | None, typer.Option("--config", help="Path to rxdelta.toml. Defaults to config/.")
]
JsonOption = Annotated[
    Path | None, typer.Option("--json", help="Write machine readable JSON to this path.")
]
CsvOption = Annotated[
    Path | None, typer.Option("--csv", help="Write a flat CSV table to this path.")
]


def handle_errors(func: F) -> F:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except RxdeltaError as exc:
            err_console.print(f"[bold red]error[/bold red] {exc}")
            raise typer.Exit(code=1) from exc

    return wrapper  # type: ignore[return-value]


def _open(db: Path, config_path: Path | None) -> tuple[sqlite3.Connection, Config]:
    """Open the database and fold in the committed drug name cache.

    Names are reference data, so loading them here means every command has them
    without a refresh and without touching the network.
    """
    conn = connect(db)
    config = get_config(config_path)
    load_cache(conn, config.resolve(config.names.cache_path))
    return conn, config


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _write_csv(path: Path, header: list[str], rows: Iterable[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _print_limitations() -> None:
    console.print()
    console.print(f"[bold]{LIMITATIONS_TITLE}[/bold]")
    for line in LIMITATIONS:
        console.print(f"  - {line}", highlight=False)
    console.print()
    console.print(f"[dim]{ESTIMATE_NOTE}[/dim]", highlight=False)
    console.print(f"[dim]{OPEN_ENDED_NOTE}[/dim]", highlight=False)


def _print_severity_distribution(groups: list[ChangeGroup]) -> dict[str, Any]:
    """Show whether the score actually separates the changes it ranks."""
    console.print()
    if not groups:
        console.print("No change groups, so there is no severity distribution to show.")
        return {"groups": 0, "distinct": 0, "largest_tie": 0, "buckets": {}, "top_ties": []}

    scores = [g.severity for g in groups]
    counts = Counter(scores)
    buckets: Counter[int] = Counter()
    for score in scores:
        buckets[min(int(score // 10) * 10, 90)] += 1
    widest = max(buckets.values())

    table = Table(title="Severity distribution", title_justify="left")
    table.add_column("Bucket")
    table.add_column("Groups", justify="right")
    table.add_column("Share", justify="right")
    table.add_column("")
    for low in range(0, 100, 10):
        n = buckets.get(low, 0)
        bar = "#" * round(24 * n / widest) if n else ""
        table.add_row(
            f"{low:>2} to {low + 9:<2}",
            f"{n:,}",
            f"{100.0 * n / len(scores):5.1f}%",
            bar,
        )
    console.print(table)

    largest_score, largest_n = counts.most_common(1)[0]
    console.print(
        f"{len(counts):,} distinct severity value(s) across {len(groups):,} change group(s). "
        f"Largest tie: {largest_n:,} group(s) share {largest_score:.2f}.",
        highlight=False,
    )
    ties = [(score, n) for score, n in counts.most_common(5) if n > 1]
    if ties:
        console.print("Largest tie groups:", highlight=False)
        for score, n in ties:
            console.print(f"  {score:6.2f}  {n:,} group(s)", highlight=False)
    if len(counts) < max(4, len(groups) // 10):
        console.print(
            "[yellow]The score is not discriminating well on this comparison.[/yellow] "
            "Its inputs are the cost range, direction, affected plan count and whether a "
            "restriction was added; if those barely vary, neither will the score.",
            highlight=False,
        )
    return {
        "groups": len(groups),
        "distinct": len(counts),
        "largest_tie": largest_n,
        "largest_tie_score": largest_score,
        "buckets": {f"{low}-{low + 9}": buckets.get(low, 0) for low in range(0, 100, 10)},
        "top_ties": [{"severity": s_, "groups": n} for s_, n in counts.most_common(5)],
    }


def _group_payload(config: Config, group: ChangeGroup) -> dict[str, Any]:
    return {
        "ndc_11": group.ndc_11,
        "ndc_display": fmt.ndc_display(group.ndc_11),
        "rxcui": group.rxcui,
        "change_types": [t.value for t in group.change_types],
        "tier_before": group.tier_before,
        "tier_after": group.tier_after,
        "tier_before_label": config.tier_label(group.tier_before),
        "tier_after_label": config.tier_label(group.tier_after),
        "cost_direction": group.cost_direction.value if group.cost_direction else None,
        "plan_count": group.plan_count,
        "plans": [str(p) for p in group.plans],
        "severity": group.severity,
        "impact": {
            "low": group.impact.low,
            "high": group.impact.high,
            "direction": group.impact.direction,
            "open_ended": group.impact.open_ended,
            "priced": group.impact.priced,
            "display": fmt.impact_range(group.impact),
            "basis": group.impact.basis,
        },
    }


def _diff_payload(config: Config, result: DiffResult, groups: list[ChangeGroup]) -> dict[str, Any]:
    return {
        "month_from": result.month_from,
        "month_to": result.month_to,
        "plan_filter": result.plan_filter,
        "totals": {
            "changes": len(result.changes),
            "change_groups": len(groups),
            "plans_compared": result.plans_compared,
            "plans_only_in_from": [str(p) for p in result.plans_removed],
            "plans_only_in_to": [str(p) for p in result.plans_added],
            "affected_plans": result.affected_plans,
            "affected_drugs": result.affected_drugs,
            "drugs_in_from": result.drugs_from,
            "drugs_in_to": result.drugs_to,
        },
        "counts_by_change_type": {t.value: n for t, n in result.counts_by_type().items()},
        "tier_moves_by_cost_direction": _tier_moves_by_cost(groups),
        "groups": [_group_payload(config, g) for g in groups],
        "limitations": list(LIMITATIONS),
        "estimate_note": ESTIMATE_NOTE,
        "open_ended_note": OPEN_ENDED_NOTE,
    }


def _tier_moves_by_cost(groups: list[ChangeGroup]) -> dict[str, dict[str, int]]:
    """Drug and plan pairs per tier move, split by what the move did to cost."""
    out: dict[str, dict[str, int]] = {}
    for group in groups:
        if group.cost_direction is None:
            continue
        for change_type in group.change_types:
            if change_type in (ChangeType.TIER_UP, ChangeType.TIER_DOWN):
                row = out.setdefault(change_type.value, {d.value: 0 for d in CostDirection})
                row[group.cost_direction.value] += group.plan_count
    return out


_GROUP_CSV_HEADER = [
    "ndc_11",
    "rxcui",
    "change_types",
    "tier_before",
    "tier_after",
    "plan_count",
    "impact_low",
    "impact_high",
    "impact_open_ended",
    "impact_priced",
    "severity",
    "cost_direction",
]


def _group_csv_rows(groups: list[ChangeGroup]) -> list[list[Any]]:
    return [
        [
            g.ndc_11,
            g.rxcui,
            ";".join(t.value for t in g.change_types),
            g.tier_before if g.tier_before is not None else "",
            g.tier_after if g.tier_after is not None else "",
            g.plan_count,
            g.impact.low,
            g.impact.high,
            int(g.impact.open_ended),
            int(g.impact.priced),
            g.severity,
            g.cost_direction.value if g.cost_direction else "",
        ]
        for g in groups
    ]


@app.command()
@handle_errors
def load(
    month: Annotated[str, typer.Option("--month", help="Snapshot month, YYYY-MM.")],
    directory: Annotated[
        Path, typer.Option("--dir", help="Root directory holding one folder per month.")
    ] = Path("data"),
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
    json_out: JsonOption = None,
) -> None:
    """Ingest one monthly snapshot. Running the same month twice is idempotent."""
    conn, config = _open(db, config_path)
    summary = load_month(conn, config, month, directory)

    table = Table(title=f"Loaded {month} from {summary.directory}", title_justify="left")
    table.add_column("File type")
    table.add_column("File")
    table.add_column("Rows", justify="right")
    table.add_column("Collapsed", justify="right")
    table.add_column("Rejected", justify="right")
    table.add_column("sha256")
    for file_result in summary.files:
        table.add_row(
            file_result.file_type,
            file_result.file_name,
            f"{file_result.row_count:,}",
            f"{file_result.collapsed_row_count:,}",
            f"{file_result.rejected_row_count:,}",
            file_result.sha256[:12],
        )
    console.print(table)
    if summary.total_collapsed:
        console.print(
            f"{summary.total_collapsed:,} source row(s) collapsed as identical repeats of a key "
            "already seen. The CMS plan information file carries one row per plan per county.",
            highlight=False,
        )

    if summary.total_rejected:
        console.print(
            f"[yellow]{summary.total_rejected:,} row(s) rejected.[/yellow] Reasons, with counts:"
        )
        for reason, count in sorted(
            summary.rejected_reasons.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            console.print(f"  {count:,}  {reason}", highlight=False)
    else:
        console.print("No rows rejected.")

    if json_out:
        _write_json(
            json_out,
            {
                "month": summary.month,
                "directory": str(summary.directory),
                "files": [
                    {
                        "file_type": f.file_type,
                        "file_name": f.file_name,
                        "sha256": f.sha256,
                        "row_count": f.row_count,
                        "collapsed_row_count": f.collapsed_row_count,
                        "rejected_row_count": f.rejected_row_count,
                    }
                    for f in summary.files
                ],
                "total_rows": summary.total_rows,
                "total_collapsed": summary.total_collapsed,
                "total_rejected": summary.total_rejected,
                "rejected_reasons": summary.rejected_reasons,
            },
        )
        console.print(f"Wrote {json_out}")


@app.command("diff")
@handle_errors
def diff_command(
    month_from: Annotated[str, typer.Option("--from", help="Baseline month, YYYY-MM.")],
    month_to: Annotated[str, typer.Option("--to", help="Comparison month, YYYY-MM.")],
    plan: Annotated[
        str | None, typer.Option("--plan", help="Limit to one contract id, for example H1234.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Rows to print in the terminal.")] = 25,
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
    json_out: JsonOption = None,
    csv_out: CsvOption = None,
) -> None:
    """Classify every drug and plan change between two snapshots."""
    conn, config = _open(db, config_path)
    result = diff_snapshots(conn, month_from, month_to, plan_filter=plan)
    groups = impact.build_groups(conn, config, result)

    console.print(
        f"[bold]{month_from} to {month_to}[/bold]: {len(result.changes):,} drug and plan "
        f"change(s) across {result.plans_compared:,} plan(s), "
        f"{len(groups):,} distinct change group(s)."
    )
    if not groups:
        console.print("No changes found between these two snapshots. There is nothing to show.")
    else:
        table = Table(title=f"Top {min(limit, len(groups))} by severity", title_justify="left")
        table.add_column("Drug (NDC)")
        table.add_column("Change")
        table.add_column("Tier")
        table.add_column("Plans", justify="right")
        table.add_column("Est. monthly change", justify="right")
        table.add_column("Severity", justify="right")
        for group in groups[:limit]:
            table.add_row(
                fmt.ndc_display(group.ndc_11),
                fmt.change_label(group),
                fmt.tier_move(config, group),
                f"{group.plan_count:,}",
                fmt.impact_range(group.impact),
                f"{group.severity:.1f}",
            )
        console.print(table)
        if len(groups) > limit:
            console.print(f"[dim]{len(groups) - limit:,} more group(s) not shown.[/dim]")

    _print_limitations()

    if json_out:
        _write_json(json_out, _diff_payload(config, result, groups))
        console.print(f"Wrote {json_out}")
    if csv_out:
        _write_csv(csv_out, _GROUP_CSV_HEADER, _group_csv_rows(groups))
        console.print(f"Wrote {csv_out}")


@app.command()
@handle_errors
def summary(
    month_from: Annotated[str, typer.Option("--from", help="Baseline month, YYYY-MM.")],
    month_to: Annotated[str, typer.Option("--to", help="Comparison month, YYYY-MM.")],
    plan: Annotated[str | None, typer.Option("--plan", help="Limit to one contract id.")] = None,
    top_plans: Annotated[int, typer.Option("--top-plans", help="Plans to list.")] = 15,
    severity_distribution: Annotated[
        bool,
        typer.Option(
            "--severity-distribution",
            help="Show how well the severity score discriminates: distinct values, "
            "a 10 point histogram, and the largest tie group.",
        ),
    ] = False,
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
    json_out: JsonOption = None,
    csv_out: CsvOption = None,
) -> None:
    """Roll the diff up by plan and by change type. Run this first."""
    conn, config = _open(db, config_path)
    result = diff_snapshots(conn, month_from, month_to, plan_filter=plan)

    console.print(
        f"[bold]{month_from} to {month_to}[/bold]  "
        f"{result.plans_compared:,} plan(s) compared, "
        f"{result.drugs_from:,} drug(s) in {month_from}, {result.drugs_to:,} in {month_to}."
    )
    if result.plans_added or result.plans_removed:
        console.print(
            f"[dim]{len(result.plans_added):,} plan(s) only in {month_to}, "
            f"{len(result.plans_removed):,} only in {month_from}. Not compared.[/dim]"
        )

    by_type = result.counts_by_type()
    by_plan = result.counts_by_plan()

    if not result.changes:
        console.print()
        console.print("No changes found between these two snapshots. There is nothing to roll up.")
    else:
        type_table = Table(title="By change type", title_justify="left")
        type_table.add_column("Change type")
        type_table.add_column("Drug and plan pairs", justify="right")
        for change_type, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0].value)):
            type_table.add_row(change_type.label, f"{count:,}")
        console.print(type_table)

        plan_table = Table(
            title=f"By plan (top {min(top_plans, len(by_plan))} of {len(by_plan):,})",
            title_justify="left",
        )
        plan_table.add_column("Plan")
        plan_table.add_column("Changed drugs", justify="right")
        for plan_key, count in list(by_plan.items())[:top_plans]:
            plan_table.add_row(str(plan_key), f"{count:,}")
        console.print(plan_table)

    distribution: dict[str, Any] | None = None
    if severity_distribution:
        groups = impact.build_groups(conn, config, result)
        distribution = _print_severity_distribution(groups)

    _print_limitations()

    payload = {
        "month_from": month_from,
        "month_to": month_to,
        "plan_filter": plan,
        "plans_compared": result.plans_compared,
        "total_changes": len(result.changes),
        "by_change_type": {t.value: n for t, n in by_type.items()},
        "by_plan": {str(k): v for k, v in by_plan.items()},
        "limitations": list(LIMITATIONS),
    }
    if distribution is not None:
        payload["severity_distribution"] = distribution
    if json_out:
        _write_json(json_out, payload)
        console.print(f"Wrote {json_out}")
    if csv_out:
        _write_csv(
            csv_out,
            ["scope", "key", "changed_items"],
            [["change_type", t.value, n] for t, n in by_type.items()]
            + [["plan", str(k), v] for k, v in by_plan.items()],
        )
        console.print(f"Wrote {csv_out}")


@app.command()
@handle_errors
def report(
    month_from: Annotated[str, typer.Option("--from", help="Baseline month, YYYY-MM.")],
    month_to: Annotated[str, typer.Option("--to", help="Comparison month, YYYY-MM.")],
    out: Annotated[Path, typer.Option("--out", help="Output HTML file.")],
    plan: Annotated[str | None, typer.Option("--plan", help="Limit to one contract id.")] = None,
    frozen_timestamp: Annotated[
        bool,
        typer.Option(
            "--frozen-timestamp",
            help="Stamp the report with the comparison months instead of the wall clock, "
            "so a committed copy does not churn on every regeneration.",
        ),
    ] = False,
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
    json_out: JsonOption = None,
) -> None:
    """Write a self contained HTML digest of the most impactful changes."""
    conn, config = _open(db, config_path)
    result = diff_snapshots(conn, month_from, month_to, plan_filter=plan)
    groups = impact.build_groups(conn, config, result)
    stamp = f"{month_from} to {month_to} comparison" if frozen_timestamp else None
    written = html.write(config, result, groups, out, generated_at=stamp)

    console.print(f"Wrote {written} covering {len(groups):,} change group(s).")
    notice = html.low_result_notice(config, result, len(groups))
    if notice:
        console.print(
            f"[yellow]Only {notice.count} change group(s), below the reporting floor of "
            f"{notice.floor}.[/yellow] The report says so on its first screen."
        )
    if json_out:
        _write_json(json_out, _diff_payload(config, result, groups))
        console.print(f"Wrote {json_out}")


@app.command()
@handle_errors
def status(
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
    json_out: JsonOption = None,
) -> None:
    """Show what is loaded, row counts per table, and the ingest log."""
    conn, config = _open(db, config_path)
    months = queries.loaded_months(conn)
    counts = queries.row_counts(conn)
    log = queries.ingest_log(conn)
    reasons = queries.rejection_reasons(conn)

    console.print(f"Database {db}, config {config.path}")
    if not months:
        console.print("No months loaded. Run: rxdelta load --month YYYY-MM --dir data")
        if json_out:
            _write_json(json_out, {"months": [], "row_counts": {}, "ingest_log": []})
        return

    count_table = Table(title="Rows per table", title_justify="left")
    count_table.add_column("Month")
    for table_name in (*queries.FACT_TABLES, "rejected_rows"):
        count_table.add_column(table_name, justify="right")
    for month in months:
        count_table.add_row(
            month,
            *[
                f"{counts[table_name].get(month, 0):,}"
                for table_name in (*queries.FACT_TABLES, "rejected_rows")
            ],
        )
    console.print(count_table)

    log_table = Table(title="Ingest log", title_justify="left")
    log_table.add_column("Month")
    log_table.add_column("File")
    log_table.add_column("Rows", justify="right")
    log_table.add_column("Rejected", justify="right")
    log_table.add_column("sha256")
    log_table.add_column("Loaded at")
    for entry in log:
        log_table.add_row(
            entry.snapshot_month,
            entry.file_name,
            f"{entry.row_count:,}",
            f"{entry.rejected_row_count:,}",
            entry.sha256[:12],
            entry.loaded_at,
        )
    console.print(log_table)

    if reasons:
        reason_table = Table(title="Rejected rows by reason", title_justify="left")
        reason_table.add_column("Month")
        reason_table.add_column("Reason")
        reason_table.add_column("Rows", justify="right")
        for month, reason, count in reasons:
            reason_table.add_row(month, reason, f"{count:,}")
        console.print(reason_table)
    else:
        console.print("No rejected rows in any loaded month.")

    if json_out:
        _write_json(
            json_out,
            {
                "database": str(db),
                "config": str(config.path),
                "months": months,
                "row_counts": counts,
                "ingest_log": [
                    {
                        "snapshot_month": e.snapshot_month,
                        "file_name": e.file_name,
                        "sha256": e.sha256,
                        "row_count": e.row_count,
                        "rejected_row_count": e.rejected_row_count,
                        "loaded_at": e.loaded_at,
                    }
                    for e in log
                ],
                "rejected_by_reason": [
                    {"snapshot_month": m, "reason": r, "count": n} for m, r, n in reasons
                ],
            },
        )
        console.print(f"Wrote {json_out}")


names_app = typer.Typer(
    no_args_is_help=True, help="Manage drug names: fetch, load and check coverage."
)
app.add_typer(names_app, name="names")


@names_app.command("refresh")
@handle_errors
def names_refresh(
    month: Annotated[
        str | None,
        typer.Option("--month", help="Only resolve RXCUIs in this snapshot month."),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Refetch every RXCUI, including cached ones.")
    ] = False,
    workers: Annotated[int, typer.Option("--workers", help="Concurrent requests.")] = 4,
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
    json_out: JsonOption = None,
) -> None:
    """Resolve drug names from RxNav and update the committed cache.

    This is the only command that uses the network. Everything else reads the
    cache, so a checkout with no connectivity still renders names.
    """
    conn, config = _open(db, config_path)
    cache_path = config.resolve(config.names.cache_path)

    sql = "SELECT DISTINCT rxcui FROM formulary WHERE rxcui != ''"
    params: list[str] = []
    if month:
        sql += " AND snapshot_month = ?"
        params.append(month)
    rxcuis = [str(r["rxcui"]) for r in conn.execute(sql, params).fetchall()]
    if not rxcuis:
        console.print(
            "No RXCUIs found in the database. Load a snapshot first: rxdelta load --month YYYY-MM"
        )
        return

    cached = read_csv(cache_path)
    pending = len(rxcuis) if force else len([r for r in rxcuis if r not in cached])
    console.print(
        f"{len(rxcuis):,} distinct RXCUI(s) in scope, {len(cached):,} already in "
        f"{cache_path}, {pending:,} to fetch."
    )
    if not pending:
        console.print("Nothing to fetch. The cache already covers every RXCUI in scope.")
        return

    def progress(done: int, total: int) -> None:
        console.print(f"  {done:,}/{total:,} fetched", highlight=False)

    console.print(f"Fetching from {config.names.api_base} at {config.names.requests_per_second}/s.")
    updated, outcome = rxnav.refresh(
        config.names, rxcuis, cached, force=force, workers=workers, on_progress=progress
    )
    written = write_csv(cache_path, updated)
    loaded = load_cache(conn, cache_path)

    table = Table(title="Name refresh", title_justify="left")
    table.add_column("Outcome")
    table.add_column("RXCUIs", justify="right")
    table.add_row("Already cached", f"{outcome.already_cached:,}")
    table.add_row("Resolved", f"{outcome.resolved:,}")
    table.add_row("Not found in RxNav", f"{outcome.not_found:,}")
    table.add_row("Failed", f"{outcome.failed:,}")
    console.print(table)
    console.print(f"Cache now holds {written:,} row(s); {loaded:,} named drug(s) in the database.")
    if outcome.failed:
        console.print(
            f"[yellow]{outcome.failed:,} RXCUI(s) failed and were not cached.[/yellow] "
            "Rerun to retry only those."
        )

    if json_out:
        _write_json(
            json_out,
            {
                "cache_path": str(cache_path),
                "in_scope": outcome.requested,
                "already_cached": outcome.already_cached,
                "resolved": outcome.resolved,
                "not_found": outcome.not_found,
                "failed": outcome.failed,
                "cache_rows": written,
            },
        )
        console.print(f"Wrote {json_out}")


@names_app.command("fetch")
@handle_errors
def names_fetch(
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
) -> None:
    """Fetch full, ingredient and brand names from RxNav for formulary RXCUIs.

    Skips RXCUIs that already have names or were already reported missing, so
    it is safe to stop and rerun. Uses the network; nothing else does.
    """
    conn = connect(db)
    config = get_config(config_path)
    client = names_fetch_module.RxNavClient(config.names)

    def progress(summary: names_fetch_module.FetchSummary, total: int) -> None:
        done = summary.fetched + summary.not_found + summary.failed
        console.print(
            f"  {done:,}/{total:,} checked: {summary.fetched:,} fetched, "
            f"{summary.not_found:,} not found, {summary.failed:,} failed",
            highlight=False,
        )

    console.print(
        f"Fetching from {config.names.api_base} at up to "
        f"{config.names.requests_per_second:g} requests per second."
    )
    try:
        summary = names_fetch_module.fetch(
            conn, client, progress_every=config.names.progress_every, on_progress=progress
        )
    finally:
        client.close()
    if not summary.in_scope:
        console.print(
            "No RXCUIs found in the database. Load a snapshot first: rxdelta load --month YYYY-MM"
        )
        return
    table = Table(title="Name fetch", title_justify="left")
    table.add_column("Outcome")
    table.add_column("RXCUIs", justify="right")
    table.add_row("Fetched", f"{summary.fetched:,}")
    table.add_row("Already present", f"{summary.already_present:,}")
    table.add_row("Not found in RxNav", f"{summary.not_found:,}")
    table.add_row("Failed", f"{summary.failed:,}")
    console.print(table)
    if summary.failed:
        console.print(
            f"[yellow]{summary.failed:,} RXCUI(s) failed and were not stored.[/yellow] "
            "Rerun to retry only those."
        )


@names_app.command("load")
@handle_errors
def names_load(
    rrf: Annotated[
        Path | None, typer.Option("--rrf", help="Path to a local RxNorm RXNCONSO.RRF file.")
    ] = None,
    fixture: Annotated[
        Path | None,
        typer.Option("--fixture", help="CSV with rxcui, name_type and name columns."),
    ] = None,
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
) -> None:
    """Load drug names from a local file instead of the network."""
    if (rrf is None) == (fixture is None):
        raise RxdeltaError("Pass exactly one of --rrf or --fixture.")
    conn = connect(db)
    get_config(config_path)
    if rrf is not None:
        outcome = names_rrf.load(conn, rrf)
        console.print(
            f"{outcome.named:,} of {outcome.in_scope:,} formulary RXCUI(s) named from {rrf}. "
            f"{outcome.not_found:,} not in the file, logged as missing."
        )
        return
    assert fixture is not None
    written = names_fixture.load(conn, fixture)
    console.print(f"Loaded {written:,} name row(s) from {fixture}.")


@names_app.command("status")
@handle_errors
def names_status(
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
) -> None:
    """Show how many formulary RXCUIs have names, by source."""
    conn = connect(db)
    get_config(config_path)
    report = names_store.status(conn)
    if not report.formulary_rxcuis:
        console.print("No RXCUIs found in the database. Load a snapshot first.")
        return
    share = 100.0 * report.named / report.formulary_rxcuis
    console.print(
        f"{report.named:,} of {report.formulary_rxcuis:,} formulary RXCUI(s) have names "
        f"({share:.1f}%). {report.missing:,} have none; {report.logged_not_found:,} of those "
        "were looked up and not found.",
        highlight=False,
    )
    table = Table(title="Named RXCUIs by source", title_justify="left")
    table.add_column("Source")
    table.add_column("RXCUIs", justify="right")
    for source, count in report.by_source.items():
        table.add_row(source, f"{count:,}")
    console.print(table)


tools_app = typer.Typer(no_args_is_help=True, help="Inspect the read-only question tools.")
app.add_typer(tools_app, name="tools")


@tools_app.command("list")
def tools_list() -> None:
    """Name and one line description of every tool."""
    table = Table(title="Tools", title_justify="left")
    table.add_column("Name")
    table.add_column("What it does")
    for spec in tool_registry.TOOLS:
        table.add_row(spec.name, spec.summary)
    console.print(table)


@tools_app.command("schema")
@handle_errors
def tools_schema(
    output_format: Annotated[
        str,
        typer.Option(
            "--format",
            help="anthropic: a list of {name, description, input_schema}. "
            "json: input and output schemas.",
        ),
    ] = "anthropic",
) -> None:
    """Print every tool's input schema as JSON."""
    if output_format == "anthropic":
        payload: list[dict[str, Any]] = tool_registry.anthropic_tools()
    elif output_format == "json":
        payload = tool_registry.json_schemas()
    else:
        raise RxdeltaError(f"--format must be anthropic or json, got {output_format!r}")
    typer.echo(json.dumps(payload, indent=2))


@app.command("tool")
@handle_errors
def tool_call(
    name: Annotated[str, typer.Argument(help="Tool name, see rxdelta tools list.")],
    args: Annotated[str, typer.Option("--args", help="Tool arguments as a JSON object.")] = "{}",
    pretty: Annotated[bool, typer.Option("--pretty", help="Indent the JSON output.")] = False,
    db: DbOption = DEFAULT_DB_PATH,
    config_path: ConfigOption = None,
) -> None:
    """Call one tool by hand and print its result as JSON."""
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError as exc:
        raise RxdeltaError(f"--args is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RxdeltaError('--args must be a JSON object, for example \'{"query": "ozempic"}\'')
    context = ToolContext(db_path=db, config=get_config(config_path))
    result = tool_registry.call_tool(name, parsed, context)
    typer.echo(result.model_dump_json(indent=2 if pretty else None))


evalset_app = typer.Typer(no_args_is_help=True, help="Build the question eval set.")
app.add_typer(evalset_app, name="evalset")


@evalset_app.command("build")
@handle_errors
def evalset_build(
    db: Annotated[Path, typer.Option("--db", help="Database to compute gold answers from.")],
    month_from: Annotated[str, typer.Option("--from", help="First month to draw from.")],
    month_to: Annotated[str, typer.Option("--to", help="Last month to draw from.")],
    out: Annotated[Path, typer.Option("--out", help="Eval set file to write (.jsonl).")],
    n: Annotated[int, typer.Option("--n", help="Generated items, before hand-written ones.")] = 200,
    seed: Annotated[int, typer.Option("--seed", help="Random seed.")] = 7,
    handwritten: Annotated[
        Path | None,
        typer.Option("--handwritten", help="Hand-written items to merge in."),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            help="Where the loaded CMS files are. Records each month's release ID in the "
            "manifest after checking the files' hashes against the load.",
        ),
    ] = None,
    config_path: ConfigOption = None,
) -> None:
    """Build a deterministic eval set with gold answers computed from the database."""
    config = get_config(config_path)
    hand = handwritten if handwritten and handwritten.is_file() else None
    result, manifest = evalset_builder.build_file(
        db,
        config,
        month_from=month_from,
        month_to=month_to,
        n=n,
        seed=seed,
        out=out,
        handwritten_path=hand,
        data_dir=data_dir,
    )
    table = Table(title=f"Eval set {out}", title_justify="left")
    table.add_column("Category")
    table.add_column("dev", justify="right")
    table.add_column("test", justify="right")
    for category in CATEGORIES:
        members = [i for i in result.items if i.category == category]
        table.add_row(
            category,
            str(sum(1 for i in members if i.split == "dev")),
            str(sum(1 for i in members if i.split == "test")),
        )
    console.print(table)
    console.print(f"{len(result.items)} item(s) written. Manifest: {manifest}")
    if hand is None:
        console.print("[yellow]No hand-written items merged.[/yellow]")
    for short, missing in result.shortfalls.items():
        console.print(
            f"[yellow]{short}: {missing} fewer item(s) than the mix asks for.[/yellow] "
            "The database does not hold enough distinct facts for that category."
        )


@evalset_app.command("paraphrase-draft")
@handle_errors
def evalset_paraphrase_draft(
    set_path: Annotated[Path, typer.Option("--set", help="Eval set to paraphrase.")] = Path(
        "evals/rxdelta_v1.jsonl"
    ),
    n: Annotated[int, typer.Option("--n", min=1, help="Dev items, spread over categories.")] = 40,
    model: Annotated[str | None, typer.Option("--model", help="Model ID.")] = None,
    out: Annotated[Path, typer.Option("--out", help="Candidate CSV.")] = Path(
        "evals/paraphrase_candidates.csv"
    ),
    config_path: ConfigOption = None,
) -> None:
    """Draft one casual paraphrase per item with a model, for a person to approve."""
    config = get_config(config_path)
    chosen = model or config.agent.default_model
    prices = config.agent.prices(chosen)
    ledger = Ledger(config.resolve(config.agent.ledger_path))
    estimated = estimate_spend(config.agent, ledger, chosen, 0)
    if estimated.spent >= config.agent.budget_usd:
        raise RxdeltaError("The ledger is already at [agent].budget_usd.")
    drafted = paraphrase.draft(
        set_path, n, make_agent_client(config), chosen, max_tokens=1024, effort="low"
    )
    paraphrase.write_csv(out, drafted.rows)
    cost = cost_usd(drafted.usage, prices)
    ledger.append(
        run_dir=out,
        model=chosen,
        items=len(drafted.rows),
        answers=len(drafted.rows),
        cost=cost,
        kind="paraphrase",
    )
    console.print(
        f"Wrote {len(drafted.rows)} candidates to {out} for ${cost:.4f}. Mark the approved "
        "column 'yes' on each row to keep, then run rxdelta evalset paraphrase --approved.",
        highlight=False,
    )


@evalset_app.command("paraphrase")
@handle_errors
def evalset_paraphrase(
    approved: Annotated[Path, typer.Option("--approved", help="Reviewed candidate CSV.")],
    set_path: Annotated[Path, typer.Option("--set", help="Eval set the rows come from.")] = Path(
        "evals/rxdelta_v1.jsonl"
    ),
    out: Annotated[Path, typer.Option("--out", help="Paraphrase eval set.")] = Path(
        "evals/rxdelta_v1_para.jsonl"
    ),
) -> None:
    """Build an eval set from approved paraphrases, keeping the original gold answers."""
    items = paraphrase.build(set_path, approved, out)
    console.print(f"Wrote {len(items)} items to {out} with a manifest.", highlight=False)


eval_app = typer.Typer(no_args_is_help=True, help="Score a runner against the eval set.")
app.add_typer(eval_app, name="eval")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


@eval_app.command("run")
@handle_errors
def eval_run(
    set_path: Annotated[Path, typer.Option("--set", help="Eval set file.")] = Path(
        "evals/rxdelta_v1.jsonl"
    ),
    runner_name: Annotated[str, typer.Option("--runner", help="oracle or null.")] = "oracle",
    split: Annotated[str, typer.Option("--split", help="dev, test or all.")] = "dev",
    limit: Annotated[int | None, typer.Option("--limit", help="Only the first N items.")] = None,
    category: Annotated[str | None, typer.Option("--category", help="One category.")] = None,
    workers: Annotated[
        int | None, typer.Option("--workers", help="Items answered in parallel.")
    ] = None,
    db: Annotated[Path, typer.Option("--db", help="Database the eval set was built from.")] = Path(
        "rxdelta-cms.db"
    ),
    out_dir: Annotated[Path, typer.Option("--out-dir", help="Where run folders go.")] = Path(
        "runs"
    ),
    min_accuracy: Annotated[
        float | None, typer.Option("--min-accuracy", help="Override [eval].min_accuracy.")
    ] = None,
    max_missed_refusal_rate: Annotated[
        float | None,
        typer.Option("--max-missed-refusal-rate", help="Override [eval].max_missed_refusal_rate."),
    ] = None,
    stratified: Annotated[
        bool, typer.Option("--stratified", help="Spread --limit evenly over categories.")
    ] = False,
    model: Annotated[str | None, typer.Option("--model", help="Agent model ID.")] = None,
    prompt: Annotated[
        str | None, typer.Option("--prompt", help="Agent system prompt version, e.g. v1.")
    ] = None,
    repeats: Annotated[int, typer.Option("--repeats", min=1, help="Runs per item.")] = 1,
    no_schema_guard: Annotated[
        bool, typer.Option("--no-schema-guard", help="Turn off the schema guard.")
    ] = False,
    no_grounding_guard: Annotated[
        bool, typer.Option("--no-grounding-guard", help="Turn off the grounding guard.")
    ] = False,
    no_consistency_guard: Annotated[
        bool, typer.Option("--no-consistency-guard", help="Turn off the consistency guard.")
    ] = False,
    resume: Annotated[
        Path | None, typer.Option("--resume", help="Run folder to finish; same flags as before.")
    ] = None,
    record: Annotated[
        Path | None, typer.Option("--record", help="Record model responses to this cassette.")
    ] = None,
    replay: Annotated[
        Path | None, typer.Option("--replay", help="Answer only from this cassette, no key.")
    ] = None,
    config_path: ConfigOption = None,
) -> None:
    """Run one runner over the eval set and write results, a summary and a report."""
    if split not in ("dev", "test", "all"):
        raise RxdeltaError(f"--split must be dev, test or all, got {split!r}")
    config = get_config(config_path)
    guards_off = tuple(
        name
        for name, off in (
            ("schema", no_schema_guard),
            ("grounding", no_grounding_guard),
            ("consistency", no_consistency_guard),
        )
        if off
    )
    agent_flags = model or prompt or guards_off or record or replay
    if runner_name != "agent" and agent_flags:
        raise RxdeltaError("--model, --prompt, guard, --record and --replay need --runner agent.")
    if record and replay:
        raise RxdeltaError("Use --record or --replay, not both.")
    factory = None
    options = eval_harness.RunOptions(repeats=repeats, resume=resume)
    if runner_name == "agent":
        settings = AgentSettings.from_config(
            config, model=model, prompt_version=prompt, guards_off=guards_off
        )
        client: ModelClient
        if replay is not None:
            client = ReplayClient(replay, "replay")
        elif record is not None:
            client = ReplayClient(record, "record", make_agent_client(config))
        else:
            client = make_agent_client(config)
        ledger = None if replay else Ledger(config.resolve(config.agent.ledger_path))
        cap = None
        if ledger is not None:
            answers = eval_harness.planned_answers(
                set_path, split, category, limit, stratified, repeats, resume
            )
            estimated = estimate_spend(config.agent, ledger, settings.model, answers)
            console.print(estimated.describe(), highlight=False)
            check_budget(estimated)
            cap = config.agent.budget_usd - estimated.spent

        def factory(items: list[Any], ctx: ToolContext) -> AgentRunner:
            return AgentRunner(items, ctx, client=client, settings=settings)

        options = eval_harness.RunOptions(
            repeats=repeats,
            resume=resume,
            settings=settings.describe(),
            ledger=ledger,
            budget_cap=cap,
            model=settings.model,
        )
    outcome = eval_harness.run(
        set_path=set_path,
        runner_name=runner_name,
        split=split,
        db_path=db,
        config=config,
        out_root=out_dir,
        category=category,
        limit=limit,
        workers=workers,
        min_accuracy=min_accuracy,
        max_missed_refusal_rate=max_missed_refusal_rate,
        runner_factory=factory,
        stratified=stratified,
        options=options,
    )
    summary = outcome.summary
    if record is not None:
        scores_file = record.with_suffix(".scores.json")
        scores_file.write_text(
            json.dumps(
                {
                    "settings": summary["settings"],
                    "eval_set": str(set_path),
                    "split": split,
                    "limit": limit,
                    "stratified": stratified,
                    "correct": eval_harness.outcomes(outcome.directory),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        console.print(f"Recorded {record} and {scores_file}", highlight=False)
    overall = summary["overall"]
    table = Table(title=f"{runner_name} on {split}", title_justify="left")
    table.add_column("Category")
    table.add_column("Items", justify="right")
    table.add_column("Accuracy", justify="right")
    for name, row in summary["by_category"].items():
        table.add_row(name, str(row["items"]), _pct(row["accuracy"]))
    console.print(table)
    console.print(
        f"Accuracy {_pct(overall['accuracy'])} ({overall['correct']} of {overall['items']}). "
        f"False refusals {overall['false_refusals']}, missed refusals "
        f"{overall['missed_refusals']}, items with ungrounded numbers "
        f"{summary['grounding']['items_with_ungrounded']}.",
        highlight=False,
    )
    agent = summary.get("agent")
    if agent:
        reps = summary["repeats"]
        console.print(
            f"Repeats {reps['repeats']}: mean {_pct(reps['mean'])}, min {_pct(reps['min'])}, "
            f"max {_pct(reps['max'])}, stdev {100 * reps['stdev']:.1f} points. First draft "
            f"ungrounded {_pct(agent['first_draft_ungrounded_rate'])}, final "
            f"{_pct(agent['final_ungrounded_rate'])}. Cost ${summary['run_cost_usd']:.4f} "
            f"(${agent['cost_per_question']:.4f} per answer), cache hit share "
            f"{_pct(agent['cache_hit_share'])}.",
            highlight=False,
        )
    if summary.get("stopped_early"):
        err_console.print(f"[yellow]Stopped early, {summary['stopped_early']}.[/yellow]")
    if "ledger_total_usd" in summary:
        console.print(f"Total spend in the ledger ${summary['ledger_total_usd']:.4f}.")
    console.print(f"Wrote {outcome.directory}", highlight=False)
    if not outcome.passed:
        err_console.print(
            "[bold red]gate failed[/bold red] " + "; ".join(summary["gates"]["reasons"])
        )
        raise typer.Exit(code=1)


@eval_app.command("compare")
@handle_errors
def eval_compare(
    run_a: Annotated[Path, typer.Argument(help="Earlier run folder.")],
    run_b: Annotated[Path, typer.Argument(help="Later run folder.")],
) -> None:
    """Per category accuracy change and the items that flipped between two runs."""
    result = eval_harness.compare(run_a, run_b)
    if result.settings_differ:
        settings = Table(title="Settings that differ", title_justify="left")
        settings.add_column("Setting")
        settings.add_column("A")
        settings.add_column("B")
        for key, (a, b) in result.settings_differ.items():
            settings.add_row(key, json.dumps(a), json.dumps(b))
        console.print(settings)
    else:
        console.print("Both runs used the same settings.", highlight=False)
    table = Table(title="Accuracy by category", title_justify="left")
    table.add_column("Category")
    table.add_column("A", justify="right")
    table.add_column("B", justify="right")
    table.add_column("Change", justify="right")
    for name, row in result.by_category.items():
        delta = row["delta"]
        table.add_row(
            name,
            _pct(row["a"]),
            _pct(row["b"]),
            "n/a" if delta is None else f"{100 * delta:+.1f} points",
        )
    console.print(table)
    console.print(f"Right to wrong: {', '.join(result.right_to_wrong) or 'none'}", highlight=False)
    console.print(f"Wrong to right: {', '.join(result.wrong_to_right) or 'none'}", highlight=False)


@eval_app.command("show")
@handle_errors
def eval_show(
    run_dir: Annotated[Path, typer.Argument(help="Run folder.")],
    item_id: Annotated[str, typer.Argument(help="Item ID, for example rxd-v1-0042.")],
) -> None:
    """Print one item's run: tool calls, guard decisions, the answer and the gold."""
    for record in agent_trace.find_records(run_dir, item_id):
        console.print(agent_trace.render(record), highlight=False, markup=False)
        console.print()


def make_agent_client(config: Config) -> ModelClient:
    """The model client ask uses. Tests replace it with a scripted one."""
    return default_client(config)


@app.command("ask")
@handle_errors
def ask_command(
    question: Annotated[str, typer.Argument(help="The question, in plain English.")],
    model: Annotated[str | None, typer.Option("--model", help="Model ID.")] = None,
    from_month: Annotated[
        str | None, typer.Option("--from", help="Earlier month, YYYY-MM.")
    ] = None,
    to_month: Annotated[str | None, typer.Option("--to", help="Later month, YYYY-MM.")] = None,
    show_tools: Annotated[
        bool, typer.Option("--show-tools", help="Print each tool call as it happens.")
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the full structured output as JSON.")
    ] = False,
    db: Annotated[
        Path | None,
        typer.Option("--db", help="Database. Default: rxdelta-cms.db if present, else rxdelta.db."),
    ] = None,
    config_path: ConfigOption = None,
) -> None:
    """Answer a question about formulary changes from the loaded CMS data."""
    config = get_config(config_path)
    if (from_month is None) != (to_month is None):
        raise RxdeltaError("Give both --from and --to, or neither.")
    db_path = ask_support.choose_db(db)
    ctx = ToolContext(db_path=db_path, config=config)
    ask_support.check_months(ctx)
    settings = AgentSettings.from_config(config, model=model)
    if not as_json:
        err_console.print(f"[dim]Using {db_path}[/dim]", highlight=False, soft_wrap=True)

    def show(kind: str, payload: dict[str, Any]) -> None:
        if kind == "tool_call":
            args = escape(json.dumps(payload["args"], sort_keys=True))
            summary = escape(payload["summary"])
            err_console.print(
                f"[cyan]{payload['name']}[/cyan] {args}\n  [dim]-> {summary}[/dim]",
                highlight=False,
            )
        elif kind == "guard":
            problems = escape("; ".join(payload["problems"]))
            err_console.print(
                f"[yellow]check {payload['guard']} failed[/yellow] {problems}", highlight=False
            )

    runner = AgentRunner(
        [],
        ctx,
        client=make_agent_client(config),
        settings=settings,
        on_event=show if show_tools else None,
    )
    context = QuestionContext(default_from=from_month, default_to=to_month)
    output = runner.answer(question, context)
    if as_json:
        console.print_json(output.model_dump_json())
    elif output.error is None:
        console.print(output.final_text, highlight=False, markup=False, soft_wrap=True)
        console.print(
            f"[dim]{ask_support.footer(output, settings.model)}[/dim]",
            highlight=False,
            soft_wrap=True,
        )
        console.print(f"[dim]{ask_support.DISCLAIMER}[/dim]", highlight=False)
    if output.error is not None:
        raise RxdeltaError(
            ask_support.friendly_error(output.error, settings.max_cost_per_question_usd)
        )


@app.command("fetch-cms")
@handle_errors
def fetch_cms(
    release: Annotated[
        str, typer.Option("--release", help="Release ID, the archive name: 2026_20260513.")
    ],
    month: Annotated[
        str | None,
        typer.Option("--month", help="Snapshot month to file it under. Default: from the ID."),
    ] = None,
    directory: Annotated[
        Path, typer.Option("--dir", help="Root directory holding one folder per month.")
    ] = Path("data"),
    config_path: ConfigOption = None,
) -> None:
    """Download one CMS monthly release, only the three tables rxdelta reads.

    Uses the network. Reads the release archive in place over HTTP range
    requests, so about 10MB is transferred instead of the 2.3GB archive.
    """
    config = get_config(config_path)
    console.print(f"Looking up {release} in {config.download.catalog_url}")
    fetched = cms_download.fetch_release(config, release, directory, month=month)
    console.print(f"Read the tables from {fetched.url}", highlight=False)
    for name in sorted(fetched.files):
        console.print(f"  {fetched.directory / name}", highlight=False)
    console.print(
        f"Load it with: rxdelta load --month {fetched.month} --dir {directory}", highlight=False
    )


@app.command()
def version() -> None:
    """Print the rxdelta version."""
    console.print(__version__)


def main() -> None:
    try:
        app()
    except RxdeltaError as exc:  # pragma: no cover - typer normally handles this
        print(f"error {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
