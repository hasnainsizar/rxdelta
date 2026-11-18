"""The eval item format and reading and writing eval set files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rxdelta.ask.answer import ChangeAnswer, QuestionContext, StructuredAnswer
from rxdelta.config import Category

Difficulty = Literal["easy", "medium", "hard"]
Split = Literal["dev", "test"]
Source = Literal["generated", "handwritten"]


class GoldSpec(BaseModel):
    """Which gold query answers the item, with its arguments. The arguments also
    carry what the question names (the drug or plan as written), which is what
    the oracle runner looks up."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    args: dict[str, Any] = Field(default_factory=dict)


class EvalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    template_id: str
    question: str
    context: QuestionContext = Field(default_factory=QuestionContext)
    expected: StructuredAnswer
    # For ambiguous items: the full answer for every candidate, which also counts
    # as correct when the runner answers instead of asking.
    alternative: ChangeAnswer | None = None
    required_tools: list[str]
    gold_query: str
    gold: GoldSpec
    difficulty: Difficulty
    split: Split
    source: Source
    seed: int
    tags: list[str] = Field(default_factory=list)


class HandwrittenItem(BaseModel):
    """What a hand-written item supplies. The builder computes the rest."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    question: str
    context: QuestionContext = Field(default_factory=QuestionContext)
    gold: GoldSpec
    tags: list[str] = Field(default_factory=list)


def item_line(item: EvalItem) -> str:
    return json.dumps(item.model_dump(mode="json"), ensure_ascii=False)


def write_items(path: Path, items: list[EvalItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(item_line(i) + "\n" for i in items), encoding="utf-8")


def read_items(path: Path) -> list[EvalItem]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [EvalItem.model_validate_json(line) for line in lines if line.strip()]


def read_handwritten(path: Path) -> list[HandwrittenItem]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [HandwrittenItem.model_validate_json(line) for line in lines if line.strip()]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_path(set_path: Path) -> Path:
    return set_path.with_name(f"{set_path.stem}.manifest.json")
