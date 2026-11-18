from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from rxdelta.ask.answer import (
    ChangeAnswer,
    ClarifyAnswer,
    CostRangeAnswer,
    CountAnswer,
    CoverageAnswer,
    DrugListAnswer,
    NoDataAnswer,
)
from rxdelta.config import CATEGORIES, Config
from rxdelta.evals import builder, gold
from rxdelta.evals.items import EvalItem, manifest_path, read_handwritten, read_items, sha256_file
from rxdelta.names.match import DrugMatcher
from rxdelta.tools.context import ToolContext
from rxdelta.tools.registry import call_tool
from tests.conftest import EVAL_SET, HANDWRITTEN, REPO

F1, F2, F3 = "00026123", "00026124", "00026125"
PAIRS = (("2026-01", "2026-02"), ("2026-02", "2026-03"), ("2026-01", "2026-03"))


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build(db: Path, out: Path, config: Config, seed: int = 7, n: int = 200) -> list[EvalItem]:
    builder.build_file(
        db,
        config,
        month_from="2026-01",
        month_to="2026-03",
        n=n,
        seed=seed,
        out=out,
        handwritten_path=HANDWRITTEN,
    )
    return read_items(out)


@pytest.fixture(scope="module")
def built(ask_db: Path, tmp_path_factory: pytest.TempPathFactory) -> list[EvalItem]:
    from rxdelta.config import load_config
    from tests.conftest import CONFIG_PATH

    out = tmp_path_factory.mktemp("set") / "set.jsonl"
    return build(ask_db, out, load_config(CONFIG_PATH))


# Independence


def test_gold_module_does_not_import_the_tool_layer() -> None:
    tree = ast.parse((REPO / "rxdelta" / "evals" / "gold.py").read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any(m.startswith(("rxdelta.tools", "rxdelta.diff")) for m in imported), imported

    loaded = (
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys, rxdelta.evals.gold; "
                "print(','.join(m for m in sys.modules if m.startswith('rxdelta')))",
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO,
        )
        .stdout.strip()
        .split(",")
    )
    assert not [m for m in loaded if m.startswith(("rxdelta.tools", "rxdelta.diff"))], loaded


# Gold and tools agree


@pytest.fixture
def ctx(ask_db: Path, config: Config) -> ToolContext:
    return ToolContext(db_path=ask_db, config=config)


def tool_rows(ctx: ToolContext, **filters: Any) -> list[Any]:
    rows: list[Any] = []
    while True:
        result = call_tool("get_changes", {"limit": 100, "offset": len(rows), **filters}, ctx)
        if not result.ok:
            return rows
        assert result.data is not None
        rows += result.data.rows
        if len(rows) >= (result.total or 0):
            return rows


@pytest.mark.parametrize("pair", PAIRS)
def test_gold_changes_agree_with_get_changes(
    ask_db: Path, ctx: ToolContext, pair: tuple[str, str]
) -> None:
    conn = open_ro(ask_db)
    expected = {
        (
            c.rxcui,
            c.formulary_from,
            c.formulary_to,
            c.change_type.value,
            c.before,
            c.after,
            len(c.plans),
        )
        for c in gold.changes(conn, *pair)
    }
    got = {
        (
            r.rxcui,
            r.formulary_id_from,
            r.formulary_id,
            r.change_type.value,
            r.before_value,
            r.after_value,
            r.affected_plans,
        )
        for r in tool_rows(ctx, from_month=pair[0], to_month=pair[1])
    }
    assert got == expected


@pytest.mark.parametrize("pair", PAIRS)
def test_gold_counts_agree_with_count_changes(
    ask_db: Path, ctx: ToolContext, pair: tuple[str, str]
) -> None:
    conn = open_ro(ask_db)
    for change_type in sorted({c.change_type.value for c in gold.changes(conn, *pair)}):
        for formulary in (None, F1, F2, F3):
            args: dict[str, Any] = {"from": pair[0], "to": pair[1], "change_types": [change_type]}
            tool_args: dict[str, Any] = {
                "from_month": pair[0],
                "to_month": pair[1],
                "change_types": [change_type],
            }
            if formulary:
                args["formulary_id"] = formulary
                tool_args["formulary_ids"] = [formulary]
            expected = gold.count_drugs(conn, args)
            result = call_tool("count_changes", tool_args, ctx)
            if isinstance(expected, NoDataAnswer):
                assert not result.ok
            else:
                assert isinstance(expected, CountAnswer)
                assert result.data is not None
                assert result.data.totals.drugs == expected.count, (change_type, formulary)


