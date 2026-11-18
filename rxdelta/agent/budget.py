"""The spend ledger and the budget cap for runs that call a paid model."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rxdelta.config import AgentConfig
from rxdelta.types import RxdeltaError


class BudgetError(RxdeltaError):
    """A run would take total spend past the budget."""


@dataclass(frozen=True)
class Ledger:
    path: Path

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def total(self) -> float:
        return round(sum(float(e.get("cost_usd", 0.0)) for e in self.entries()), 6)

    def mean_cost(self, model: str) -> float | None:
        """Cost per answered question in the latest ledger entry for this model."""
        for entry in reversed(self.entries()):
            if entry.get("model") == model and entry.get("answers"):
                return float(entry["cost_usd"]) / int(entry["answers"])
        return None

    def append(self, *, run_dir: Path, model: str, items: int, answers: int, cost: float) -> None:
        entry = {
            "date": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_dir": str(run_dir),
            "model": model,
            "items": items,
            "answers": answers,
            "cost_usd": round(cost, 6),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


@dataclass(frozen=True)
class Estimate:
    answers: int
    per_answer: float
    basis: str
    spent: float
    budget: float

    @property
    def cost(self) -> float:
        return self.answers * self.per_answer

    def describe(self) -> str:
        return (
            f"Estimated cost ${self.cost:.2f} for {self.answers} answers at "
            f"${self.per_answer:.4f} each ({self.basis}). Spent so far ${self.spent:.2f} "
            f"of the ${self.budget:.2f} budget."
        )


def estimate(agent: AgentConfig, ledger: Ledger, model: str, answers: int) -> Estimate:
    measured = ledger.mean_cost(model)
    per_answer = measured if measured is not None else agent.prices(model).estimate_usd_per_question
    basis = "mean of the last run of this model" if measured is not None else "config default"
    return Estimate(
        answers=answers,
        per_answer=per_answer,
        basis=basis,
        spent=ledger.total(),
        budget=agent.budget_usd,
    )


def check(estimated: Estimate) -> None:
    if estimated.spent + estimated.cost > estimated.budget:
        raise BudgetError(
            f"{estimated.describe()} That would pass [agent].budget_usd; raise it in "
            "config/rxdelta.toml or run fewer items."
        )


class SpendGuard:
    """Counts spend as answers finish; once it reaches the cap, no new answer starts."""

    def __init__(self, remaining: float) -> None:
        self.remaining = remaining
        self.spent = 0.0
        self._lock = threading.Lock()

    def add(self, cost: float) -> None:
        with self._lock:
            self.spent += cost

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self.spent >= self.remaining
