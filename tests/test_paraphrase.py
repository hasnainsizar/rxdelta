from __future__ import annotations

import csv
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rxdelta import cli
from rxdelta.agent.budget import Ledger
from rxdelta.agent.client import ScriptedClient
from rxdelta.config import Config
from rxdelta.evals import harness, paraphrase
from tests.agent_script import FIXTURE_SET, text_turn
from tests.test_agent_eval import _config_with_ledger


def _draft(tmp_path: Path, n: int = 4) -> Path:
    client = ScriptedClient([text_turn(f"casual   version {k}\n") for k in range(n)])
    drafted = paraphrase.draft(FIXTURE_SET, n, client, "claude-haiku-5-5", 256, "low")
    assert len({r["category"] for r in drafted.rows}) == n
    assert drafted.rows[0]["paraphrase"] == "casual version 0"
    assert all(r["approved"] == "" for r in drafted.rows)
    path = tmp_path / "candidates.csv"
    paraphrase.write_csv(path, drafted.rows)
    return path


def _approve(path: Path, keep: set[int]) -> None:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for n, row in enumerate(rows):
        row["approved"] = "yes" if n in keep else ""
        row["paraphrase"] = f"hey, {row['original'].lower()}"
    paraphrase.write_csv(path, rows)


def test_approved_rows_become_an_eval_set_with_the_original_gold(
    tmp_path: Path, eval_db: Path, config: Config
) -> None:
    candidates = _draft(tmp_path)
    _approve(candidates, {0, 2})
    out = tmp_path / "para.jsonl"
    items = paraphrase.build(FIXTURE_SET, candidates, out)
    originals = {i.id: i for i in harness.load_set(FIXTURE_SET).items}
    assert len(items) == 2
    for item in items:
        source = originals[item.id.removesuffix("-para")]
        assert item.expected == source.expected and item.gold == source.gold
        assert item.question.startswith("hey, ") and "paraphrase" in item.tags
    result = harness.run(
        set_path=out,
        runner_name="oracle",
        split="all",
        db_path=eval_db,
        config=config,
        out_root=tmp_path / "runs",
    )
    assert result.summary["overall"]["accuracy"] == 1.0


def test_nothing_approved_is_an_error(tmp_path: Path) -> None:
    candidates = _draft(tmp_path, 2)
    with pytest.raises(harness.EvalError, match="No approved rows"):
        paraphrase.build(FIXTURE_SET, candidates, tmp_path / "para.jsonl")


def test_paraphrase_cli_draft_logs_spend_and_build_writes_the_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = ScriptedClient([text_turn(f"version {k}") for k in range(3)])
    monkeypatch.setattr(cli, "make_agent_client", lambda config: client)
    out = tmp_path / "c.csv"
    drafted = CliRunner().invoke(
        cli.app,
        [
            "evalset",
            "paraphrase-draft",
            "--set",
            str(FIXTURE_SET),
            "--n",
            "3",
            "--out",
            str(out),
            "--config",
            str(_config_with_ledger(tmp_path)),
        ],
    )
    assert drafted.exit_code == 0, drafted.output
    [entry] = Ledger(tmp_path / "spend.jsonl").entries()
    assert entry["kind"] == "paraphrase" and entry["answers"] == 3
    assert Ledger(tmp_path / "spend.jsonl").mean_cost("claude-haiku-5-5") is None
    _approve(out, {1})
    built = CliRunner().invoke(
        cli.app,
        [
            "evalset",
            "paraphrase",
            "--approved",
            str(out),
            "--set",
            str(FIXTURE_SET),
            "--out",
            str(tmp_path / "p.jsonl"),
        ],
    )
    assert built.exit_code == 0, built.output
    assert harness.load_set(tmp_path / "p.jsonl").manifest["items"] == 1
