"""Gold answers, computed straight from the tables.

Nothing here imports rxdelta.tools or rxdelta.diff. The change classification,
the cost range and every lookup are written again in SQL, so a bug in a tool
shows up as a disagreement instead of being copied into the eval set. The only
shared pieces are the answer contract in rxdelta.ask (payload models and the
canonical before and after strings) and the config.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from rxdelta.ask.answer import (
    ChangeAnswer,
    ChangeItem,
    ClarifyAnswer,
    ClarifyCandidate,
    CostRangeAnswer,
    CountAnswer,
    CoverageAnswer,
    DrugListAnswer,
    NoDataAnswer,
    NoDataReason,
    RefusalAnswer,
    StructuredAnswer,
)
from rxdelta.ask.values import CoverageState, change_values
from rxdelta.config import Config
from rxdelta.types import ChangeType

_PLAN_ID = re.compile(r"^([A-Z]\d{4})-(\d{1,3})(?:-(\d{1,3}))?$")
_COLUMNS = (
    "tier_level, prior_auth, step_therapy, quantity_limit, quantity_limit_amount, "
    "quantity_limit_days"
)


class GoldError(Exception):
    """A gold spec that cannot be evaluated against this database."""


@dataclass(frozen=True)
class GoldChange:
    rxcui: str
    formulary_from: str
    formulary_to: str
    change_type: ChangeType
    before: str | None
    after: str | None
    tier_before: int | None
    tier_after: int | None
    plans: frozenset[str]


# Months and plans


def months(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT snapshot_month FROM ingest_log ORDER BY 1").fetchall()
    return [str(r[0]) for r in rows]


def resolve_month(conn: sqlite3.Connection, month: str) -> str:
    """'latest' and 'previous' are resolved against what is loaded."""
    loaded = months(conn)
    if month == "latest":
        return loaded[-1]
    if month == "previous":
        return loaded[-2]
    return month


def plan_key(plan_id: str) -> tuple[str, str, str]:
    found = _PLAN_ID.match(plan_id.upper())
    if not found:
        raise GoldError(f"bad plan id {plan_id}")
    contract, plan, segment = found.groups()
    return contract, plan.zfill(3), (segment or "0").zfill(3)


def plan_formulary(conn: sqlite3.Connection, plan_id: str, month: str) -> str | None:
    contract, plan, segment = plan_key(plan_id)
    row = conn.execute(
        "SELECT formulary_id FROM plan_info WHERE snapshot_month = ? AND contract_id = ? "
        "AND plan_id = ? AND segment_id = ?",
        (month, contract, plan, segment),
    ).fetchone()
    return None if row is None else str(row[0])


def full_names(conn: sqlite3.Connection, rxcuis: list[str]) -> list[str]:
    names = dict(
        conn.execute("SELECT rxcui, name FROM drug_names WHERE name_type = 'full'").fetchall()
    )
    return [str(names.get(r, f"RXCUI {r}")) for r in rxcuis]


def rxcuis_with_token(conn: sqlite3.Connection, token: str) -> list[str]:
    """RXCUIs with any name containing the word, for questions like 'insulin products'."""
    rows = conn.execute(
        "SELECT DISTINCT rxcui FROM drug_names WHERE ' ' || name_norm || ' ' LIKE ? ORDER BY 1",
        (f"% {token.lower()} %",),
    ).fetchall()
    return [str(r[0]) for r in rows]


def rxcuis_named(conn: sqlite3.Connection, name_norm: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT rxcui FROM drug_names WHERE name_norm = ? ORDER BY 1", (name_norm,)
    ).fetchall()
    return [str(r[0]) for r in rows]


def product_of(conn: sqlite3.Connection, rxcui: str) -> str:
    for name_type in ("brand", "ingredient"):
        rows = conn.execute(
            "SELECT DISTINCT name FROM drug_names WHERE rxcui = ? AND name_type = ? ORDER BY 1",
            (rxcui, name_type),
        ).fetchall()
        if rows:
            return " / ".join(str(r[0]) for r in rows)
    return full_names(conn, [rxcui])[0]


# Coverage


def _state(row: sqlite3.Row | tuple[Any, ...], offset: int = 0) -> CoverageState:
    amount = row[offset + 4]
    days = row[offset + 5]
    return CoverageState(
        tier=int(row[offset]),
        prior_auth=bool(row[offset + 1]),
        step_therapy=bool(row[offset + 2]),
        quantity_limit=bool(row[offset + 3]),
        quantity_limit_amount=None if amount is None else float(amount),
        quantity_limit_days=None if days is None else int(days),
    )


def coverage_state(
    conn: sqlite3.Connection, rxcui: str, month: str, formulary_id: str
) -> CoverageState | None:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM formulary WHERE snapshot_month = ? AND formulary_id = ? "  # noqa: S608
        "AND rxcui = ? ORDER BY ndc_11",
        (month, formulary_id, rxcui),
    ).fetchall()
    states = {_state(r) for r in rows}
    if len(states) > 1:
        raise GoldError(f"RXCUI {rxcui} has NDCs that disagree on formulary {formulary_id}")
    return next(iter(states), None)


def _known_rxcui(conn: sqlite3.Connection, rxcui: str) -> bool:
    return bool(
        conn.execute("SELECT 1 FROM formulary WHERE rxcui = ? LIMIT 1", (rxcui,)).fetchone()
        or conn.execute("SELECT 1 FROM drug_names WHERE rxcui = ? LIMIT 1", (rxcui,)).fetchone()
    )


def coverage(
    conn: sqlite3.Connection,
    rxcui: str,
    month: str,
    formulary_id: str | None = None,
    plan_id: str | None = None,
) -> StructuredAnswer:
    month = resolve_month(conn, month)
    if month not in months(conn):
        return NoDataAnswer(reason=NoDataReason.month_not_loaded)
    if not _known_rxcui(conn, rxcui):
        return NoDataAnswer(reason=NoDataReason.unknown_drug)
    if plan_id:
        formulary_id = plan_formulary(conn, plan_id, month)
        if formulary_id is None:
            raise GoldError(f"plan {plan_id} not in {month}")
    if formulary_id is None:
        raise GoldError("coverage needs a formulary or a plan")
    state = coverage_state(conn, rxcui, month, formulary_id)
    if state is None:
        return NoDataAnswer(reason=NoDataReason.not_on_formulary)
    return CoverageAnswer(
        rxcui=rxcui,
        month=month,
        formulary_id=formulary_id,
        tier=state.tier,
        prior_auth=state.prior_auth,
        step_therapy=state.step_therapy,
        quantity_limit=state.quantity_limit,
        quantity_limit_amount=state.quantity_limit_amount,
        quantity_limit_days=state.quantity_limit_days,
    )


# Changes, classified again in SQL

_PAIRS_SQL = """
SELECT a.contract_id || '-' || a.plan_id || '-' || a.segment_id AS plan,
       a.formulary_id AS f_from, b.formulary_id AS f_to
  FROM plan_info a
  JOIN plan_info b
    ON b.contract_id = a.contract_id AND b.plan_id = a.plan_id AND b.segment_id = a.segment_id
 WHERE a.snapshot_month = :m_from AND b.snapshot_month = :m_to
