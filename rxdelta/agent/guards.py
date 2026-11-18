"""Checks a submitted answer must pass before it is accepted.

Each check is a pure function returning the problems it found, empty when the
answer passes. The loop sends the problems back to the model as an error.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from rxdelta.ask.answer import ClarifyAnswer, CountAnswer, DrugListAnswer, StructuredAnswer
from rxdelta.ask.answer import parse_answer as _parse
from rxdelta.evals import grounding

_PARTIAL = re.compile(
    r"\b(partial|first \d+|first page|not (?:the )?(?:full|complete|whole)|shortened|"
    r"truncated|more than|only (?:the )?first|showing|of \d[\d,]* (?:drugs|results|rows))\b",
    re.IGNORECASE,
)
_SELF_TALK = re.compile(
    r"\b(as an ai|as a language model|i am an ai|i'm an ai|as an assistant)\b", re.IGNORECASE
)


def schema_problems(raw: Any) -> tuple[StructuredAnswer | None, list[str]]:
    """The parsed answer, or the validation errors in a form the model can act on."""
    try:
        return _parse(raw), []
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"]) or "answer"
            problems.append(f"answer.{where}: {err['msg']}")
        return None, problems


def ungrounded_numbers(text: str, question: str, results: Iterable[dict[str, Any]]) -> list[str]:
    return list(grounding.check(text, question, results).ungrounded)


def _has_number(text: str, value: int) -> bool:
    plain = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    return re.search(rf"(?<![\d.]){value}(?![\d.]|,\d)", plain) is not None


def consistency_problems(answer: StructuredAnswer, text: str) -> list[str]:
    problems: list[str] = []
    if not text.strip():
        return ["final_text is empty; write the answer for the person who asked."]
    if _SELF_TALK.search(text):
        problems.append("final_text talks about itself as an AI; state the facts plainly.")
    if isinstance(answer, CountAnswer) and not _has_number(text, answer.count):
        problems.append(
            f"The answer's count is {answer.count} but final_text does not state {answer.count}."
        )
    if isinstance(answer, DrugListAnswer) and answer.truncated:
        if answer.total is None:
            problems.append(
                "The drug list is marked truncated but has no total; set total from the tool's "
                "total or from count_changes."
            )
        elif not _has_number(text, answer.total):
            problems.append(
                f"The list is partial but final_text does not state the total, {answer.total}."
            )
        if not _PARTIAL.search(text):
            problems.append(
                "The list is marked truncated but final_text does not say it is partial."
            )
    if isinstance(answer, ClarifyAnswer):
        lowered = text.lower()
        missing = [c.product for c in answer.candidates if c.product.lower() not in lowered]
        if missing:
            problems.append(
                "final_text asks which drug is meant but does not name: " + ", ".join(missing) + "."
            )
    return problems


def grounding_message(ungrounded: list[str]) -> str:
    return (
        "These numbers in final_text do not appear in any tool result: "
        + ", ".join(ungrounded)
        + ". Restate every number exactly as a tool returned it. If you counted or added up "
        "items yourself, call count_changes instead and use its number."
    )
