from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rxdelta.cli import app
from rxdelta.config import Config
from rxdelta.db import connect
from rxdelta.tools.context import ToolContext
from rxdelta.tools.envelope import ErrorCode, ToolResult
from rxdelta.tools.registry import TOOLS, anthropic_tools, call_tool, clean_schema

runner = CliRunner()
F1, F2, F3 = "00026123", "00026124", "00026125"


@pytest.fixture
def ctx(ask_db: Path, config: Config) -> ToolContext:
    return ToolContext(db_path=ask_db, config=config)


def capped(ctx: ToolContext, **caps: int) -> ToolContext:
    tools = ctx.config.tools.model_copy(update={"row_caps": {**ctx.config.tools.row_caps, **caps}})
    return ToolContext(db_path=ctx.db_path, config=replace(ctx.config, tools=tools))


def call(ctx: ToolContext, name: str, **args: Any) -> ToolResult[Any]:
    return call_tool(name, args, ctx)


def assert_error(result: ToolResult[Any], code: ErrorCode, text: str) -> None:
    assert not result.ok
    assert result.error is not None
    assert result.error.code == code, result.error
    assert text in (result.note or ""), result.note


def assert_empty(result: ToolResult[Any], text: str) -> None:
    """A query that ran and matched nothing: a success with a plain note."""
    assert result.ok, result.error
    assert result.error is None
    assert result.total == 0 and not result.truncated
    assert text in (result.note or ""), result.note