"""

_SIDE = (
    "{p}.rxcui, {p}.tier_level, {p}.prior_auth, {p}.step_therapy, {p}.quantity_limit, "
    "{p}.quantity_limit_amount, {p}.quantity_limit_days"
)
_NULL_SIDE = ", ".join(["NULL"] * 7)

_JOINED_SQL = f"""
WITH pairs AS (SELECT DISTINCT f_from, f_to FROM ({_PAIRS_SQL}))
SELECT pairs.f_from, pairs.f_to, x.ndc_11, {_SIDE.format(p="x")}, {_SIDE.format(p="y")}
  FROM pairs
  JOIN formulary x ON x.snapshot_month = :m_from AND x.formulary_id = pairs.f_from
  LEFT JOIN formulary y
    ON y.snapshot_month = :m_to AND y.formulary_id = pairs.f_to AND y.ndc_11 = x.ndc_11
UNION ALL
SELECT pairs.f_from, pairs.f_to, y.ndc_11, {_NULL_SIDE}, {_SIDE.format(p="y")}
  FROM pairs
  JOIN formulary y ON y.snapshot_month = :m_to AND y.formulary_id = pairs.f_to
 WHERE NOT EXISTS (
       SELECT 1 FROM formulary x
        WHERE x.snapshot_month = :m_from AND x.formulary_id = pairs.f_from
          AND x.ndc_11 = y.ndc_11)
