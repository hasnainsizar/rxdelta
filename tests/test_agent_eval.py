from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rxdelta import cli
from rxdelta.agent.budget import BudgetError, Ledger, SpendGuard, check, estimate
from rxdelta.agent.client import ModelRequest, ModelResponse, Usage
from rxdelta.agent.loop import AgentRunner
from rxdelta.config import Config, load_config
from rxdelta.evals import harness
from tests.agent_script import FIXTURE_SET, submit, turn
from tests.conftest import CONFIG_PATH

RIGHT = {"kind": "refusal", "reason": "medical_advice"}
WRONG = {"kind": "no_data", "reason": "no_matching_changes"}


class PatternClient:
    """Answers each question right or wrong by how many times it has been asked."""

    def __init__(self, patterns: list[list[bool]], usage: Usage | None = None) -> None:
        self._patterns = patterns
        self._usage = usage or Usage(input_tokens=1000, output_tokens=100)
        self._order: list[str] = []
        self._asked: dict[str, int] = defaultdict(int)

    def create(self, request: ModelRequest) -> ModelResponse:
        question = str(request.messages[0]["content"])
        if question not in self._order:
            self._order.append(question)
        pattern = self._patterns[self._order.index(question)]
        right = pattern[self._asked[question] % len(pattern)]
        self._asked[question] += 1
        return turn(submit(RIGHT if right else WRONG, "See a pharmacist."), usage=self._usage)


def _run(
    db: Path,
    config: Config,
    out: Path,
    client: Any,
    *,
    limit: int = 3,
    options: harness.RunOptions | None = None,
) -> harness.RunResult:
    return harness.run(
        set_path=FIXTURE_SET,
        runner_name="agent",
        split="all",
        db_path=db,
        config=config,
        out_root=out,
        category="out_of_scope",
        limit=limit,
        workers=1,
        runner_factory=lambda items, ctx: AgentRunner(items, ctx, client=client),
        options=options,
    )


def test_repeats_report_the_spread_and_consistency(
    eval_db: Path, config: Config, tmp_path: Path
) -> None:
    client = PatternClient([[True, True, True], [True, False, True], [False, False, False]])
    result = _run(eval_db, config, tmp_path, client, options=harness.RunOptions(repeats=3))
    reps = result.summary["repeats"]
    assert reps["accuracy_by_repeat"] == [0.6667, 0.3333, 0.6667]
    assert reps["mean"] == pytest.approx(0.5556, abs=1e-4)
    assert (reps["min"], reps["max"]) == (0.3333, 0.6667)
    assert reps["stdev"] == pytest.approx(0.1925, abs=1e-4)
    assert (
        reps["items_right_every_repeat"],
        reps["items_right_some_repeats"],
        reps["items_right_no_repeat"],
    ) == (1, 1, 1)
    assert result.summary["overall"]["items"] == 9
    lines = (result.directory / "results.jsonl").read_text().splitlines()
    assert [json.loads(line)["repeat"] for line in lines] == [1, 1, 1, 2, 2, 2, 3, 3, 3]
    assert "| mean, min, max, stdev |" in (result.directory / "summary.md").read_text()
    assert "Accuracy spread" in (result.directory / "report.html").read_text()
    agent = result.summary["agent"]
    assert agent["settings"]["model"] == "claude-haiku-5-5"
    assert agent["turns"]["mean"] == 1.0
    assert agent["first_draft_ungrounded_rate"] == 0.0


def test_estimate_refuses_a_run_over_budget(tmp_path: Path, config: Config) -> None:
    ledger = Ledger(tmp_path / "spend.jsonl")
    fresh = estimate(config.agent, ledger, "claude-haiku-5-5", 100)
    assert fresh.basis == "config default" and fresh.cost == pytest.approx(1.0)
    check(fresh)
    ledger.append(run_dir=tmp_path, model="claude-haiku-5-5", items=10, answers=20, cost=29.9)
    measured = estimate(config.agent, ledger, "claude-haiku-5-5", 100)
    assert measured.per_answer == pytest.approx(1.495)
    assert measured.spent == pytest.approx(29.9)
    with pytest.raises(BudgetError, match="budget_usd"):
        check(measured)
    other = estimate(config.agent, ledger, "claude-sonnet-5-5", 1)
    assert other.basis == "config default"


def test_spend_guard_stops_at_the_cap() -> None:
    guard = SpendGuard(0.02)
    guard.add(0.01)
    assert not guard.exhausted
    guard.add(0.01)
    assert guard.exhausted