def all_changes(ctx: ToolContext, **filters: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    while True:
        result = call(ctx, "get_changes", offset=len(rows), limit=100, **filters)
        assert result.ok and result.data is not None, result.note
        rows += [r.model_dump(mode="json") for r in result.data.rows]
        if not result.truncated or len(rows) >= (result.total or 0):
            return rows


# Read only


def test_a_write_through_the_tool_connection_fails(ctx: ToolContext) -> None:
    conn = ctx.connect()
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM formulary")
    conn.close()


def test_tools_never_raise_on_a_missing_database(tmp_path: Path, config: Config) -> None:
    result = call_tool("list_months", {}, ToolContext(tmp_path / "nope.db", config))
    assert_error(result, ErrorCode.INTERNAL, "Database not found")


def test_unknown_tool_name_is_invalid_args(ctx: ToolContext) -> None:
    assert_error(call(ctx, "drop_tables"), ErrorCode.INVALID_ARGS, "No tool named")


# list_months


def test_list_months_happy_path(ctx: ToolContext) -> None:
    result = call(ctx, "list_months")
    assert result.ok and result.data is not None
    assert [m.month for m in result.data.months] == ["2026-01", "2026-02", "2026-03"]
    assert (result.data.earliest, result.data.latest) == ("2026-01", "2026-03")
    assert result.provenance.months == ["2026-01", "2026-02", "2026-03"]


def test_list_months_on_an_empty_database_says_why(tmp_path: Path, config: Config) -> None:
    connect(tmp_path / "e.db").close()
    result = call_tool("list_months", {}, ToolContext(tmp_path / "e.db", config))
    assert_empty(result, "No months are loaded")
    assert result.data is not None and result.data.months == []


def test_list_months_rejects_arguments(ctx: ToolContext) -> None:
    assert_error(call(ctx, "list_months", month="2026-01"), ErrorCode.INVALID_ARGS, "month")


# find_drug


def test_find_drug_happy_path_groups_one_brand(ctx: ToolContext) -> None:
    result = call(ctx, "find_drug", query="Ozempic")
    assert result.ok and result.data is not None
    assert [(p.product, p.rxcuis) for p in result.data.products] == [
        ("Ozempic", ["8800101", "8800102", "8800103"])
    ]
    assert result.data.ambiguous
    assert "one product with 3 RXCUIs" in (result.note or "")


def test_find_drug_flags_drugs_not_on_any_formulary(ctx: ToolContext) -> None:
    result = call(ctx, "find_drug", query="Wegovy")
    assert result.ok and result.data is not None
    drug = result.data.products[0].drugs[0]
    assert drug.rxcui == "8800111" and not drug.on_formulary and drug.months == []
    assert "Not on any loaded formulary" in (result.note or "")


def test_find_drug_unknown_lists_near_misses(ctx: ToolContext) -> None:
    result = call(ctx, "find_drug", query="lantsu")
    assert_error(result, ErrorCode.UNKNOWN_DRUG, "Closest names below the match threshold")
    assert "Lantus" in (result.note or "")
    nothing = call(ctx, "find_drug", query="qqqqqqqqqqqq")
    assert_error(nothing, ErrorCode.UNKNOWN_DRUG, "Nothing in the name table is close")


def test_find_drug_bad_args(ctx: ToolContext) -> None:
    assert_error(call(ctx, "find_drug", query=""), ErrorCode.INVALID_ARGS, "query")
    assert_error(call(ctx, "find_drug", query="x", limit=0), ErrorCode.INVALID_ARGS, "limit")


def test_find_drug_cap(ctx: ToolContext) -> None:
    result = call(capped(ctx, find_drug=2), "find_drug", query="insulin")
    assert result.ok and result.truncated and result.total == 6
    assert result.data is not None
    assert sum(len(p.rxcuis) for p in result.data.products) == 2
    assert "Showing 2 of 6" in (result.note or "")


# find_plan


def test_find_plan_happy_paths(ctx: ToolContext) -> None:
    result = call(ctx, "find_plan", query="H1234-001")
    assert result.ok and result.data is not None
    assert [(p.plan_id, p.formulary_id) for p in result.data.plans] == [("H1234-001-000", F1)]
    by_name = call(ctx, "find_plan", query="prairie choice", month="2026-02")
    assert by_name.data is not None
    assert [p.plan_id for p in by_name.data.plans] == ["S9012-002-000"]
    by_formulary = call(ctx, "find_plan", query=F3, month="2026-01")
    assert by_formulary.data is not None and by_formulary.data.matched_by == "formulary_id"
    assert len(by_formulary.data.plans) == 2


def test_find_plan_unknown(ctx: ToolContext) -> None:
    assert_error(call(ctx, "find_plan", query="Z9999-001"), ErrorCode.UNKNOWN_PLAN, "No plan")


def test_find_plan_bad_args_and_month(ctx: ToolContext) -> None:
    assert_error(call(ctx, "find_plan"), ErrorCode.INVALID_ARGS, "query")
    assert_error(
        call(ctx, "find_plan", query="H1234", month="2025-12"),
        ErrorCode.MONTH_NOT_LOADED,
        "2025-12",
    )


def test_find_plan_cap(ctx: ToolContext) -> None:
    result = call(capped(ctx, find_plan=1), "find_plan", query="S9012", month="2026-03")
    assert result.ok and result.truncated and result.total == 3


# get_coverage


def test_get_coverage_for_one_plan(ctx: ToolContext) -> None:
    result = call(ctx, "get_coverage", rxcui="8800141", month="2026-03", plan_id="H1234-001")
    assert result.ok and result.data is not None
    (row,) = result.data.rows
    assert (row.formulary_id, row.coverage.tier, row.coverage.prior_auth) == (F1, 2, True)
    assert row.ndcs == ["88000014101", "88000014102"]
    assert len(result.provenance.row_keys) == 2


def test_get_coverage_summary_without_a_filter(ctx: ToolContext) -> None:
    result = call(ctx, "get_coverage", rxcui="8800141", month="2026-01")
    assert result.ok and result.data is not None
    assert result.data.summary.formularies_covering == 3
    assert result.data.summary.tier_distribution == {"3": 3}


def test_get_coverage_not_on_formulary_is_an_honest_empty(ctx: ToolContext) -> None:
    result = call(ctx, "get_coverage", rxcui="8800191", month="2026-01", formulary_id=F2)
    assert_empty(result, f"is not on formulary {F2} in 2026-01")
    assert result.data is not None and result.data.rows == []


def test_get_coverage_errors(ctx: ToolContext) -> None:
    assert_error(
        call(ctx, "get_coverage", rxcui="4040404", month="2026-01"),
        ErrorCode.UNKNOWN_DRUG,
        "4040404",
    )
    assert_error(
        call(
            ctx,
            "get_coverage",
            rxcui="8800141",
            month="2026-01",
            formulary_id=F1,
            plan_id="H1234-001",
        ),
        ErrorCode.INVALID_ARGS,
        "not both",
    )
    assert_error(
        call(ctx, "get_coverage", rxcui="8800141", month="2027-01"),
        ErrorCode.MONTH_NOT_LOADED,
        "2027-01",
    )
    assert_error(
        call(ctx, "get_coverage", rxcui="8800141", month="2026-01", plan_id="H9999-001"),
        ErrorCode.UNKNOWN_PLAN,
        "H9999-001-000",
    )


def test_get_coverage_cap(ctx: ToolContext) -> None:
    result = call(capped(ctx, get_coverage=1), "get_coverage", rxcui="8800141", month="2026-01")
    assert result.ok and result.truncated and result.total == 3
    assert result.data is not None and result.data.summary.formularies_covering == 3


# get_changes


def test_get_changes_happy_path(ctx: ToolContext) -> None:
    result = call(
        ctx,
        "get_changes",
        from_month="2026-01",
        to_month="2026-03",
        change_types=["prior_auth_added"],
        formulary_ids=[F1],
    )
    assert result.ok and result.data is not None
    assert sorted(r.rxcui for r in result.data.rows) == ["8800102", "8800141", "8800331"]
    for row in result.data.rows:
        assert (row.before_value, row.after_value) == ("no", "yes")
        assert row.affected_plans == 2


def test_get_changes_tier_values_and_filters(ctx: ToolContext) -> None:
    rows = all_changes(ctx, from_month="2026-02", to_month="2026-03", tier_from=3, tier_to=4)
    assert {(r["rxcui"], r["formulary_id"]) for r in rows} == {
        ("8800151", F1),
        ("8800441", F1),
        ("8800102", F2),
        ("8800151", F2),
        ("8800101", F3),
        ("8800102", F3),
    }
    assert {(r["before_value"], r["after_value"]) for r in rows} == {("3", "4")}


def test_get_changes_empty_result_explains(ctx: ToolContext) -> None:
    result = call(
        ctx,
        "get_changes",
        from_month="2026-01",
        to_month="2026-03",
        rxcuis=["8800271"],
        formulary_ids=[F1],
    )
    assert_empty(result, "nothing of that kind changed")
    assert result.data is not None and result.data.rows == []


def test_get_changes_errors(ctx: ToolContext) -> None:
    assert_error(
        call(
            ctx, "get_changes", from_month="2026-01", to_month="2026-03", change_types=["pa_added"]
        ),
        ErrorCode.INVALID_ARGS,
        "change_types",
    )
    assert_error(
        call(ctx, "get_changes", from_month="2026-01", to_month="2026-01"),
        ErrorCode.INVALID_ARGS,
        "must be different",
    )
    assert_error(
        call(ctx, "get_changes", from_month="2025-12", to_month="2026-03"),
        ErrorCode.MONTH_NOT_LOADED,
        "2025-12",
    )
    assert_error(
        call(ctx, "get_changes", from_month="2026-01", to_month="2026-03", rxcuis=["4040404"]),
        ErrorCode.UNKNOWN_DRUG,
        "4040404",
    )
    assert_error(
        call(
            ctx, "get_changes", from_month="2026-01", to_month="2026-03", formulary_ids=["99999999"]
        ),
        ErrorCode.UNKNOWN_PLAN,
        "99999999",
    )


def test_get_changes_cap_and_paging(ctx: ToolContext) -> None:
    small = capped(ctx, get_changes=5)
    first = call(small, "get_changes", from_month="2026-01", to_month="2026-02", limit=50)
    assert first.ok and first.truncated
    assert first.data is not None and len(first.data.rows) == 5
    total = first.total
    assert total is not None and total > 5
    assert "Showing 5 of" in (first.note or "")
    everything = all_changes(ctx, from_month="2026-01", to_month="2026-02")
    assert len(everything) == total
    past = call(ctx, "get_changes", from_month="2026-01", to_month="2026-02", offset=10_000)
    assert_error(past, ErrorCode.INVALID_ARGS, "past the end")


def test_every_change_type_appears_in_every_month_pair(ctx: ToolContext) -> None:
    from rxdelta.types import ChangeType

    for pair in (("2026-01", "2026-02"), ("2026-02", "2026-03"), ("2026-01", "2026-03")):
        result = call(ctx, "count_changes", from_month=pair[0], to_month=pair[1])
        assert result.data is not None
        assert {g.key for g in result.data.groups} == {c.value for c in ChangeType}, pair


# count_changes


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"change_types": ["tier_up"]},
        {"formulary_ids": [F2], "change_types": ["quantity_limit_added", "prior_auth_added"]},
        {"rxcuis": ["8800101", "8800102", "8800103"]},
        {"tier_from": 3},
    ],
)
@pytest.mark.parametrize("group_by", ["change_type", "formulary_id", "rxcui", "tier_move"])
def test_count_changes_agrees_with_get_changes(
    ctx: ToolContext, filters: dict[str, Any], group_by: str
) -> None:
    rows = all_changes(ctx, from_month="2026-01", to_month="2026-03", **filters)
    result = call(
        ctx,
        "count_changes",
        from_month="2026-01",
        to_month="2026-03",
        group_by=group_by,
        **filters,
    )
    assert result.ok and result.data is not None
    totals = result.data.totals
    assert totals.rows == len(rows)
    assert totals.drugs == len({r["rxcui"] for r in rows})
    assert totals.formularies == len({r["formulary_id"] for r in rows})
    assert totals.plans == len({p for r in rows for p in r["plans"]})
    assert sum(g.counts.rows for g in result.data.groups) == len(rows)


