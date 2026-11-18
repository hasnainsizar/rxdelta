"""Every tool, how to call one by name, and how to export their schemas."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from rxdelta.tools import (
    count_changes,
    estimate_cost_change,
    find_drug,
    find_plan,
    get_changes,
    get_coverage,
    list_months,
)
from rxdelta.tools.context import ToolContext
from rxdelta.tools.envelope import ErrorCode, Provenance, ToolResult, failure
from rxdelta.types import RxdeltaError


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    func: Callable[[Any, ToolContext], ToolResult[Any]]
    example: dict[str, Any]

    @property
    def summary(self) -> str:
        return self.description.split(". ")[0].rstrip(".") + "."


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        list_months.NAME,
        list_months.DESCRIPTION,
        list_months.ListMonthsInput,
        list_months.ListMonthsOutput,
        list_months.list_months,
        list_months.EXAMPLE,
    ),
    ToolSpec(
        find_drug.NAME,
        find_drug.DESCRIPTION,
        find_drug.FindDrugInput,
        find_drug.FindDrugOutput,
        find_drug.find_drug,
        find_drug.EXAMPLE,
    ),
    ToolSpec(
        find_plan.NAME,
        find_plan.DESCRIPTION,
        find_plan.FindPlanInput,
        find_plan.FindPlanOutput,
        find_plan.find_plan,
        find_plan.EXAMPLE,
    ),
    ToolSpec(
        get_coverage.NAME,
        get_coverage.DESCRIPTION,
        get_coverage.GetCoverageInput,
        get_coverage.GetCoverageOutput,
        get_coverage.get_coverage,
        get_coverage.EXAMPLE,
    ),
    ToolSpec(
        get_changes.NAME,
        get_changes.DESCRIPTION,
        get_changes.GetChangesInput,
        get_changes.GetChangesOutput,
        get_changes.get_changes,
        get_changes.EXAMPLE,
    ),
    ToolSpec(
        count_changes.NAME,
        count_changes.DESCRIPTION,
        count_changes.CountChangesInput,
        count_changes.CountChangesOutput,
        count_changes.count_changes,
        count_changes.EXAMPLE,
    ),
    ToolSpec(
        estimate_cost_change.NAME,
        estimate_cost_change.DESCRIPTION,
        estimate_cost_change.EstimateCostInput,
        estimate_cost_change.EstimateCostOutput,
        estimate_cost_change.estimate_cost_change,
        estimate_cost_change.EXAMPLE,
    ),
)

_BY_NAME = {spec.name: spec for spec in TOOLS}


def get_spec(name: str) -> ToolSpec | None:
    return _BY_NAME.get(name)


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        where = ".".join(str(p) for p in err["loc"]) or "arguments"
        parts.append(f"{where}: {err['msg']}")
    return "Invalid arguments. " + "; ".join(parts) + "."


def call_tool(name: str, args: dict[str, Any] | None, ctx: ToolContext) -> ToolResult[Any]:
    """Validate and run one tool. Never raises: every problem comes back as ok=False."""
    raw = dict(args or {})
    provenance = Provenance(tool=name, args=raw)
    spec = get_spec(name)
    if spec is None:
        known = ", ".join(s.name for s in TOOLS)
        return failure(
            ErrorCode.INVALID_ARGS, f"No tool named {name!r}. Tools: {known}.", provenance
        )
    try:
        validated = spec.input_model.model_validate(raw)
    except ValidationError as exc:
        return failure(ErrorCode.INVALID_ARGS, _validation_message(exc), provenance)
    try:
        return spec.func(validated, ctx)
    except RxdeltaError as exc:
        return failure(ErrorCode.INTERNAL, str(exc), provenance)
    except Exception as exc:  # noqa: BLE001
        return failure(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}", provenance)


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = copy.deepcopy(defs[node["$ref"].split("/")[-1]])
        target.pop("title", None)
        target.pop("description", None)
        merged = {**_inline(target, defs), **{k: v for k, v in node.items() if k != "$ref"}}
        return _inline(merged, defs)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("$defs", "title"):
            continue
        out[key] = (
            _inline(value, defs)
            if key != "properties"
            else {prop: _inline(schema, defs) for prop, schema in value.items()}
        )
    any_of = out.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and {"type": "null"} in any_of:
        branch = next(b for b in any_of if b != {"type": "null"})
        rest = {k: v for k, v in out.items() if k != "anyOf"}
        if rest.get("default", 0) is None:
            rest.pop("default")
        out = {**branch, **rest}
    return out


def clean_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema with no $defs indirection, no titles and no nullable optionals."""
    raw = model.model_json_schema()
    schema: dict[str, Any] = _inline(raw, raw.get("$defs", {}))
    schema["type"] = "object"
    schema.setdefault("properties", {})
    schema["required"] = sorted(
        name for name, field in model.model_fields.items() if field.is_required()
    )
    schema["additionalProperties"] = False
    return schema


def anthropic_tools() -> list[dict[str, Any]]:
    """Tool definitions as a list of name, description and input schema."""
    return [
        {"name": s.name, "description": s.description, "input_schema": clean_schema(s.input_model)}
        for s in TOOLS
    ]


def json_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "description": s.description,
            "input_schema": clean_schema(s.input_model),
            "output_schema": ToolResult[s.output_model].model_json_schema(),  # type: ignore[name-defined]
        }
        for s in TOOLS
    ]
