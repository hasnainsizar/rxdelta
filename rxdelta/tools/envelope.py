"""The result shape every tool returns."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)


class ErrorCode(StrEnum):
    INVALID_ARGS = "invalid_args"
    MONTH_NOT_LOADED = "month_not_loaded"
    UNKNOWN_DRUG = "unknown_drug"
    UNKNOWN_PLAN = "unknown_plan"
    INTERNAL = "internal"


class ToolError(BaseModel):
    code: ErrorCode
    message: str


class Provenance(BaseModel):
    """Exactly what produced a result: the tool, its validated arguments, the
    snapshot months read and the keys of the rows the answer rests on."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    months: list[str] = Field(default_factory=list)
    row_keys: list[str] = Field(default_factory=list)


class ToolResult(BaseModel, Generic[T]):
    ok: bool
    data: T | None = None
    total: int | None = None
    truncated: bool = False
    note: str | None = None
    error: ToolError | None = None
    provenance: Provenance


def success(
    data: T,
    provenance: Provenance,
    *,
    total: int | None = None,
    shown: int | None = None,
    note: str | None = None,
) -> ToolResult[T]:
    truncated = total is not None and shown is not None and total > shown
    return ToolResult[T](
        ok=True, data=data, total=total, truncated=truncated, note=note, provenance=provenance
    )


def failure(
    code: ErrorCode,
    message: str,
    provenance: Provenance,
    *,
    data: T | None = None,
    total: int | None = None,
) -> ToolResult[T]:
    """ok=False: the call itself could not be answered as asked. A query that
    ran and matched nothing is a success with empty data, never a failure."""
    return ToolResult[T](
        ok=False,
        data=data,
        total=total,
        truncated=False,
        note=message,
        error=ToolError(code=code, message=message),
        provenance=provenance,
    )


def cap_note(total: int, shown: int, narrow: str) -> str:
    return f"Showing {shown} of {total}. The list is partial; {narrow}."