def test_count_changes_tier_move_groups(ctx: ToolContext) -> None:
    result = call(
        ctx,
        "count_changes",
        from_month="2026-02",
        to_month="2026-03",
        change_types=["tier_up"],
        group_by="tier_move",
    )
    assert result.data is not None
    groups = {g.key: g.counts.drugs for g in result.data.groups}
    assert groups["3->4"] == 4


def test_count_changes_empty_and_errors(ctx: ToolContext) -> None:
    empty = call(
        ctx,
        "count_changes",
        from_month="2026-01",
        to_month="2026-02",
        rxcuis=["8800271"],
        formulary_ids=[F1],
    )
    assert_empty(empty, "No changes match")
    assert empty.data is not None and empty.data.totals.rows == 0
    assert_error(
        call(ctx, "count_changes", from_month="2026-01", to_month="2026-02", group_by="plan"),
        ErrorCode.INVALID_ARGS,
        "group_by",
    )
    assert_error(
        call(ctx, "count_changes", from_month="2026-01", to_month="2026-04"),
        ErrorCode.MONTH_NOT_LOADED,
        "2026-04",
    )


def test_count_changes_cap_keeps_full_totals(ctx: ToolContext) -> None:
    result = call(
        capped(ctx, count_changes=2),
        "count_changes",
        from_month="2026-01",
        to_month="2026-03",
        group_by="rxcui",
    )
    assert result.ok and result.truncated and result.data is not None
    assert len(result.data.groups) == 2
    assert result.total is not None and result.total > 2
    assert result.data.totals.drugs == result.total