def test_mid_run_stop_keeps_finished_items_and_resume_finishes(
    eval_db: Path, config: Config, tmp_path: Path
) -> None:
    ledger = Ledger(tmp_path / "spend.jsonl")
    # 100,000 input tokens at $0.10 per million is $0.01 per answer.
    usage = Usage(input_tokens=100_000)
    stopped = _run(
        eval_db,
        config,
        tmp_path,
        PatternClient([[True]] * 3, usage),
        options=harness.RunOptions(ledger=ledger, budget_cap=0.015, model="claude-haiku-5-5"),
    )
    assert stopped.summary["overall"]["items"] == 2
    assert stopped.summary["stopped_early"] == "budget reached: 2 of 3 answers finished"
    assert len((stopped.directory / "results.jsonl").read_text().splitlines()) == 2
    [entry] = ledger.entries()
    assert entry["answers"] == 2 and entry["cost_usd"] == pytest.approx(0.02)
    assert entry["run_dir"] == str(stopped.directory)

    resumed = _run(
        eval_db,
        config,
        tmp_path,
        PatternClient([[True]] * 3, usage),
        options=harness.RunOptions(resume=stopped.directory, ledger=ledger, model="x"),
    )
    assert resumed.directory == stopped.directory
    assert resumed.summary["overall"]["items"] == 3
    assert resumed.summary["stopped_early"] is None
    assert resumed.summary["this_session_cost_usd"] == pytest.approx(0.01)
    assert len(ledger.entries()) == 2 and ledger.total() == pytest.approx(0.03)

    with pytest.raises(harness.EvalError, match="repeats"):
        _run(
            eval_db,
            config,
            tmp_path,
            PatternClient([[True]] * 3),
            options=harness.RunOptions(resume=stopped.directory, repeats=2),
        )


def _config_with_ledger(tmp_path: Path) -> Path:
    text = CONFIG_PATH.read_text(encoding="utf-8").replace(
        'ledger_path = "runs/spend.jsonl"', f'ledger_path = "{tmp_path / "spend.jsonl"}"'
    )
    path = tmp_path / "rxdelta.toml"
    path.write_text(text, encoding="utf-8")
    assert load_config(path).agent.ledger_path == str(tmp_path / "spend.jsonl")
    return path


def _cli_run(eval_db: Path, tmp_path: Path, *extra: str) -> Any:
    return CliRunner().invoke(
        cli.app,
        [
            "eval",
            "run",
            "--set",
            str(FIXTURE_SET),
            "--db",
            str(eval_db),
            "--split",
            "all",
            "--category",
            "out_of_scope",
            "--limit",
            "2",
            "--workers",
            "1",
            "--out-dir",
            str(tmp_path / "runs"),
            "--config",
            str(_config_with_ledger(tmp_path)),
            "--min-accuracy",
            "0",
            *extra,
        ],
    )


def test_eval_cli_runs_the_agent_with_a_budget_and_compares_settings(
    eval_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "make_agent_client", lambda config: PatternClient([[True]] * 2))
    first = _cli_run(eval_db, tmp_path, "--runner", "agent", "--repeats", "2")
    assert first.exit_code == 0, first.output
    out = " ".join(first.stdout.split())
    assert "Estimated cost $0.04 for 4 answers" in out
    assert "Repeats 2: mean 100.0%" in out
    assert "Total spend in the ledger" in out
    second = _cli_run(eval_db, tmp_path, "--runner", "agent", "--no-grounding-guard")
    assert second.exit_code == 0, second.output
    assert "mean of the last run of this model" in " ".join(second.stdout.split())
    ledger = Ledger(tmp_path / "spend.jsonl")
    assert [e["answers"] for e in ledger.entries()] == [4, 2]

    runs = sorted((tmp_path / "runs").iterdir())
    compared = CliRunner().invoke(cli.app, ["eval", "compare", str(runs[0]), str(runs[1])])
    assert compared.exit_code == 0, compared.output
    assert "Settings that differ" in compared.stdout
    assert "guards.grounding" in compared.stdout and "repeats" in compared.stdout


def test_eval_cli_refuses_a_run_over_budget(
    eval_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "make_agent_client", lambda config: PatternClient([[True]]))
    Ledger(tmp_path / "spend.jsonl").append(
        run_dir=tmp_path, model="claude-haiku-5-5", items=1, answers=1, cost=30.0
    )
    refused = _cli_run(eval_db, tmp_path, "--runner", "agent")
    assert refused.exit_code == 1
    assert "budget_usd" in refused.stderr
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())


def test_agent_flags_need_the_agent_runner(eval_db: Path, tmp_path: Path) -> None:
    result = _cli_run(eval_db, tmp_path, "--runner", "oracle", "--model", "claude-haiku-5-5")
    assert result.exit_code == 1
    assert "need --runner agent" in result.stderr


def test_stratified_limit_spreads_over_categories(config: Config) -> None:
    items = harness.load_set(FIXTURE_SET).items
    chosen = harness.select(items, "all", None, 10, stratified=True)
    assert len({i.category for i in chosen}) == 10
    plain = harness.select(items, "all", None, 10)
    assert len({i.category for i in plain}) == 1