def test_gold_coverage_agrees_with_get_coverage(ask_db: Path, ctx: ToolContext) -> None:
    conn = open_ro(ask_db)
    rxcuis = [str(r[0]) for r in conn.execute("SELECT DISTINCT rxcui FROM formulary")]
    checked = 0
    for month in ("2026-01", "2026-02", "2026-03"):
        for formulary in (F1, F2, F3):
            for rxcui in rxcuis:
                expected = gold.coverage(conn, rxcui, month, formulary_id=formulary)
                result = call_tool(
                    "get_coverage",
                    {"rxcui": rxcui, "month": month, "formulary_id": formulary},
                    ctx,
                )
                if isinstance(expected, NoDataAnswer):
                    assert result.error is not None and result.error.code == "no_results"
                    continue
                assert isinstance(expected, CoverageAnswer) and result.data is not None
                (row,) = result.data.rows
                got = row.coverage
                assert (
                    got.tier,
                    got.prior_auth,
                    got.step_therapy,
                    got.quantity_limit,
                    got.quantity_limit_amount,
                    got.quantity_limit_days,
                ) == (
                    expected.tier,
                    expected.prior_auth,
                    expected.step_therapy,
                    expected.quantity_limit,
                    expected.quantity_limit_amount,
                    expected.quantity_limit_days,
                )
                checked += 1
    assert checked > 300


def test_gold_cost_agrees_with_estimate_cost_change(
    ask_db: Path, ctx: ToolContext, config: Config
) -> None:
    conn = open_ro(ask_db)
    plans = [
        f"{r[0]}-{r[1]}"
        for r in conn.execute(
            "SELECT DISTINCT contract_id, plan_id FROM plan_info WHERE snapshot_month = '2026-01'"
        )
    ]
    checked = 0
    for pair in PAIRS:
        moved = {
            c.rxcui for c in gold.changes(conn, *pair) if c.change_type.value.startswith("tier")
        }
        for rxcui in sorted(moved | {"8800141", "8800271"}):
            for plan in plans:
                args = {"rxcui": rxcui, "from": pair[0], "to": pair[1], "plan_id": plan}
                try:
                    expected = gold.cost_change(conn, config, args)
                except gold.GoldError:
                    continue
                result = call_tool(
                    "estimate_cost_change",
                    {"rxcui": rxcui, "from_month": pair[0], "to_month": pair[1], "plan_id": plan},
                    ctx,
                )
                if isinstance(expected, NoDataAnswer):
                    assert not result.ok
                    continue
                assert isinstance(expected, CostRangeAnswer)
                assert result.data is not None and result.data.change is not None
                tolerance = config.eval.cost_tolerance
                assert abs(result.data.change.low - expected.low) <= tolerance, args
                assert abs(result.data.change.high - expected.high) <= tolerance, args
                checked += 1
    assert checked > 30


def test_gold_class_members_agree_with_find_drug(ask_db: Path, ctx: ToolContext) -> None:
    conn = open_ro(ask_db)
    for token in ("insulin", "semaglutide", "tirzepatide"):
        result = call_tool("find_drug", {"query": token, "limit": 25}, ctx)
        assert result.data is not None
        found = {r for p in result.data.products for r in p.rxcuis}
        assert found == set(gold.rxcuis_with_token(conn, token)), token


# The built set


def test_build_is_byte_identical_for_the_same_inputs(
    ask_db: Path, config: Config, tmp_path: Path
) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    build(ask_db, first, config)
    build(ask_db, second, config)
    assert first.read_bytes() == second.read_bytes()
    a = json.loads(manifest_path(first).read_text())
    b = json.loads(manifest_path(second).read_text())
    names = ("file", "eval_set")
    assert {k: v for k, v in a.items() if k not in names} == {
        k: v for k, v in b.items() if k not in names
    }
    other = tmp_path / "c.jsonl"
    build(ask_db, other, config, seed=8)
    assert other.read_bytes() != first.read_bytes()


def test_committed_eval_set_matches_a_fresh_build(
    eval_db: Path, config: Config, tmp_path: Path
) -> None:
    out = tmp_path / "rxdelta_v1.jsonl"
    build(eval_db, out, config)
    assert out.read_bytes() == EVAL_SET.read_bytes(), "rebuild with: make evalset"
    committed = json.loads(manifest_path(EVAL_SET).read_text())
    fresh = json.loads(manifest_path(out).read_text())
    assert fresh == committed
    assert committed["file_sha256"] == sha256_file(EVAL_SET)


def test_category_mix_and_splits(built: list[EvalItem], config: Config) -> None:
    generated = Counter(i.category for i in built if i.source == "generated")
    quotas = builder._quotas(config, 200)
    assert dict(generated) == {c: q for c, q in quotas.items() if q}
    assert sum(generated.values()) == 200
    for category in CATEGORIES:
        members = [i for i in built if i.category == category]
        dev = sum(1 for i in members if i.split == "dev")
        assert dev == round(config.evalset.splits.dev * len(members)), category


def test_no_near_duplicates(built: list[EvalItem]) -> None:
    keys = [
        i.template_id
        + json.dumps(
            {k: (v.lower() if isinstance(v, str) else v) for k, v in i.gold.args.items()},
            sort_keys=True,
        )
        for i in built
    ]
    assert len(keys) == len(set(keys))
    assert len({i.id for i in built}) == len(built)


