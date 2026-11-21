"""What `rxdelta ask` needs around the agent: the database, readiness checks and
the one line summary under an answer."""

from __future__ import annotations

from pathlib import Path

from rxdelta.ask.answer import RunnerOutput
from rxdelta.db import DEFAULT_DB_PATH
from rxdelta.tools.context import ToolContext
from rxdelta.tools.registry import call_tool
from rxdelta.types import RxdeltaError

REAL_DB = Path("rxdelta-cms.db")
DISCLAIMER = "This reports CMS formulary data. It is not medical advice."
_MONTH_KEYS = (("from_month", "to_month"), ("from", "to"))


def choose_db(db: Path | None) -> Path:
    """The database asked for, else the real CMS one when present, else the default."""
    if db is not None:
        chosen = db
    elif REAL_DB.is_file():
        chosen = REAL_DB
    else:
        chosen = DEFAULT_DB_PATH
    if not chosen.is_file():
        raise RxdeltaError(
            f"Database not found: {chosen}. Load months with rxdelta load or pass --db."
        )
    return chosen


def check_months(ctx: ToolContext) -> list[str]:
    result = call_tool("list_months", {}, ctx)
    months = [m.month for m in result.data.months] if result.ok and result.data else []
    if not months:
        raise RxdeltaError(f"No months are loaded in {ctx.db_path}. Load one with rxdelta load.")
    return months


def months_used(output: RunnerOutput) -> str | None:
    """The month pair of the last tool call that compared two months."""
    for call in reversed(output.tool_calls):
        for start, end in _MONTH_KEYS:
            if start in call.args and end in call.args:
                return f"{call.args[start]} to {call.args[end]}"
        if "month" in call.args:
            return str(call.args["month"])
    return None


def friendly_error(error: str, cap: float) -> str:
    if error.startswith("limit: spent"):
        return (
            f"This question went over the per question cost cap of ${cap:.2f} "
            "([agent].max_cost_per_question_usd in config/rxdelta.toml)."
        )
    if error.startswith("limit:"):
        return "The question needed more steps than allowed: " + error.removeprefix("limit: ") + "."
    return error


def footer(output: RunnerOutput, model: str) -> str:
    months = months_used(output)
    parts = [
        f"months {months}" if months else "no months read",
        model,
        f"{len(output.tool_calls)} tool calls",
        f"{output.input_tokens:,} in and {output.output_tokens:,} out tokens",
        f"${output.cost_usd:.4f}",
        f"{output.latency_ms / 1000:.1f}s",
    ]
    return ", ".join(parts)