# estimate_cost_change


def test_estimate_cost_change_on_a_tier_move(ctx: ToolContext) -> None:
    result = call(
        ctx,
        "estimate_cost_change",
        rxcui="8800151",
        from_month="2026-02",
        to_month="2026-03",
        plan_id="H1234-001",
    )
    assert result.ok and result.data is not None
    data = result.data
    assert (data.tier_before, data.tier_after, data.tier_changed) == (3, 4, True)
    assert data.before is not None and (data.before.low, data.before.high) == (33.75, 54.0)
    assert data.after is not None and (data.after.low, data.after.high) == (71.25, 114.0)
    assert data.change is not None and (data.change.low, data.change.high) == (17.25, 80.25)
    assert data.currency == "USD" and data.leaves_out


def test_estimate_cost_change_without_a_tier_move_is_zero(ctx: ToolContext) -> None:
    result = call(
        ctx,
        "estimate_cost_change",
        rxcui="8800141",
        from_month="2026-02",
        to_month="2026-03",
        formulary_id=F1,
    )
    assert result.ok and result.data is not None
    assert result.data.change is not None
    assert (result.data.change.low, result.data.change.high) == (0.0, 0.0)
    assert "did not change tier" in (result.note or "")


def test_no_tier_move_is_a_zero_change_even_when_the_tier_is_unpriced(
    ctx: ToolContext,
) -> None:
    """Plan S9012-001 publishes no dollar bounds for tier 5 coinsurance. Humira
    stays on tier 5 there, so the change is zero though no amount is known."""
    result = call(
        ctx,
        "estimate_cost_change",
        rxcui="8800331",
        from_month="2026-01",
        to_month="2026-02",
        plan_id="S9012-001",
    )
    assert result.ok and result.data is not None
    assert result.data.before is None and result.data.after is None
    assert result.data.change is not None
    assert (result.data.change.low, result.data.change.high) == (0.0, 0.0)
    assert result.data.priced
    assert "did not change tier" in (result.note or "")


