"""Render the HTML digest. Self contained: no network requests, no CDN links."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from rxdelta.config import Config
from rxdelta.diff.engine import DiffResult
from rxdelta.diff.impact import ChangeGroup
from rxdelta.limitations import (
    ESTIMATE_NOTE,
    LIMITATIONS,
    LIMITATIONS_TITLE,
    OPEN_ENDED_NOTE,
)
from rxdelta.report import format as fmt
from rxdelta.report.fonts import embedded_faces

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_CHANNEL_LABELS = {
    "retail_preferred": "preferred retail pharmacy",
    "retail_non_preferred": "non preferred retail pharmacy",
    "mail_preferred": "preferred mail order",
    "mail_non_preferred": "non preferred mail order",
}


@dataclass(frozen=True)
class SeveritySummary:
    """How much the score actually discriminates on this comparison.

    The report states this rather than implying a precision the data does not
    support.
    """

    distinct: int
    total: int
    largest_tie: int

    @property
    def discriminates(self) -> bool:
        return self.distinct >= max(4, self.total // 10)


def severity_summary(groups: list[ChangeGroup]) -> SeveritySummary:
    counts = Counter(g.severity for g in groups)
    return SeveritySummary(
        distinct=len(counts),
        total=len(groups),
        largest_tie=max(counts.values(), default=0),
    )


@dataclass(frozen=True)
class ReportRow:
    drug_label: str
    has_name: bool
    ndc: str
    rxcui: str
    change_types: str
    plan_count: int
    plan_examples: str
    impact: str
    tier_before: str
    tier_after: str
    tier_before_level: int | None
    tier_after_level: int | None
    severity: float
    severity_band: str
    direction_glyph: str
    direction_word: str
    direction_class: str
    modal: str
    modal_glyph: str
    modal_word: str
    modal_class: str
    modal_known: bool
    spans_zero: bool
    plans: tuple[str, ...]
    plans_hidden: int
    open_ended: bool
    priced: bool


@dataclass(frozen=True)
class TypeTally:
    """One row of the change type rollup."""

    label: str
    count: int


@dataclass(frozen=True)
class LowResultNotice:
    count: int
    floor: int
    reasons: tuple[str, ...]


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def low_result_notice(
    config: Config, result: DiffResult, group_count: int
) -> LowResultNotice | None:
    if group_count >= config.report.low_result_floor:
        return None
    reasons = [
        f"{result.month_from} and {result.month_to} may be adjacent months with little "
        "formulary movement, which is the normal case.",
        "One or both snapshots may be only partially loaded. Run rxdelta status and check "
        "the row counts and the rejected row count.",
    ]
    if result.plan_filter:
        reasons.append(
            f"The comparison was filtered to contract {result.plan_filter}, covering "
            f"{result.plans_compared} plans. Drop --plan to widen it."
        )
    else:
        reasons.append(
            f"The comparison covered {result.plans_compared} plans present in both months. "
            "Plans that exist in only one month are not compared."
        )
    return LowResultNotice(
        count=group_count, floor=config.report.low_result_floor, reasons=tuple(reasons)
    )


def build_rows(config: Config, groups: list[ChangeGroup]) -> list[ReportRow]:
    rows = []
    for group in groups:
        examples = group.plan_names[0] if group.plan_names else ""
        if len(group.plan_names) > 1:
            examples += f" and {len(group.plan_names) - 1} more"
        glyph, word, css_class = fmt.direction_mark(group.impact)
        modal_glyph, modal_word, modal_class = fmt.modal_mark(group.impact)
        rows.append(
            ReportRow(
                drug_label=fmt.drug_label(group.drug_name, group.ndc_11),
                has_name=bool(group.drug_name),
                ndc=fmt.ndc_display(group.ndc_11),
                rxcui=group.rxcui,
                change_types=fmt.change_types(group.change_types),
                plan_count=group.plan_count,
                plan_examples=examples,
                impact=fmt.impact_range(group.impact),
                tier_before=config.tier_label(group.tier_before),
                tier_after=config.tier_label(group.tier_after),
                tier_before_level=group.tier_before,
                tier_after_level=group.tier_after,
                severity=group.severity,
                severity_band=fmt.severity_band(config, group.severity),
                direction_glyph=glyph,
                direction_word=word,
                direction_class=css_class,
                modal=fmt.modal_figure(group.impact),
                modal_glyph=modal_glyph,
                modal_word=modal_word,
                modal_class=modal_class,
                modal_known=group.impact.modal_known,
                spans_zero=group.impact.spans_zero,
                plans=tuple(str(p) for p in group.plans[: config.report.max_plans_listed]),
                plans_hidden=max(0, group.plan_count - config.report.max_plans_listed),
                open_ended=group.impact.open_ended,
                priced=group.impact.priced,
            )
        )
    return rows


def build_tallies(result: DiffResult) -> list[TypeTally]:
    counts = sorted(result.counts_by_type().items(), key=lambda kv: (-kv[1], kv[0].value))
    return [TypeTally(label=change_type.label, count=count) for change_type, count in counts]


def render(
    config: Config,
    result: DiffResult,
    groups: list[ChangeGroup],
    *,
    generated_at: str | None = None,
) -> str:
    notice = low_result_notice(config, result, len(groups))
    top = groups[: config.report.top_changes]
    rows = build_rows(config, top)
    template = _environment().get_template("report.html.j2")
    return template.render(
        month_from=result.month_from,
        month_to=result.month_to,
        plan_filter=result.plan_filter,
        total_changes=len(result.changes),
        change_groups=len(groups),
        plans_compared=result.plans_compared,
        plans_added=len(result.plans_added),
        plans_removed=len(result.plans_removed),
        drugs_from=result.drugs_from,
        drugs_to=result.drugs_to,
        affected_drugs=result.affected_drugs,
        affected_plans=result.affected_plans,
        rows=rows,
        shown=len(top),
        by_type=build_tallies(result),
        notice=notice,
        limitations=LIMITATIONS,
        limitations_title=LIMITATIONS_TITLE,
        estimate_note=ESTIMATE_NOTE,
        open_ended_note=OPEN_ENDED_NOTE,
        severity_bands=config.report.severity_bands,
        faces=embedded_faces(),
        generated_at=generated_at or datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        config_path=config.path.name,
        coverage_phase=config.codes.coverage_level[config.impact.coverage_level],
        normalize_days=config.impact.normalize_days,
        modal_channel=_CHANNEL_LABELS.get(
            config.impact.modal.channel, config.impact.modal.channel.replace("_", " ")
        ),
        modal_supply=config.codes.days_supply[config.impact.modal.days_supply].label,
        named_rows=sum(1 for r in rows if r.has_name),
        severity_summary=severity_summary(groups),
    )


def write(
    config: Config,
    result: DiffResult,
    groups: list[ChangeGroup],
    out_path: Path,
    *,
    generated_at: str | None = None,
) -> Path:
    html = render(config, result, groups, generated_at=generated_at)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path
