from __future__ import annotations

import json
import shutil
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

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
    RunnerOutput,
    StructuredAnswer,
    ToolCallRecord,
)
from rxdelta.cli import app
from rxdelta.config import Category, Config
from rxdelta.evals import builder, harness
from rxdelta.evals.items import EvalItem, GoldSpec, manifest_path, read_items
from rxdelta.evals.runners import RUNNERS
from rxdelta.evals.score import score_item, set_scores
from rxdelta.types import ChangeType
from tests.conftest import EVAL_SET, HANDWRITTEN
from tests.noisy_runner import Corruption, NoisyRunner

runner = CliRunner()


@pytest.fixture(scope="module")
def eval_set(ask_db: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    from rxdelta.config import load_config
    from tests.conftest import CONFIG_PATH

    out = tmp_path_factory.mktemp("set") / "set.jsonl"
    builder.build_file(
        ask_db,
        load_config(CONFIG_PATH),
        month_from="2026-01",
        month_to="2026-03",
        n=200,
        seed=7,
        out=out,
        handwritten_path=HANDWRITTEN,
    )
    return out


def run(
    eval_set: Path,
    ask_db: Path,
    config: Config,
    tmp_path: Path,
    name: str = "oracle",
    **kwargs: Any,
) -> harness.RunResult:
    return harness.run(
        set_path=eval_set,
        runner_name=name,
        split=kwargs.pop("split", "all"),
        db_path=ask_db,
        config=config,
        out_root=tmp_path / "runs",
        **kwargs,
    )


@pytest.fixture(scope="module")
def oracle_summary(
    eval_set: Path, ask_db: Path, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    from rxdelta.config import load_config
    from tests.conftest import CONFIG_PATH

    result = run(eval_set, ask_db, load_config(CONFIG_PATH), tmp_path_factory.mktemp("o"))
    return result.summary


# Oracle and null


def test_oracle_scores_100_percent_on_every_metric(oracle_summary: dict[str, Any]) -> None:
    overall = oracle_summary["overall"]
    assert overall["accuracy"] == 1.0
    assert overall["mean_f1_drug_list"] == 1.0
    assert overall["false_refusals"] == 0 and overall["missed_refusals"] == 0
    assert overall["no_data_reason_match_rate"] == 1.0
    assert overall["secondary_match_rate"] == 1.0
    assert overall["errors"] == 0
    assert all(row["accuracy"] == 1.0 for row in oracle_summary["by_category"].values())
    assert oracle_summary["tools"]["required_called_rate"] == 1.0
    assert oracle_summary["tools"]["invalid_args_calls"] == 0
    assert oracle_summary["tools"]["repeated_calls"] == 0
    assert oracle_summary["grounding"]["items_with_ungrounded"] == 0
    assert oracle_summary["gates"]["passed"]
    for expected, row in oracle_summary["kind_confusion"].items():
        assert set(row) == {expected}


def test_oracle_scores_100_percent_on_the_committed_set(
    eval_db: Path, config: Config, tmp_path: Path
) -> None:
    result = run(EVAL_SET, eval_db, config, tmp_path)
    assert result.summary["overall"]["accuracy"] == 1.0
    assert result.summary["grounding"]["items_with_ungrounded"] == 0


def test_null_scores_the_floor_predicted_by_the_mix(
    eval_set: Path, ask_db: Path, config: Config, tmp_path: Path
) -> None:
    items = read_items(eval_set)
    no_data = sum(1 for i in items if i.expected.kind == "no_data")
    answerable = sum(1 for i in items if i.expected.kind not in ("no_data", "refusal"))
    refusals = sum(1 for i in items if i.expected.kind == "refusal")

    result = run(eval_set, ask_db, config, tmp_path, name="null")
    overall = result.summary["overall"]
    assert overall["correct"] == no_data
    assert overall["accuracy"] == round(no_data / len(items), 4)
    assert overall["false_refusals"] == answerable
    assert overall["missed_refusals"] == refusals
    assert result.summary["by_category"]["no_answer"]["accuracy"] == 1.0
    for category, row in result.summary["by_category"].items():
        if category != "no_answer":
            assert row["accuracy"] == 0.0, category
    assert result.summary["tools"]["mean_calls"] == 0.0
    assert not result.passed


# Noisy runner: each corruption moves its metric and only its metric

_GROUPS = ("accuracy", "mean_f1", "count_accuracy", "grounding")


def _metrics(summary: dict[str, Any]) -> dict[str, Any]:
    by_category = summary["by_category"]
    return {
        "accuracy": {c: r["accuracy"] for c, r in by_category.items() if c != "count"},
        "mean_f1": summary["overall"]["mean_f1_drug_list"],
        "count_accuracy": by_category["count"]["accuracy"],
        "grounding": summary["grounding"]["items_with_ungrounded"],
    }


@pytest.mark.parametrize(
    ("corruption", "moved"),
    [
        ("drop_item", {"accuracy", "mean_f1"}),
        ("fake_rxcui", {"accuracy", "mean_f1"}),
        ("off_by_one", {"count_accuracy"}),
        ("fake_number", {"grounding"}),
    ],
)
def test_noisy_runner_moves_the_right_metric(
    eval_set: Path,
    ask_db: Path,
    config: Config,
    tmp_path: Path,
    oracle_summary: dict[str, Any],
    corruption: Corruption,
    moved: set[str],
) -> None:
    result = harness.run(
        set_path=eval_set,
        runner_name="noisy",
        split="all",
        db_path=ask_db,
        config=config,
        out_root=tmp_path,
        runner_factory=partial(NoisyRunner, corruption=corruption),
    )
    before, after = _metrics(oracle_summary), _metrics(result.summary)
    for group in _GROUPS:
        if group in moved:
            assert after[group] != before[group], group
        else:
            assert after[group] == before[group], group
    if corruption == "fake_number":
        assert result.summary["overall"]["accuracy"] == 1.0
        assert result.summary["grounding"]["share_with_ungrounded"] == 1.0
    else:
        assert result.summary["overall"]["accuracy"] < 1.0


def test_noisy_rate_is_seeded(eval_set: Path, ask_db: Path, config: Config, tmp_path: Path) -> None:
    def once(seed: int) -> float:
        result = harness.run(
            set_path=eval_set,
            runner_name="noisy",
            split="all",
            db_path=ask_db,
            config=config,
            out_root=tmp_path,
            runner_factory=partial(NoisyRunner, corruption="fake_number", rate=0.5, seed=seed),
        )
        return float(result.summary["grounding"]["share_with_ungrounded"])

    assert once(1) == once(1)
    assert 0.3 < once(1) < 0.7


# Scorer, every kind and the edge cases


def _item(
    expected: StructuredAnswer,
    *,
    category: Category = "filtered_list",
    alternative: ChangeAnswer | None = None,
    tools: list[str] | None = None,
) -> EvalItem:
    return EvalItem(
        id="t-1",
        category=category,
        template_id="t",
        question="Which drugs moved in 2026?",
        expected=expected,
        alternative=alternative,
        required_tools=tools or [],
        gold_query="q()",
        gold=GoldSpec(query="q"),
        difficulty="easy",
        split="dev",
        source="generated",
        seed=1,
    )


def _out(
    answer: StructuredAnswer,
    text: str = "",
    calls: list[ToolCallRecord] | None = None,
    error: str | None = None,
) -> RunnerOutput:
    return RunnerOutput(
        answer=answer, final_text=text, tool_calls=calls or [], latency_ms=1, error=error
    )


def test_set_scores_edge_cases() -> None:
    assert set_scores([], []) == (1.0, 1.0, 1.0)
    assert set_scores(["1"], []) == (0.0, 0.0, 0.0)
    assert set_scores([], ["1"]) == (0.0, 0.0, 0.0)
    p, r, f = set_scores(["1", "2"], ["1", "1", "3"])
    assert (p, r) == (0.5, 0.5) and f == 0.5


def test_drug_list_scoring(config: Config) -> None:
    item = _item(DrugListAnswer(rxcuis=["1", "2"]))
    exact = score_item(item, _out(DrugListAnswer(rxcuis=["2", "1", "2"])), config)
    assert exact.correct and exact.f1 == 1.0
    partial_list = score_item(item, _out(DrugListAnswer(rxcuis=["1"])), config)
    assert not partial_list.correct and partial_list.recall == 0.5 and partial_list.precision == 1.0
    empty_expected = _item(DrugListAnswer(rxcuis=[]))
    assert score_item(empty_expected, _out(DrugListAnswer(rxcuis=[])), config).correct
    tiers = _item(DrugListAnswer(rxcuis=["1"], tiers={"1": 3}), category="multi_step")
    wrong_tier = score_item(tiers, _out(DrugListAnswer(rxcuis=["1"], tiers={"1": 4})), config)
    assert wrong_tier.correct and wrong_tier.secondary_match is False


def test_kind_mismatch_is_always_wrong(config: Config) -> None:
    item = _item(CountAnswer(count=3), category="count")
    result = score_item(item, _out(DrugListAnswer(rxcuis=["1", "2", "3"])), config)
    assert not result.correct and not result.kind_match
    assert (result.expected_kind, result.got_kind) == ("count", "drug_list")


def test_count_change_coverage_and_cost_scoring(config: Config) -> None:
    assert score_item(_item(CountAnswer(count=3)), _out(CountAnswer(count=3)), config).correct
    assert not score_item(_item(CountAnswer(count=3)), _out(CountAnswer(count=4)), config).correct

    change = ChangeAnswer(
        rxcuis=["1"],
        items=[
            ChangeItem(
                rxcui="1", formulary_id="F", change_type=ChangeType.TIER_UP, before="3", after="4"
            )
        ],
    )
    wrong_after = ChangeAnswer(
        rxcuis=["1"],
        items=[
            ChangeItem(
                rxcui="1", formulary_id="F", change_type=ChangeType.TIER_UP, before="3", after="5"
            )
        ],
    )
    assert score_item(_item(change), _out(change), config).correct
    assert not score_item(_item(change), _out(wrong_after), config).correct

    coverage = CoverageAnswer(
        rxcui="1",
        month="2026-01",
        formulary_id="F",
        tier=2,
        prior_auth=False,
        step_therapy=False,
        quantity_limit=True,
        quantity_limit_amount=30.0,
        quantity_limit_days=30,
    )
    other_limit = coverage.model_copy(update={"quantity_limit_amount": 60.0})
    assert score_item(_item(coverage), _out(coverage), config).correct
    assert not score_item(_item(coverage), _out(other_limit), config).correct

    cost = CostRangeAnswer(rxcui="1", low=-5.0, high=10.0)
    close = CostRangeAnswer(rxcui="1", low=-5.005, high=10.009)
    far = CostRangeAnswer(rxcui="1", low=-5.0, high=10.5)
    assert score_item(_item(cost), _out(close), config).correct
    assert not score_item(_item(cost), _out(far), config).correct


def test_no_data_refusal_and_false_or_missed_refusals(config: Config) -> None:
    item = _item(NoDataAnswer(reason=NoDataReason.not_on_formulary), category="no_answer")
    other_reason = score_item(item, _out(NoDataAnswer(reason=NoDataReason.unknown_drug)), config)
    assert other_reason.correct and other_reason.reason_match is False

    refusal = _item(RefusalAnswer(), category="out_of_scope")
    assert score_item(refusal, _out(RefusalAnswer()), config).correct
    missed = score_item(refusal, _out(DrugListAnswer(rxcuis=["1"])), config)
    assert missed.missed_refusal and not missed.correct

    answerable = _item(CountAnswer(count=2), category="count")
    false_refusal = score_item(answerable, _out(RefusalAnswer()), config)
    assert false_refusal.false_refusal and false_refusal.answerable
    gave_up = score_item(answerable, _out(NoDataAnswer(reason=NoDataReason.unknown_drug)), config)
    assert gave_up.false_refusal


def test_clarify_accepts_asking_or_answering_for_every_candidate(config: Config) -> None:
    clarify = ClarifyAnswer(candidates=[ClarifyCandidate(product="Ozempic", rxcuis=["1", "2"])])
    full = ChangeAnswer(
        rxcuis=["1", "2"],
        items=[
            ChangeItem(
                rxcui="1", formulary_id="F", change_type=ChangeType.TIER_UP, before="3", after="4"
            )
        ],
    )
    item = _item(clarify, category="ambiguous", alternative=full)

    asked = score_item(item, _out(clarify), config)
    assert asked.correct and asked.clarify_path == "asked"
    narrow = ClarifyAnswer(candidates=[ClarifyCandidate(product="Ozempic", rxcuis=["1"])])
    assert not score_item(item, _out(narrow), config).correct

    answered = score_item(item, _out(full, "Both Ozempic strengths: ..."), config)
    assert answered.correct and answered.clarify_path == "answered_all"
    unsaid = score_item(item, _out(full, "Here is the change."), config)
    assert not unsaid.correct and unsaid.clarify_path == "missed"


def test_a_runner_error_is_never_correct(config: Config) -> None:
    item = _item(NoDataAnswer(reason=NoDataReason.unknown_drug), category="no_answer")
    result = score_item(
        item, _out(NoDataAnswer(reason=NoDataReason.unknown_drug), error="boom"), config
    )
    assert not result.correct and result.error == "boom"


def test_tool_metrics(config: Config) -> None:
    def call(name: str, args: dict[str, Any], code: str | None = None) -> ToolCallRecord:
        result = {"ok": code is None, "error": {"code": code} if code else None}
        return ToolCallRecord(name=name, args=args, result=result, duration_ms=1)

    item = _item(CountAnswer(count=1), tools=["find_drug", "count_changes"])
    calls = [
        call("find_drug", {"query": "x"}),
        call("find_drug", {"query": "x"}),
        call("get_changes", {"bad": 1}, "invalid_args"),
    ]
    result = score_item(item, _out(CountAnswer(count=1), calls=calls), config)
    assert result.tool_calls == 3 and result.repeated_calls == 1 and result.invalid_args_calls == 1
    assert not result.required_tools_called and result.missing_tools == ["count_changes"]


# Manifest, database and compare guards


def test_a_set_that_does_not_match_its_manifest_is_refused(
    eval_set: Path, ask_db: Path, config: Config, tmp_path: Path
) -> None:
    copy = tmp_path / "set.jsonl"
    shutil.copy(eval_set, copy)
    shutil.copy(manifest_path(eval_set), manifest_path(copy))
    lines = copy.read_text().splitlines()
    copy.write_text("\n".join(lines[:-1]) + "\n")
    with pytest.raises(harness.EvalError, match="does not match its manifest"):
        run(copy, ask_db, config, tmp_path)
    manifest_path(copy).unlink()
    with pytest.raises(harness.EvalError, match="No manifest"):
        run(copy, ask_db, config, tmp_path)


def test_a_different_database_is_refused(
    eval_set: Path, eval_db: Path, config: Config, tmp_path: Path
) -> None:
    with pytest.raises(harness.EvalError, match="is not the database"):
        run(eval_set, eval_db, config, tmp_path)


def test_compare_reports_flips_and_refuses_mismatched_sets(
    eval_set: Path, ask_db: Path, config: Config, tmp_path: Path
) -> None:
    good = run(eval_set, ask_db, config, tmp_path)
    bad = harness.run(
        set_path=eval_set,
        runner_name="noisy",
        split="all",
        db_path=ask_db,
        config=config,
        out_root=tmp_path / "runs",
        runner_factory=partial(NoisyRunner, corruption="off_by_one"),
    )
    comparison = harness.compare(good.directory, bad.directory)
    counts = [i.id for i in read_items(eval_set) if i.expected.kind == "count"]
    assert sorted(comparison.right_to_wrong) == sorted(counts)
    assert comparison.wrong_to_right == []
    assert comparison.by_category["count"]["delta"] == -1.0
    assert comparison.by_category["cost"]["delta"] == 0.0

    summary = json.loads((bad.directory / "summary.json").read_text())
    summary["eval_set_sha256"] = "0" * 64
    (bad.directory / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(harness.EvalError, match="different eval sets"):
        harness.compare(good.directory, bad.directory)


def test_results_are_written_in_item_order_whatever_the_worker_count(
    eval_set: Path, ask_db: Path, config: Config, tmp_path: Path
) -> None:
    def ids(workers: int) -> list[str]:
        result = run(eval_set, ask_db, config, tmp_path, workers=workers)
        lines = (result.directory / "results.jsonl").read_text().splitlines()
        return [json.loads(line)["id"] for line in lines]

    expected = [i.id for i in read_items(eval_set)]
    assert ids(1) == expected
    assert ids(8) == expected


def test_run_outputs(eval_set: Path, ask_db: Path, config: Config, tmp_path: Path) -> None:
    result = run(eval_set, ask_db, config, tmp_path, split="dev", category="count", limit=5)
    files = {p.name for p in result.directory.iterdir()}
    assert files == {"results.jsonl", "summary.json", "summary.md", "report.html", "run.json"}
    assert result.directory.parent == tmp_path / "runs"
    assert result.directory.name.endswith("_oracle_dev")
    summary = json.loads((result.directory / "summary.json").read_text())
    for key in (
        "eval_set_sha256",
        "git_commit",
        "config_sha256",
        "kind_confusion",
        "tools",
        "grounding",
        "cost_speed",
        "by_category",
    ):
        assert key in summary
    assert summary["overall"]["items"] == 5
    assert set(summary["cost_speed"]["latency_ms"]) == {"total", "mean", "p50", "p95"}
    page = (result.directory / "report.html").read_text()
    assert page.startswith("<!DOCTYPE html>")
    for heading in ("Headline", "By category", "Answer kind confusion", "Worst items"):
        assert heading in page
    for marker in ("http://", "https://", "<script"):
        assert marker not in page
    assert "| accuracy | 100.0% (5 of 5) |" in (result.directory / "summary.md").read_text()


def test_the_report_lists_failures(
    eval_set: Path, ask_db: Path, config: Config, tmp_path: Path
) -> None:
    result = run(eval_set, ask_db, config, tmp_path, name="null", category="count")
    page = (result.directory / "report.html").read_text()
    assert "Runner error" not in page
    assert page.count('class="item"') == min(
        config.eval.worst_items, result.summary["overall"]["items"]
    )


def test_percentile() -> None:
    assert harness.percentile([], 0.95) == 0.0
    assert harness.percentile([5.0], 0.5) == 5.0
    values = [float(v) for v in range(1, 101)]
    assert harness.percentile(values, 0.50) == 50.0
    assert harness.percentile(values, 0.95) == 95.0


def test_runner_registry_has_oracle_null_and_agent() -> None:
    assert set(RUNNERS) == {"oracle", "null", "agent"}


# CLI


def test_eval_cli_run_and_compare(eval_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "runs"
    oracle = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--set",
            str(EVAL_SET),
            "--runner",
            "oracle",
            "--split",
            "dev",
            "--db",
            str(eval_db),
            "--out-dir",
            str(out),
            "--min-accuracy",
            "1.0",
        ],
    )
    assert oracle.exit_code == 0, oracle.output
    assert "Accuracy 100.0%" in oracle.output
    null = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--set",
            str(EVAL_SET),
            "--runner",
            "null",
            "--split",
            "dev",
            "--db",
            str(eval_db),
            "--out-dir",
            str(out),
        ],
    )
    assert null.exit_code == 1
    first, second = sorted(out.iterdir())
    compared = runner.invoke(app, ["eval", "compare", str(first), str(second)])
    assert compared.exit_code == 0, compared.output
    assert "Right to wrong" in compared.output

    noisy = runner.invoke(
        app,
        [
            "eval",
            "run",
            "--set",
            str(EVAL_SET),
            "--runner",
            "noisy",
            "--db",
            str(eval_db),
            "--out-dir",
            str(out),
        ],
    )
    assert noisy.exit_code == 1 and "No runner named 'noisy'" in noisy.output
    bad_split = runner.invoke(
        app, ["eval", "run", "--set", str(EVAL_SET), "--split", "train", "--db", str(eval_db)]
    )
    assert bad_split.exit_code == 1


def test_over_cap_scoring_paths(config: Config) -> None:
    full = DrugListAnswer(rxcuis=["1", "2", "3"], total=3)
    item = _item(CountAnswer(count=3), category="over_cap").model_copy(update={"alternative": full})

    def path(got: StructuredAnswer) -> tuple[bool, str | None]:
        result = score_item(item, _out(got), config)
        return result.correct, result.over_cap_path

    assert path(CountAnswer(count=3)) == (True, "count")
    assert path(CountAnswer(count=4)) == (False, "missed")
    assert path(DrugListAnswer(rxcuis=["1"], truncated=True, total=3)) == (True, "truncated_list")
    # A partial list must say it is partial, state the right total and hold no stranger.
    assert path(DrugListAnswer(rxcuis=["1"], truncated=True, total=2)) == (False, "missed")
    assert path(DrugListAnswer(rxcuis=["1", "9"], truncated=True, total=3)) == (False, "missed")
    assert path(DrugListAnswer(rxcuis=["1"])) == (False, "missed")
    assert path(DrugListAnswer(rxcuis=["3", "2", "1"])) == (True, "full_list")
    assert path(NoDataAnswer(reason=NoDataReason.no_matching_changes)) == (False, "missed")