def test_estimate_cost_change_empty_and_errors(ctx: ToolContext) -> None:
    empty_cost = call(
        ctx,
        "estimate_cost_change",
        rxcui="8800191",
        from_month="2026-01",
        to_month="2026-02",
        plan_id="H5678-001",
    )
    assert_empty(empty_cost, "no cost sharing to compare")
    assert empty_cost.data is not None and empty_cost.data.plans == []
    assert empty_cost.data.change is None and not empty_cost.data.priced
    assert_error(
        call(
            ctx, "estimate_cost_change", rxcui="8800151", from_month="2026-01", to_month="2026-02"
        ),
        ErrorCode.INVALID_ARGS,
        "exactly one",
    )
    assert_error(
        call(
            ctx,
            "estimate_cost_change",
            rxcui="8800151",
            from_month="2026-01",
            to_month="2026-05",
            plan_id="H1234-001",
        ),
        ErrorCode.MONTH_NOT_LOADED,
        "2026-05",
    )
    assert_error(
        call(
            ctx,
            "estimate_cost_change",
            rxcui="8800151",
            from_month="2026-01",
            to_month="2026-03",
            plan_id="S9012-003",
        ),
        ErrorCode.UNKNOWN_PLAN,
        "not in both",
    )


# Agreement with the existing diff command


def test_a_replaced_package_code_is_not_a_change(ctx: ToolContext) -> None:
    """Formulary 00026124 swaps Eliquis NDC 02 for NDC 03 in February. The diff
    sees a removal and an addition; at the RXCUI grain nothing changed."""
    rows = all_changes(ctx, from_month="2026-01", to_month="2026-02", rxcuis=["8800141"])
    assert not [r for r in rows if r["formulary_id"] == F2]
    coverage = call(ctx, "get_coverage", rxcui="8800141", month="2026-02", formulary_id=F2)
    assert coverage.data is not None
    assert coverage.data.rows[0].ndcs == ["88000014101", "88000014103"]


def test_a_tier_move_inside_a_package_code_swap_still_shows(ctx: ToolContext) -> None:
    """Formulary 00026124 swaps Januvia's NDC in March and moves it from tier 3
    to tier 4. The NDC diff sees only a removal and an addition."""
    rows = all_changes(
        ctx, from_month="2026-02", to_month="2026-03", rxcuis=["8800151"], formulary_ids=[F2]
    )
    assert [(r["change_type"], r["before_value"], r["after_value"], r["ndcs"]) for r in rows] == [
        ("tier_up", "3", "4", ["88000015101", "88000015102"])
    ]


def test_get_changes_agrees_with_the_diff_command(
    ask_db: Path, ctx: ToolContext, tmp_path: Path
) -> None:
    copy = tmp_path / "copy.db"
    shutil.copy(ask_db, copy)
    out = tmp_path / "diff.json"
    for pair in (("2026-01", "2026-02"), ("2026-02", "2026-03"), ("2026-01", "2026-03")):
        result = runner.invoke(
            app, ["diff", "--from", pair[0], "--to", pair[1], "--db", str(copy), "--json", str(out)]
        )
        assert result.exit_code == 0, result.output
        groups = json.loads(out.read_text())["groups"]
        from_diff = {
            (g["rxcui"], t, p) for g in groups for t in g["change_types"] for p in g["plans"]
        }
        from_tool = {
            (r["rxcui"], r["change_type"], p)
            for r in all_changes(ctx, from_month=pair[0], to_month=pair[1])
            for p in r["plans"]
        }
        assert from_tool == from_diff, pair


# Schemas


def _walk(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, dict):
        return [node] + [d for v in node.values() for d in _walk(v)]
    if isinstance(node, list):
        return [d for v in node for d in _walk(v)]
    return []


_JSON_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


@pytest.mark.parametrize("spec", TOOLS, ids=[s.name for s in TOOLS])
def test_schema_is_clean_json_schema_and_round_trips(spec: Any) -> None:
    schema = clean_schema(spec.input_model)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["required"]) <= set(schema["properties"])
    for node in _walk(schema):
        if "type" in node:
            assert node["type"] in _JSON_TYPES
    for name, prop in schema["properties"].items():
        assert "type" in prop or "enum" in prop, name
    required = {n for n, f in spec.input_model.model_fields.items() if f.is_required()}
    assert set(schema["required"]) == required

    validated = spec.input_model.model_validate(spec.example)
    again = spec.input_model.model_validate(validated.model_dump(mode="json"))
    assert again == validated
    assert json.dumps(spec.example) in spec.description.replace("'", '"') or spec.example == {}


def test_change_type_enum_is_spelled_out(ctx: ToolContext) -> None:
    from rxdelta.types import ChangeType

    tools = {t["name"]: t for t in anthropic_tools()}
    items = tools["get_changes"]["input_schema"]["properties"]["change_types"]["items"]
    assert items["enum"] == [c.value for c in ChangeType]
    assert set(tools) == {s.name for s in TOOLS} and len(tools) == 7
    assert all(set(t) == {"name", "description", "input_schema"} for t in tools.values())


