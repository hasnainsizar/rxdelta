from __future__ import annotations

import json
from pathlib import Path

import pytest

from rxdelta.agent.client import ReplayClient
from rxdelta.agent.loop import AgentRunner, AgentSettings
from rxdelta.config import Config
from rxdelta.evals import harness
from tests.agent_script import REPO

CASSETTE = REPO / "tests" / "cassettes" / "agent_fixture.jsonl"
SCORES = CASSETTE.with_suffix(".scores.json")


@pytest.mark.skipif(
    not (CASSETTE.is_file() and SCORES.is_file()),
    reason="No agent cassette recorded yet: run make ask-record with ANTHROPIC_API_KEY set.",
)
def test_the_agent_replays_its_recorded_run_exactly(
    eval_db: Path, config: Config, tmp_path: Path
) -> None:
    recorded = json.loads(SCORES.read_text(encoding="utf-8"))
    guards = recorded["settings"]["guards"]
    settings = AgentSettings.from_config(
        config,
        model=recorded["settings"]["model"],
        prompt_version=recorded["settings"]["prompt_version"],
        guards_off=tuple(g for g, on in guards.items() if not on),
    )
    client = ReplayClient(CASSETTE, "replay")
    result = harness.run(
        set_path=REPO / recorded["eval_set"],
        runner_name="agent",
        split=recorded["split"],
        db_path=eval_db,
        config=config,
        out_root=tmp_path,
        limit=recorded["limit"],
        stratified=recorded["stratified"],
        workers=1,
        runner_factory=lambda items, ctx: AgentRunner(items, ctx, client=client, settings=settings),
    )
    assert result.summary["overall"]["errors"] == 0
    assert harness.outcomes(result.directory) == recorded["correct"]