def test_every_category_uses_at_least_four_templates(built: list[EvalItem]) -> None:
    for category in CATEGORIES:
        if category == "typo":
            continue
        templates = {
            i.template_id for i in built if i.category == category and i.source == "generated"
        }
        assert len(templates) >= 4, (category, templates)


def test_expected_kinds_match_categories(built: list[EvalItem]) -> None:
    kinds: dict[str, set[type[Any]]] = {
        "coverage_lookup": {CoverageAnswer},
        "drug_change": {ChangeAnswer},
        "filtered_list": {DrugListAnswer},
        "count": {CountAnswer},
        "cost": {CostRangeAnswer},
        "multi_step": {DrugListAnswer},
        "no_answer": {NoDataAnswer},
        "ambiguous": {ClarifyAnswer},
    }
    for item in built:
        if item.source == "generated" and item.category in kinds:
            assert type(item.expected) in kinds[item.category], item.id
        if item.category == "out_of_scope":
            assert item.expected.kind == "refusal"
        if item.category == "ambiguous":
            assert item.alternative is not None


def test_no_answer_items_include_answerable_looking_ones(built: list[EvalItem]) -> None:
    reasons = Counter(
        i.expected.reason.value
        for i in built
        if i.category == "no_answer" and isinstance(i.expected, NoDataAnswer)
    )
    assert set(reasons) == {
        "no_matching_changes",
        "not_on_formulary",
        "unknown_drug",
        "month_not_loaded",
    }
    assert reasons["no_matching_changes"] >= max(reasons.values()) * 0.5


def test_typo_items_still_resolve_to_the_intended_drug(
    built: list[EvalItem], ask_conn: sqlite3.Connection, config: Config
) -> None:
    matcher = DrugMatcher(ask_conn, config.match)
    typos = [i for i in built if i.category == "typo"]
    assert typos
    for item in typos:
        mention = str(item.gold.args["drug"])
        assert mention.split()[0].lower() in item.question.lower()
        resolved = {m.rxcui for m in matcher.match_all(mention)}
        intended = set(item.gold.args.get("rxcuis") or [item.gold.args["rxcui"]])
        assert resolved == intended, item.id
        assert item.difficulty in ("medium", "hard")


def test_id_mentions_are_used_in_a_small_share(built: list[EvalItem]) -> None:
    drugs = [str(i.gold.args["drug"]) for i in built if "drug" in i.gold.args]
    by_id = [d for d in drugs if d.upper().startswith(("RXCUI", "NDC"))]
    assert 0 < len(by_id) < 0.25 * len(drugs)


def test_context_never_carries_gold(built: list[EvalItem]) -> None:
    for item in built:
        assert set(item.context.model_dump()) == {"default_from", "default_to"}


def test_handwritten_items_are_merged_with_gold_answers(built: list[EvalItem]) -> None:
    hand = [i for i in built if i.source == "handwritten"]
    assert len(hand) == len(read_handwritten(HANDWRITTEN)) == 20
    by_id = {i.template_id.split(".")[-1]: i for i in hand}
    wrong_premise = by_id["h02"].expected
    assert isinstance(wrong_premise, ChangeAnswer)
    assert {(x.change_type.value, x.before, x.after) for x in wrong_premise.items} == {
        ("tier_down", "3", "2"),
        ("prior_auth_added", "no", "yes"),
    }
    assert isinstance(by_id["h03"].expected, NoDataAnswer)
    latest = by_id["h04"]
    assert "list_months" in latest.required_tools
    assert isinstance(by_id["h17"].expected, DrugListAnswer)
    assert by_id["h17"].expected.rxcuis == ["8800121"]


def test_difficulty_rules(config: Config) -> None:
    easy = CountAnswer(count=3)
    assert builder.difficulty(config, ["count_changes"], easy, False) == "easy"
    assert builder.difficulty(config, ["find_drug", "get_changes"], easy, False) == "medium"
    assert builder.difficulty(config, ["a", "b", "c"], easy, False) == "hard"
    assert builder.difficulty(config, ["count_changes"], easy, True) == "medium"
    long_list = DrugListAnswer(rxcuis=[str(i) for i in range(6)])
    assert builder.difficulty(config, ["get_changes"], long_list, False) == "hard"


def test_build_refuses_months_that_are_not_loaded(
    ask_db: Path, config: Config, tmp_path: Path
) -> None:
    with pytest.raises(builder.BuildError, match="not loaded"):
        builder.build_file(
            ask_db,
            config,
            month_from="2025-01",
            month_to="2026-03",
            n=10,
            seed=1,
            out=tmp_path / "x.jsonl",
            handwritten_path=None,
        )


def test_evalset_cli(eval_db: Path, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from rxdelta.cli import app

    out = tmp_path / "cli.jsonl"
    result = CliRunner().invoke(
        app,
        [
            "evalset",
            "build",
            "--db",
            str(eval_db),
            "--from",
            "2026-01",
            "--to",
            "2026-03",
            "--n",
            "40",
            "--seed",
            "3",
            "--out",
            str(out),
            "--handwritten",
            str(HANDWRITTEN),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(read_items(out)) == 60
    assert manifest_path(out).is_file()