def test_every_example_runs_against_the_fixtures(ctx: ToolContext) -> None:
    for spec in TOOLS:
        result = call_tool(spec.name, spec.example, ctx)
        assert result.ok, (spec.name, result.note)


# CLI


def test_tools_cli(ask_db: Path) -> None:
    listed = runner.invoke(app, ["tools", "list"])
    assert listed.exit_code == 0 and "estimate_cost_change" in listed.output

    schema = runner.invoke(app, ["tools", "schema", "--format", "anthropic"])
    assert schema.exit_code == 0
    assert len(json.loads(schema.output)) == 7
    both = runner.invoke(app, ["tools", "schema", "--format", "json"])
    assert both.exit_code == 0 and "output_schema" in json.loads(both.output)[0]
    bad = runner.invoke(app, ["tools", "schema", "--format", "xml"])
    assert bad.exit_code == 1

    called = runner.invoke(
        app,
        ["tool", "find_drug", "--args", '{"query": "eliquis"}', "--db", str(ask_db), "--pretty"],
    )
    assert called.exit_code == 0, called.output
    assert json.loads(called.output)["data"]["products"][0]["rxcuis"] == ["8800141"]
    broken = runner.invoke(app, ["tool", "find_drug", "--args", "{nope", "--db", str(ask_db)])
    assert broken.exit_code == 1


def test_only_real_failures_are_errors() -> None:
    assert {c.value for c in ErrorCode} == {
        "invalid_args",
        "month_not_loaded",
        "unknown_drug",
        "unknown_plan",
        "internal",
    }


def test_get_coverage_batch_matches_single_calls(ctx: ToolContext) -> None:
    wanted = ["8800101", "8800341", "8800102"]
    result = call(ctx, "get_coverage", rxcuis=wanted, month="2026-03", formulary_id="00026124")
    assert result.ok and result.data is not None and result.data.drugs is not None
    assert [d.rxcui for d in result.data.drugs] == wanted
    assert [d.covered for d in result.data.drugs] == [True, False, True]
    assert result.total == 3
    assert result.note is not None and "1 of 3 drugs are not on formulary 00026124" in result.note
    for drug in result.data.drugs:
        single = call(
            ctx, "get_coverage", rxcui=drug.rxcui, month="2026-03", formulary_id="00026124"
        )
        assert single.data is not None and single.data.drugs is None
        assert drug.rows == single.data.rows
        assert drug.display_name == single.data.display_name
    assert len(result.provenance.row_keys) == sum(
        len(r.ndcs) for d in result.data.drugs for r in d.rows
    )


def test_get_coverage_batch_by_plan_and_its_errors(ctx: ToolContext, config: Config) -> None:
    by_plan = call(ctx, "get_coverage", rxcuis=["8800141"], month="2026-03", plan_id="H1234-001")
    assert by_plan.ok and by_plan.data is not None and by_plan.data.drugs is not None
    assert by_plan.data.formulary_id == F1 and by_plan.data.drugs[0].covered
    for args, message in (
        ({"rxcuis": ["8800141"]}, "rxcuis needs formulary_id or plan_id"),
        ({"rxcui": "8800141", "rxcuis": ["8800141"], "formulary_id": F1}, "exactly one"),
        ({"formulary_id": F1}, "exactly one"),
        ({"rxcuis": [], "formulary_id": F1}, "at least 1"),
    ):
        bad = call(ctx, "get_coverage", month="2026-03", **args)
        assert bad.error is not None and bad.error.code == ErrorCode.INVALID_ARGS
        assert message in bad.error.message
    unknown = call(
        ctx, "get_coverage", rxcuis=["8800141", "99999999"], month="2026-03", formulary_id=F1
    )
    assert unknown.error is not None and unknown.error.code == ErrorCode.UNKNOWN_DRUG
    assert "99999999" in unknown.error.message
    capped = replace(
        config,
        tools=config.tools.model_copy(
            update={"row_caps": {**config.tools.row_caps, "get_coverage_rxcuis": 1}}
        ),
    )
    over = call_tool(
        "get_coverage",
        {"rxcuis": ["8800141", "8800101"], "month": "2026-03", "formulary_id": F1},
        ToolContext(db_path=ctx.db_path, config=capped),
    )
    assert over.error is not None and "At most 1 RXCUIs per call" in over.error.message