"""

# One branch per change type over the joined rows. a_* is the earlier month.
_CLASSIFY_SQL = f"""
WITH j(f_from, f_to, ndc, a_rxcui, a_tier, a_pa, a_st, a_ql, a_amt, a_days,
       b_rxcui, b_tier, b_pa, b_st, b_ql, b_amt, b_days) AS ({_JOINED_SQL}),
both_sides AS (SELECT * FROM j WHERE a_rxcui IS NOT NULL AND b_rxcui IS NOT NULL)
SELECT 'drug_added', * FROM j WHERE a_rxcui IS NULL
UNION ALL SELECT 'drug_removed', * FROM j WHERE b_rxcui IS NULL
UNION ALL SELECT 'tier_up', * FROM both_sides WHERE b_tier > a_tier
UNION ALL SELECT 'tier_down', * FROM both_sides WHERE b_tier < a_tier
UNION ALL SELECT 'prior_auth_added', * FROM both_sides WHERE b_pa = 1 AND a_pa = 0
UNION ALL SELECT 'prior_auth_removed', * FROM both_sides WHERE b_pa = 0 AND a_pa = 1
UNION ALL SELECT 'step_therapy_added', * FROM both_sides WHERE b_st = 1 AND a_st = 0
UNION ALL SELECT 'step_therapy_removed', * FROM both_sides WHERE b_st = 0 AND a_st = 1
UNION ALL SELECT 'quantity_limit_added', * FROM both_sides WHERE b_ql = 1 AND a_ql = 0
UNION ALL SELECT 'quantity_limit_removed', * FROM both_sides WHERE b_ql = 0 AND a_ql = 1
UNION ALL SELECT 'quantity_limit_tightened', * FROM both_sides
 WHERE a_ql = 1 AND b_ql = 1 AND a_amt IS NOT NULL AND b_amt IS NOT NULL
   AND a_days > 0 AND b_days > 0 AND b_amt * a_days < a_amt * b_days
UNION ALL SELECT 'quantity_limit_loosened', * FROM both_sides
 WHERE a_ql = 1 AND b_ql = 1 AND a_amt IS NOT NULL AND b_amt IS NOT NULL
   AND a_days > 0 AND b_days > 0 AND b_amt * a_days > a_amt * b_days
