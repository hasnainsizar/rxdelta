"""Domain types shared by the ingest and diff layers.

This module is the only thing the two layers have in common. It deliberately
imports nothing from either of them so a different source dataset can be
plugged into ingest without the diff layer noticing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RxdeltaError(Exception):
    """Base class for errors that should surface as a clean CLI message."""


class ConfigError(RxdeltaError):
    """Raised when config/rxdelta.toml is missing a value or malformed."""


class SchemaError(RxdeltaError):
    """Raised when a source file does not match its declared column layout."""


class LoadError(RxdeltaError):
    """Raised when a snapshot cannot be loaded."""


class DiffError(RxdeltaError):
    """Raised when a comparison cannot be run."""


@dataclass(frozen=True, order=True)
class PlanKey:
    """A Part D plan is identified by three fields, never by one of them alone."""

    contract_id: str
    plan_id: str
    segment_id: str

    def __str__(self) -> str:
        return f"{self.contract_id}-{self.plan_id}-{self.segment_id}"


@dataclass(frozen=True)
class DrugCoverage:
    """How one formulary treats one drug at one point in time."""

    ndc_11: str
    rxcui: str
    tier_level: int
    prior_auth: bool
    step_therapy: bool
    quantity_limit: bool
    quantity_limit_amount: float | None
    quantity_limit_days: int | None

    def daily_quantity(self) -> float | None:
        """Quantity limit expressed per day, so limits on different windows compare."""
        if not self.quantity_limit:
            return None
        if self.quantity_limit_amount is None or not self.quantity_limit_days:
            return None
        return self.quantity_limit_amount / self.quantity_limit_days


@dataclass(frozen=True)
class PlanRecord:
    """A plan and the formulary it pointed at in one snapshot."""

    key: PlanKey
    formulary_id: str
    plan_name: str
    contract_name: str


@dataclass(frozen=True)
class CostLeg:
    """One pharmacy channel of one beneficiary cost row."""

    channel: str
    cost_type: str
    cost_amt: float | None
    cost_min_amt: float | None
    cost_max_amt: float | None


@dataclass(frozen=True)
class CostRow:
    """One beneficiary cost row: a plan, tier, coverage phase and supply length."""

    key: PlanKey
    coverage_level: str
    tier: int
    days_supply: str
    legs: tuple[CostLeg, ...]


class ChangeType(StrEnum):
    """One classification of one drug and plan pair between two snapshots.

    A pair can carry several of these at once, so changes are always modelled
    as a list. `member_impact` says whether the change points toward the member
    paying or being restricted more (+1), less (-1), or neither (0).
    """

    DRUG_ADDED = "drug_added"
    DRUG_REMOVED = "drug_removed"
    TIER_UP = "tier_up"
    TIER_DOWN = "tier_down"
    PRIOR_AUTH_ADDED = "prior_auth_added"
    PRIOR_AUTH_REMOVED = "prior_auth_removed"
    STEP_THERAPY_ADDED = "step_therapy_added"
    STEP_THERAPY_REMOVED = "step_therapy_removed"
    QUANTITY_LIMIT_ADDED = "quantity_limit_added"
    QUANTITY_LIMIT_REMOVED = "quantity_limit_removed"
    QUANTITY_LIMIT_TIGHTENED = "quantity_limit_tightened"
    QUANTITY_LIMIT_LOOSENED = "quantity_limit_loosened"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").capitalize()

    @property
    def member_impact(self) -> int:
        return _MEMBER_IMPACT[self]

    @property
    def is_restriction_added(self) -> bool:
        return self in _RESTRICTIONS_ADDED


_MEMBER_IMPACT: dict[ChangeType, int] = {
    ChangeType.DRUG_ADDED: -1,
    ChangeType.DRUG_REMOVED: 1,
    ChangeType.TIER_UP: 1,
    ChangeType.TIER_DOWN: -1,
    ChangeType.PRIOR_AUTH_ADDED: 1,
    ChangeType.PRIOR_AUTH_REMOVED: -1,
    ChangeType.STEP_THERAPY_ADDED: 1,
    ChangeType.STEP_THERAPY_REMOVED: -1,
    ChangeType.QUANTITY_LIMIT_ADDED: 1,
    ChangeType.QUANTITY_LIMIT_REMOVED: -1,
    ChangeType.QUANTITY_LIMIT_TIGHTENED: 1,
    ChangeType.QUANTITY_LIMIT_LOOSENED: -1,
}

_RESTRICTIONS_ADDED: frozenset[ChangeType] = frozenset(
    {
        ChangeType.PRIOR_AUTH_ADDED,
        ChangeType.STEP_THERAPY_ADDED,
        ChangeType.QUANTITY_LIMIT_ADDED,
        ChangeType.QUANTITY_LIMIT_TIGHTENED,
        ChangeType.DRUG_REMOVED,
    }
)
