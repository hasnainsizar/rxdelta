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
from rich.table import Table

from rxdelta import __version__
from rxdelta.config import Config, get_config
from rxdelta.db import DEFAULT_DB_PATH, connect, queries
from rxdelta.diff import impact
from rxdelta.diff.engine import DiffResult, diff_snapshots
from rxdelta.diff.impact import ChangeGroup
from rxdelta.ingest.loader import load_month
from rxdelta.limitations import ESTIMATE_NOTE, LIMITATIONS, LIMITATIONS_TITLE, OPEN_ENDED_NOTE
from rxdelta.names import rxnav
from rxdelta.names.cache import load_cache, read_csv, write_csv
from rxdelta.report import format as fmt
from rxdelta.report import html
from rxdelta.types import RxdeltaError

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
        "groups": [_group_payload(config, g) for g in groups],
        "limitations": list(LIMITATIONS),
        "estimate_note": ESTIMATE_NOTE,
        "open_ended_note": OPEN_ENDED_NOTE,
    }


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
                fmt.change_types(group.change_types),
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


names_app = typer.Typer(no_args_is_help=True, help="Manage the RXCUI to drug name cache.")
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
