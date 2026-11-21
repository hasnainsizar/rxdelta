"""The answer contract: what a runner returns and what the eval set expects.

A runner (the oracle now, an agent later) returns a final text answer for the
person and a structured answer for scoring. Each answer kind has its own strict
payload model.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from rxdelta.types import ChangeType


class AnswerKind(StrEnum):
    drug_list = "drug_list"  # a set of RXCUIs
    count = "count"  # type: ignore[assignment]  # an integer; shadows str.count
    change = "change"  # change types with before and after values for one drug
    coverage = "coverage"  # tier, PA, ST, QL for one drug at one month and formulary
    cost_range = "cost_range"  # low and high change in monthly cost
    no_data = "no_data"  # answerable question, but the data says nothing
    refusal = "refusal"  # out of scope, for example medical advice
    clarify = "clarify"  # ambiguous question; the runner asks which one


class NoDataReason(StrEnum):
    unknown_drug = "unknown_drug"
    month_not_loaded = "month_not_loaded"
    no_matching_changes = "no_matching_changes"
    not_on_formulary = "not_on_formulary"


Rxcui = Annotated[str, Field(pattern=r"^\d{1,8}$")]


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DrugListAnswer(_Payload):
    kind: Literal["drug_list"] = "drug_list"
    rxcuis: list[Rxcui]
    display_names: list[str] = Field(default_factory=list)
    # Secondary check for questions that also ask "what tier is each on now".
    tiers: dict[str, int] | None = None
    # A list cut short by a tool's row cap says so, and says how many there are.
    total: int | None = Field(default=None, ge=0)
    truncated: bool = False


class CountAnswer(_Payload):
    kind: Literal["count"] = "count"
    count: int = Field(ge=0)
    unit: Literal["drugs", "formularies", "plans", "rows"] = "drugs"


class ChangeItem(_Payload):
    rxcui: Rxcui
    formulary_id: str
    change_type: ChangeType
    before: str | None
    after: str | None


class ChangeAnswer(_Payload):
    kind: Literal["change"] = "change"
    # Every RXCUI the answer covers, including ones with no change.
    rxcuis: list[Rxcui]
    items: list[ChangeItem]


class CoverageAnswer(_Payload):
    kind: Literal["coverage"] = "coverage"
    rxcui: Rxcui
    month: str
    formulary_id: str
    tier: int = Field(ge=1)
    prior_auth: bool
    step_therapy: bool
    quantity_limit: bool
    quantity_limit_amount: float | None = None
    quantity_limit_days: int | None = None


class CostRangeAnswer(_Payload):
    kind: Literal["cost_range"] = "cost_range"
    rxcui: Rxcui
    low: float
    high: float
    currency: Literal["USD"] = "USD"

    @model_validator(mode="after")
    def _ordered(self) -> CostRangeAnswer:
        if self.low > self.high:
            raise ValueError("low must not exceed high")
        return self


class NoDataAnswer(_Payload):
    kind: Literal["no_data"] = "no_data"
    reason: NoDataReason


class RefusalAnswer(_Payload):
    kind: Literal["refusal"] = "refusal"
    reason: Literal["medical_advice", "out_of_scope"] = "out_of_scope"


class ClarifyCandidate(_Payload):
    product: str
    rxcuis: list[Rxcui]


class ClarifyAnswer(_Payload):
    kind: Literal["clarify"] = "clarify"
    candidates: list[ClarifyCandidate] = Field(min_length=1)

    def rxcuis(self) -> set[str]:
        return {r for c in self.candidates for r in c.rxcuis}


StructuredAnswer = Annotated[
    DrugListAnswer
    | CountAnswer
    | ChangeAnswer
    | CoverageAnswer
    | CostRangeAnswer
    | NoDataAnswer
    | RefusalAnswer
    | ClarifyAnswer,
    Field(discriminator="kind"),
]
ANSWER_ADAPTER: TypeAdapter[StructuredAnswer] = TypeAdapter(StructuredAnswer)


def parse_answer(data: Any) -> StructuredAnswer:
    return ANSWER_ADAPTER.validate_python(data)


class ToolCallRecord(BaseModel):
    name: str
    args: dict[str, Any]
    # The ToolResult as JSON, so a saved run reads back without the tool models.
    result: dict[str, Any]
    duration_ms: int


class RunnerOutput(BaseModel):
    answer: StructuredAnswer
    final_text: str
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int
    error: str | None = None
    # A runner's own record of how it got the answer. Never scored, and left out
    # of model_dump so it is only written where asked for.
    trace: dict[str, Any] | None = Field(default=None, exclude=True)


class QuestionContext(BaseModel):
    """Only what a real user would know. Never carries gold answers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_from: str | None = None
    default_to: str | None = None


class Runner(Protocol):
    name: str

    def answer(self, question: str, context: QuestionContext) -> RunnerOutput: ...