"""


def changes(conn: sqlite3.Connection, month_from: str, month_to: str) -> list[GoldChange]:
    params = {"m_from": month_from, "m_to": month_to}
    plans_by_pair: dict[tuple[str, str], set[str]] = defaultdict(set)
    for plan, f_from, f_to in conn.execute(_PAIRS_SQL, params).fetchall():
        plans_by_pair[(str(f_from), str(f_to))].add(str(plan))

    grouped: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    tiers: dict[tuple[Any, ...], tuple[int | None, int | None]] = {}
    for row in conn.execute(_CLASSIFY_SQL, params).fetchall():
        change_type = ChangeType(str(row[0]))
        f_from, f_to = str(row[1]), str(row[2])
        before = _state(row, 5) if row[4] is not None else None
        after = _state(row, 12) if row[11] is not None else None
        rxcui = str(row[11] if row[11] is not None else row[4])
        before_value, after_value = change_values(change_type, before, after)
        key = (rxcui, f_from, f_to, change_type, before_value, after_value)
        grouped[key] |= plans_by_pair[(f_from, f_to)]
        tiers[key] = (before.tier if before else None, after.tier if after else None)
    return sorted(
        (
            GoldChange(
                rxcui=k[0],
                formulary_from=k[1],
                formulary_to=k[2],
                change_type=k[3],
                before=k[4],
                after=k[5],
                tier_before=tiers[k][0],
                tier_after=tiers[k][1],
                plans=frozenset(p),
            )
            for k, p in grouped.items()
        ),
        key=lambda c: (c.formulary_to, c.rxcui, c.change_type.value, c.before or "", c.after or ""),
    )


def _month_problem(conn: sqlite3.Connection, *wanted: str) -> bool:
    loaded = months(conn)
    return any(m not in loaded for m in wanted)


def filter_changes(
    rows: list[GoldChange],
    *,
    change_types: list[str] | None = None,
    formulary_ids: list[str] | None = None,
    rxcuis: list[str] | None = None,
    tier_from: int | None = None,
    tier_to: int | None = None,
) -> list[GoldChange]:
    types = {ChangeType(t) for t in change_types or []}
    forms = set(formulary_ids or [])
    wanted = set(rxcuis or [])
    return [
        r
        for r in rows
        if (not types or r.change_type in types)
        and (not forms or r.formulary_from in forms or r.formulary_to in forms)
        and (not wanted or r.rxcui in wanted)
        and (tier_from is None or r.tier_before == tier_from)
        and (tier_to is None or r.tier_after == tier_to)
    ]


def _scope_formularies(
    conn: sqlite3.Connection, args: dict[str, Any], month_to: str
) -> list[str] | None:
    if args.get("plan_id"):
        formulary = plan_formulary(conn, str(args["plan_id"]), month_to)
        if formulary is None:
            raise GoldError(f"plan {args['plan_id']} not in {month_to}")
        return [formulary]
    if args.get("formulary_id"):
        return [str(args["formulary_id"])]
    if args.get("formulary_ids"):
        return [str(f) for f in args["formulary_ids"]]
    return None


def _drug_list(conn: sqlite3.Connection, rxcuis: set[str]) -> StructuredAnswer:
    if not rxcuis:
        return NoDataAnswer(reason=NoDataReason.no_matching_changes)
    ordered = sorted(rxcuis)
    return DrugListAnswer(rxcuis=ordered, display_names=full_names(conn, ordered))


def _pair(conn: sqlite3.Connection, args: dict[str, Any]) -> tuple[str, str]:
    return resolve_month(conn, str(args["from"])), resolve_month(conn, str(args["to"]))


def filtered_changes(conn: sqlite3.Connection, args: dict[str, Any]) -> list[GoldChange]:
    month_from, month_to = _pair(conn, args)
    return filter_changes(
        changes(conn, month_from, month_to),
        change_types=args.get("change_types"),
        formulary_ids=_scope_formularies(conn, args, month_to),
        rxcuis=args.get("rxcuis"),
        tier_from=args.get("tier_from"),
        tier_to=args.get("tier_to"),
    )


def drug_change(conn: sqlite3.Connection, args: dict[str, Any]) -> StructuredAnswer:
    month_from, month_to = _pair(conn, args)
    if _month_problem(conn, month_from, month_to):
        return NoDataAnswer(reason=NoDataReason.month_not_loaded)
    rxcuis = sorted(str(r) for r in args["rxcuis"])
    rows = filtered_changes(conn, {**args, "rxcuis": rxcuis})
    if not rows:
        return NoDataAnswer(reason=NoDataReason.no_matching_changes)
    items = sorted(
        {
            ChangeItem(
                rxcui=r.rxcui,
                formulary_id=r.formulary_to,
                change_type=r.change_type,
                before=r.before,
                after=r.after,
            )
            for r in rows
        },
        key=lambda i: (i.rxcui, i.formulary_id, i.change_type.value),
    )
    return ChangeAnswer(rxcuis=rxcuis, items=items)


def filtered_list(conn: sqlite3.Connection, args: dict[str, Any]) -> StructuredAnswer:
    month_from, month_to = _pair(conn, args)
    if _month_problem(conn, month_from, month_to):
        return NoDataAnswer(reason=NoDataReason.month_not_loaded)
    return _drug_list(conn, {r.rxcui for r in filtered_changes(conn, args)})


def count_drugs(conn: sqlite3.Connection, args: dict[str, Any]) -> StructuredAnswer:
    month_from, month_to = _pair(conn, args)
    if _month_problem(conn, month_from, month_to):
        return NoDataAnswer(reason=NoDataReason.month_not_loaded)
    rows = filtered_changes(conn, args)
    if not rows:
        return NoDataAnswer(reason=NoDataReason.no_matching_changes)
    return CountAnswer(count=len({r.rxcui for r in rows}), unit="drugs")


def class_changes(conn: sqlite3.Connection, args: dict[str, Any]) -> StructuredAnswer:
    """Drugs whose names contain a word (a drug class like 'insulin') that changed."""
    month_from, month_to = _pair(conn, args)
    members = rxcuis_with_token(conn, str(args["class_token"]))
    rows = filtered_changes(conn, {**args, "rxcuis": members})
    found = {r.rxcui for r in rows}
    if not found:
        return NoDataAnswer(reason=NoDataReason.no_matching_changes)
    answer = _drug_list(conn, found)
    if not args.get("with_tiers"):
        return answer
    formulary = _scope_formularies(conn, args, month_to)
    assert formulary is not None and len(formulary) == 1
    tiers: dict[str, int] = {}
    for rxcui in sorted(found):
        state = coverage_state(conn, rxcui, month_to, formulary[0])
        if state is not None:
            tiers[rxcui] = state.tier
    assert isinstance(answer, DrugListAnswer)
    return answer.model_copy(update={"tiers": tiers})


def changes_where_coverage(conn: sqlite3.Connection, args: dict[str, Any]) -> StructuredAnswer:
    """Changed drugs whose coverage in the later month passes a test."""
    _, month_to = _pair(conn, args)
    rows = filtered_changes(conn, args)
    formularies = _scope_formularies(conn, args, month_to)
    target = str(args.get("coverage_formulary_id") or (formularies or [""])[0])
    test: dict[str, Any] = dict(args["coverage"])
    keep: set[str] = set()
    for rxcui in sorted({r.rxcui for r in rows}):
        state = coverage_state(conn, rxcui, month_to, target)
        if "covered" in test and (state is not None) != bool(test["covered"]):
            continue
        if state is None and any(k != "covered" for k in test):
            continue
        if state is not None and any(
            getattr(state, k) != v for k, v in test.items() if k != "covered"
        ):
            continue
        keep.add(rxcui)
    return _drug_list(conn, keep)


def changes_intersection(conn: sqlite3.Connection, args: dict[str, Any]) -> StructuredAnswer:
    """Drugs that carry every one of several change types."""
    sets = [
        {r.rxcui for r in filtered_changes(conn, {**args, "change_types": [t]})}
        for t in args["all_of"]
    ]
    return _drug_list(conn, set.intersection(*sets) if sets else set())


# Cost, priced again from the raw beneficiary cost rows

_CHANNELS = ("pref", "nonpref", "mail_pref", "mail_nonpref")


def _tier_cost(
    conn: sqlite3.Connection, config: Config, month: str, plan_id: str, tier: int | None
) -> tuple[float, float] | None:
    if tier is None:
        return None
    contract, plan, segment = plan_key(plan_id)
    rows = conn.execute(
        "SELECT * FROM beneficiary_cost WHERE snapshot_month = ? AND contract_id = ? "
        "AND plan_id = ? AND segment_id = ? AND coverage_level = ? AND tier = ?",
        (month, contract, plan, segment, config.impact.coverage_level, tier),
    ).fetchall()
    lows: list[float] = []
    highs: list[float] = []
    for row in rows:
        days = config.codes.days_supply[str(row["days_supply"])].days
        if days is None:
            continue
        scale = config.impact.normalize_days / days
        for channel in _CHANNELS:
            code = str(row[f"cost_type_{channel}"] or "")
            if not code:
                continue
            kind = config.codes.cost_type[code].kind
            if kind == "copay" and row[f"cost_amt_{channel}"] is not None:
                value = float(row[f"cost_amt_{channel}"]) * scale
                lows.append(value)
                highs.append(value)
            elif kind == "coinsurance":
                bounds = [
                    float(v)
                    for v in (row[f"cost_min_amt_{channel}"], row[f"cost_max_amt_{channel}"])
                    if v
                ]
                if bounds:
                    lows.append(min(bounds) * scale)
                    highs.append(max(bounds) * scale)
    if not lows:
        return None
    return min(lows), max(highs)


def _plans_in_scope(
    conn: sqlite3.Connection, month_from: str, month_to: str, args: dict[str, Any]
) -> list[tuple[str, str, str]]:
    rows = [
        (str(p), str(a), str(b))
        for p, a, b in conn.execute(_PAIRS_SQL, {"m_from": month_from, "m_to": month_to}).fetchall()
    ]
    if args.get("plan_id"):
        wanted = "-".join(plan_key(str(args["plan_id"])))
        return [r for r in rows if r[0] == wanted]
    formulary = str(args["formulary_id"])
    return sorted(r for r in rows if formulary in (r[1], r[2]))


def cost_change(conn: sqlite3.Connection, config: Config, args: dict[str, Any]) -> StructuredAnswer:
    month_from, month_to = _pair(conn, args)
    if _month_problem(conn, month_from, month_to):
        return NoDataAnswer(reason=NoDataReason.month_not_loaded)
    rxcui = str(args["rxcui"])
    lows: list[float] = []
    highs: list[float] = []
    covered = False
    for plan, f_from, f_to in _plans_in_scope(conn, month_from, month_to, args):
        before = coverage_state(conn, rxcui, month_from, f_from)
        after = coverage_state(conn, rxcui, month_to, f_to)
        if before is None and after is None:
            continue
        covered = True
        tier_before = before.tier if before else None
        tier_after = after.tier if after else None
        if tier_before == tier_after:
            lows.append(0.0)
            highs.append(0.0)
            continue
        if tier_before is None or tier_after is None:
            raise GoldError("open ended cost changes are not used as gold items")
        cost_before = _tier_cost(conn, config, month_from, plan, tier_before)
        cost_after = _tier_cost(conn, config, month_to, plan, tier_after)
        if cost_before is None or cost_after is None:
            continue
        low = round(cost_after[0] - cost_before[1], 2)
        high = round(cost_after[1] - cost_before[0], 2)
        lows.append(min(low, high))
        highs.append(max(low, high))
    if not covered:
        return NoDataAnswer(reason=NoDataReason.not_on_formulary)
    if not lows:
        raise GoldError(f"RXCUI {rxcui} is not priced in this scope")
    return CostRangeAnswer(rxcui=rxcui, low=min(lows), high=max(highs))


# Clarify


def clarify(conn: sqlite3.Connection, args: dict[str, Any]) -> tuple[ClarifyAnswer, ChangeAnswer]:
    """The candidates an ambiguous name stands for, and the full answer for all of them."""
    rxcuis = sorted(str(r) for r in args["rxcuis"])
    by_product: dict[str, list[str]] = defaultdict(list)
    for rxcui in rxcuis:
        by_product[product_of(conn, rxcui)].append(rxcui)
    candidates = [
        ClarifyCandidate(product=p, rxcuis=sorted(r)) for p, r in sorted(by_product.items())
    ]
    full = drug_change(conn, {**args, "rxcuis": rxcuis})
    alternative = full if isinstance(full, ChangeAnswer) else ChangeAnswer(rxcuis=rxcuis, items=[])
    return ClarifyAnswer(candidates=candidates), alternative


# Dispatch


def evaluate(conn: sqlite3.Connection, config: Config, query: str, args: dict[str, Any]) -> Any:
    """Run one gold query. Returns a StructuredAnswer, or for clarify a pair."""
    if query == "coverage":
        return coverage(
            conn,
            str(args["rxcui"]),
            str(args["month"]),
            formulary_id=args.get("formulary_id"),
            plan_id=args.get("plan_id"),
        )
    if query == "drug_change":
        return drug_change(conn, args)
    if query == "filtered_list":
        return filtered_list(conn, args)
    if query == "count_drugs":
        return count_drugs(conn, args)
    if query == "cost_change":
        return cost_change(conn, config, args)
    if query == "class_changes":
        return class_changes(conn, args)
    if query == "changes_where_coverage":
        return changes_where_coverage(conn, args)
    if query == "changes_intersection":
        return changes_intersection(conn, args)
    if query == "unknown_drug":
        return NoDataAnswer(reason=NoDataReason.unknown_drug)
    if query == "month_not_loaded":
        if not _month_problem(conn, *[str(m) for m in args["months"]]):
            raise GoldError(f"months {args['months']} are all loaded")
        return NoDataAnswer(reason=NoDataReason.month_not_loaded)
    if query == "refusal":
        return RefusalAnswer(reason=args.get("reason", "medical_advice"))
    if query == "clarify":
        return clarify(conn, args)
    raise GoldError(f"unknown gold query {query}")


def describe(query: str, args: dict[str, Any]) -> str:
    """The human readable gold_query string stored on each item."""
    parts = []
    for key in sorted(args):
        value = args[key]
        if isinstance(value, list):
            value = "[" + ",".join(str(v) for v in value) + "]"
        elif isinstance(value, dict):
            value = "{" + ",".join(f"{k}:{v}" for k, v in sorted(value.items())) + "}"
        parts.append(f"{key}={value}")
    return f"{query}({','.join(parts)})"
