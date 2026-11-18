"""A test-only runner that corrupts the oracle's answers at a seeded rate."""

from __future__ import annotations

import random
from typing import Literal

from rxdelta.ask.answer import CountAnswer, DrugListAnswer, QuestionContext, RunnerOutput
from rxdelta.evals.items import EvalItem
from rxdelta.evals.runners import OracleRunner
from rxdelta.tools.context import ToolContext

Corruption = Literal["drop_item", "fake_rxcui", "off_by_one", "fake_number"]
FAKE_RXCUI = "9999999"
FAKE_SENTENCE = " That works out to about 4321 dollars."


class NoisyRunner:
    name = "noisy"

    def __init__(
        self,
        items: list[EvalItem],
        ctx: ToolContext,
        *,
        corruption: Corruption,
        rate: float = 1.0,
        seed: int = 0,
    ) -> None:
        self._oracle = OracleRunner(items, ctx)
        self._corruption = corruption
        self._rate = rate
        self._seed = seed

    def answer(self, question: str, context: QuestionContext) -> RunnerOutput:
        output = self._oracle.answer(question, context)
        if random.Random(f"{self._seed}:{question}").random() >= self._rate:
            return output
        answer = output.answer
        if self._corruption == "drop_item" and isinstance(answer, DrugListAnswer):
            if answer.rxcuis:
                answer = answer.model_copy(update={"rxcuis": answer.rxcuis[:-1]})
        elif self._corruption == "fake_rxcui" and isinstance(answer, DrugListAnswer):
            answer = answer.model_copy(update={"rxcuis": [*answer.rxcuis, FAKE_RXCUI]})
        elif self._corruption == "off_by_one" and isinstance(answer, CountAnswer):
            answer = answer.model_copy(update={"count": answer.count + 1})
        elif self._corruption == "fake_number":
            return output.model_copy(update={"final_text": output.final_text + FAKE_SENTENCE})
        return output.model_copy(update={"answer": answer})
