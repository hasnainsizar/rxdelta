from __future__ import annotations

import pytest

from rxdelta.agent import prompts
from rxdelta.evals.items import read_items
from rxdelta.types import RxdeltaError
from tests.agent_script import REPO


def test_prompt_versions_load_and_unknown_ones_are_refused() -> None:
    assert "v1" in prompts.available()
    assert "submit_answer" in prompts.load("v1")
    with pytest.raises(RxdeltaError, match="No system prompt 'v99'"):
        prompts.load("v99")


@pytest.mark.parametrize("version", prompts.available())
def test_no_prompt_contains_an_eval_question(version: str) -> None:
    text = prompts.load(version).lower()
    for path in ("evals/rxdelta_v1.jsonl", "evals/rxdelta_fixture_v1.jsonl"):
        for item in read_items(REPO / path):
            assert item.question.lower() not in text, item.id
            assert item.id not in text
