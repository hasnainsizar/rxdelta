"""Value formatting shared by the terminal and HTML surfaces."""

from __future__ import annotations

from collections.abc import Sequence

from rxdelta.config import Config
from rxdelta.diff.impact import ChangeGroup, ImpactRange
from rxdelta.types import ChangeType


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def signed_money(value: float) -> str:
    if abs(value) < 0.005:
        return "$0.00"
    prefix = "+" if value > 0 else "-"
    return f"{prefix}${abs(value):,.2f}"


def impact_range(impact: ImpactRange) -> str:
    """One line describing the estimated monthly change in member cost."""
    if not impact.priced:
        return "not priced"
    if impact.open_ended:
        direction = "more" if impact.direction > 0 else "less"
        if abs(impact.low - impact.high) < 0.005:
            return f"{money(impact.low)} or {direction}"
        return f"{money(impact.low)} to {money(impact.high)} or {direction}"
    if abs(impact.low - impact.high) < 0.005:
        return signed_money(impact.low)
    return f"{signed_money(impact.low)} to {signed_money(impact.high)}"


def change_types(types: tuple[ChangeType, ...]) -> str:
    return ", ".join(t.label for t in types)


def tier_move(config: Config, group: ChangeGroup) -> str:
    before = config.tier_label(group.tier_before)
    after = config.tier_label(group.tier_after)
    if group.tier_before == group.tier_after and group.tier_before is not None:
        return f"tier {group.tier_before} ({before}), unchanged"
    before_text = f"tier {group.tier_before} ({before})" if group.tier_before else before
    after_text = f"tier {group.tier_after} ({after})" if group.tier_after else after
    return f"{before_text} to {after_text}"


def ndc_display(ndc_11: str) -> str:
    if len(ndc_11) != 11:
        return ndc_11
    return f"{ndc_11[:5]}-{ndc_11[5:9]}-{ndc_11[9:]}"


_INCREASE = ("↑", "increase", "up")
_DECREASE = ("↓", "decrease", "down")
_SPANS_ZERO = ("", "spans zero", "flat")
_NO_CHANGE = ("", "no change", "flat")
_NOT_PRICED = ("", "not priced", "flat")


def direction_mark(impact: ImpactRange) -> tuple[str, str, str]:
    """Return the (glyph, word, css class) triple describing the cost range.

    The label follows the sign of the range, not the direction of the rule
    change. A tier move upward can still produce a range that straddles zero
    when the new tier is coinsurance with a low published minimum, and calling
    that an increase would overstate what the data supports. Every label is
    paired with a glyph or a word so the meaning survives grayscale printing.
    """
    if not impact.priced:
        return _NOT_PRICED
    if impact.open_ended:
        # One side is unpriced, so the rule change is the only available signal.
        return _INCREASE if impact.direction > 0 else _DECREASE
    if impact.low >= 0.0 and impact.high > 0.0:
        return _INCREASE
    if impact.high <= 0.0 and impact.low < 0.0:
        return _DECREASE
    if impact.low == 0.0 and impact.high == 0.0:
        return _NO_CHANGE
    return _SPANS_ZERO


def severity_band(config: Config, score: float) -> str:
    return config.report.band_for(score).label


def modal_figure(impact: ImpactRange) -> str:
    """The modal case as a signed figure, or a tight signed range.

    A copay tier gives one exact number. A coinsurance tier gives the published
    bounds for that one channel and supply length, which stays a range rather
    than becoming an invented point estimate.
    """
    if not impact.modal_known:
        return "not published"
    assert impact.modal_low is not None and impact.modal_high is not None
    if abs(impact.modal_low - impact.modal_high) < 0.005:
        return signed_money(impact.modal_low)
    return f"{signed_money(impact.modal_low)} to {signed_money(impact.modal_high)}"


_MODAL_MARKS = {1: ("↑", "increase", "up"), -1: ("↓", "decrease", "down")}


def modal_mark(impact: ImpactRange) -> tuple[str, str, str]:
    """Glyph, word and class for the modal figure's own direction."""
    if not impact.priced or not impact.modal_known:
        return "", "modal case not published", "flat"
    return _MODAL_MARKS.get(impact.modal_direction, ("", "spans zero", "flat"))


def drug_label(name: str | None, ndc_11: str) -> str:
    """Primary line for the drug column. Falls back to the NDC when no name is
    cached, so the cell is never blank and never an error."""
    return name if name else ndc_display(ndc_11)


def plan_list(plans: Sequence[object]) -> str:
    return ", ".join(str(p) for p in plans)
