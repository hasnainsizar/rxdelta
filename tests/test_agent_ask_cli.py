from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from rxdelta import cli
from rxdelta.agent.client import ScriptedClient, Usage
from rxdelta.agent.keys import MISSING_KEY
from rxdelta.config import Config
from tests.agent_script import submit, tool_use, turn

QUESTION = "Did Brilinta change between 2026-02 and 2026-03?"


def _script() -> list[Any]:
    call = tool_use(
        "get_changes",
        {"from_month": "2026-02", "to_month": "2026-03", "rxcuis": ["8800151"]},
        1,
    )
    answer = {"kind": "no_data", "reason": "no_matching_changes"}
    return [
        turn(call, usage=Usage(input_tokens=1200, output_tokens=80)),
        turn(submit(answer, "Nothing changed for Brilinta."), usage=Usage(output_tokens=40)),
    ]


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> ScriptedClient:
    client = ScriptedClient(_script())
    monkeypatch.setattr(cli, "make_agent_client", lambda config: client)
    return client


def _ask(eval_db: Path, *extra: str) -> Any:
    return CliRunner().invoke(cli.app, ["ask", QUESTION, "--db", str(eval_db), *extra])


def test_ask_prints_the_answer_a_summary_line_and_the_disclaimer(
    eval_db: Path, scripted: ScriptedClient
) -> None:
    result = _ask(eval_db)
    assert result.exit_code == 0, result.output
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "Nothing changed for Brilinta."
    assert "months 2026-02 to 2026-03" in lines[1]
    assert "claude-haiku-5-5" in lines[1] and "1 tool calls" in lines[1]
    assert "1,200 in and 120 out tokens" in lines[1]
    assert lines[2] == "This reports CMS formulary data. It is not medical advice."
    assert f"Using {eval_db}" in result.stderr
    assert "get_changes" not in result.stderr


def test_show_tools_prints_each_call(eval_db: Path, scripted: ScriptedClient) -> None:
    result = _ask(eval_db, "--show-tools")
    assert result.exit_code == 0, result.output
    assert "get_changes" in result.stderr and '"rxcuis": ["8800151"]' in result.stderr
    assert "-> ok" in result.stderr


def test_json_prints_the_runner_output_without_the_trace(
    eval_db: Path, scripted: ScriptedClient
) -> None:
    result = _ask(eval_db, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["answer"] == {"kind": "no_data", "reason": "no_matching_changes"}
    assert payload["tool_calls"][0]["name"] == "get_changes"
    assert "trace" not in payload


def test_months_go_to_the_model(eval_db: Path, scripted: ScriptedClient) -> None:
    result = _ask(eval_db, "--from", "2026-02", "--to", "2026-03")
    assert result.exit_code == 0, result.output
    assert "2026-02" in scripted.requests[0].messages[0]["content"]
    assert _ask(eval_db, "--from", "2026-02").exit_code == 1


def test_missing_key_is_one_clear_sentence(
    eval_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environ = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    monkeypatch.setattr(os, "environ", environ)
    monkeypatch.chdir(tmp_path)
    result = _ask(eval_db)
    assert result.exit_code == 1
    assert MISSING_KEY.split(":")[0] in result.stderr
    assert "Traceback" not in result.output


def test_missing_database(tmp_path: Path, scripted: ScriptedClient) -> None:
    result = CliRunner().invoke(cli.app, ["ask", QUESTION, "--db", str(tmp_path / "none.db")])
    assert result.exit_code == 1
    assert "Database not found" in result.stderr


def test_no_months_loaded(tmp_path: Path, scripted: ScriptedClient) -> None:
    from rxdelta.db import connect

    empty = tmp_path / "empty.db"
    connect(empty).close()
    result = CliRunner().invoke(cli.app, ["ask", QUESTION, "--db", str(empty)])
    assert result.exit_code == 1
    assert "No months are loaded" in result.stderr


def test_cost_cap_is_a_plain_error(
    eval_db: Path, monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    huge = Usage(input_tokens=50_000_000)
    client = ScriptedClient([turn(tool_use("list_months", {}, 1), usage=huge)])
    monkeypatch.setattr(cli, "make_agent_client", lambda c: client)
    result = _ask(eval_db)
    assert result.exit_code == 1
    assert "per question cost cap" in result.stderr
