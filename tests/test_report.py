from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from rxdelta.config import Config
from rxdelta.diff.engine import diff_snapshots
from rxdelta.diff.impact import ImpactRange, build_groups
from rxdelta.limitations import LIMITATIONS
from rxdelta.report import format as fmt
from rxdelta.report import html
from rxdelta.report.html import low_result_notice
from rxdelta.types import ChangeType


def render(conn: sqlite3.Connection, config: Config) -> tuple[str, int]:
    result = diff_snapshots(conn, "2024-01", "2024-02")
    groups = build_groups(conn, config, result)
    return html.render(config, result, groups, generated_at="2024-03-01 00:00 UTC"), len(groups)


def test_report_is_self_contained(loaded: sqlite3.Connection, config: Config) -> None:
    page, _ = render(loaded, config)
    for marker in ("http://", "https://", "<script", "src="):
        assert marker not in page
    assert page.startswith("<!DOCTYPE html>")


def test_report_uses_no_dashes_in_prose(loaded: sqlite3.Connection, config: Config) -> None:
    page, _ = render(loaded, config)
    assert "—" not in page
    assert "–" not in page


def test_report_header_states_the_months_and_totals(
    loaded: sqlite3.Connection, config: Config
) -> None:
    page, _ = render(loaded, config)
    assert "2024-01" in page and "2024-02" in page
    assert "Plans affected" in page
    assert "Drugs affected" in page
    assert "Scope of this comparison" in page


def test_fonts_are_embedded_not_linked(loaded: sqlite3.Connection, config: Config) -> None:
    page, _ = render(loaded, config)
    assert page.count("data:font/woff2;base64,") == 3
    assert "@font-face" in page
    assert "fonts.googleapis" not in page
    assert "fonts.gstatic" not in page


def test_report_declares_a_print_stylesheet(loaded: sqlite3.Connection, config: Config) -> None:
    page, _ = render(loaded, config)
    assert "@media print" in page
    assert "size: letter" in page
    # The header has to repeat on page two and rows must not split across pages.
    assert "display: table-header-group" in page
    assert "break-inside: avoid" in page


def test_limitations_appear_before_the_findings_table(
    loaded: sqlite3.Connection, config: Config
) -> None:
    """The limitations block is the point of the report, not a footnote."""
    page, _ = render(loaded, config)
    assert page.index("What these estimates do not account for") < page.index(
        "Changes ranked by estimated member impact"
    )


def test_report_carries_the_shared_limitations(loaded: sqlite3.Connection, config: Config) -> None:
    page, _ = render(loaded, config)
    for limitation in LIMITATIONS:
        assert limitation in page


def test_low_result_path_states_the_count_and_the_reasons(
    loaded: sqlite3.Connection, config: Config
) -> None:
    # The fixture produces far fewer groups than the default floor.
    page, count = render(loaded, config)
    assert count < config.report.low_result_floor
    assert "Very few changes found" in page
    assert f"found {count} distinct change group(s)" in page
    assert "not a rendering failure" in page
    assert "partially loaded" in page
    assert "little" in page and "formulary movement" in page


def test_low_result_notice_disappears_once_the_floor_is_met(
    loaded: sqlite3.Connection, config: Config
) -> None:
    lenient = replace(config, report=replace(config.report, low_result_floor=1))
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    groups = build_groups(loaded, config, result)
    assert low_result_notice(lenient, result, len(groups)) is None
    page = html.render(lenient, result, groups, generated_at="2024-03-01 00:00 UTC")
    assert "Very few changes found" not in page


def test_low_result_notice_mentions_a_narrow_plan_filter(
    loaded: sqlite3.Connection, config: Config
) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02", plan_filter="H0001")
    notice = low_result_notice(config, result, 1)
    assert notice is not None
    assert any("H0001" in reason for reason in notice.reasons)


def test_report_writes_a_file(loaded: sqlite3.Connection, config: Config, tmp_path: Path) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    groups = build_groups(loaded, config, result)
    out = tmp_path / "nested" / "report.html"
    written = html.write(config, result, groups, out)
    assert written.is_file()
    assert "rxdelta" in written.read_text(encoding="utf-8")


def test_money_formatting() -> None:
    assert fmt.money(12.5) == "$12.50"
    assert fmt.money(-12.5) == "-$12.50"
    assert fmt.signed_money(12.5) == "+$12.50"
    assert fmt.signed_money(-12.5) == "-$12.50"
    assert fmt.signed_money(0.0) == "$0.00"


def test_ndc_display_splits_into_segments() -> None:
    assert fmt.ndc_display("12345678901") == "12345-6789-01"
    assert fmt.ndc_display("short") == "short"


def test_change_type_labels_are_readable() -> None:
    assert fmt.change_types((ChangeType.TIER_UP, ChangeType.PRIOR_AUTH_ADDED)) == (
        "Tier up, Prior auth added"
    )


def impact(
    low: float, high: float, direction: int, *, open_ended: bool = False, priced: bool = True
) -> ImpactRange:
    return ImpactRange(
        low=low, high=high, direction=direction, open_ended=open_ended, priced=priced, basis=""
    )


def test_direction_follows_the_range_not_the_rule_change() -> None:
    """A tier move upward can still produce a range that straddles zero when the
    new tier is coinsurance with a low published minimum. Calling that an
    increase would overstate what the data supports."""
    glyph, word, css = fmt.direction_mark(impact(-18.09, 1056.49, 1))
    assert (glyph, word, css) == ("", "spans zero", "flat")


def test_direction_marks_a_clear_increase_and_decrease() -> None:
    assert fmt.direction_mark(impact(49.41, 1098.99, 1)) == ("↑", "increase", "up")
    assert fmt.direction_mark(impact(-99.0, -69.0, -1)) == ("↓", "decrease", "down")


def test_direction_of_an_open_ended_change_comes_from_the_rule_change() -> None:
    # One side is unpriced, so the sign of the covered side says nothing.
    assert fmt.direction_mark(impact(80.75, 128.25, 1, open_ended=True)) == (
        "↑",
        "increase",
        "up",
    )


def test_unpriced_and_zero_changes_are_labelled_plainly() -> None:
    assert fmt.direction_mark(impact(0.0, 0.0, 0, priced=False)) == ("", "not priced", "flat")
    assert fmt.direction_mark(impact(0.0, 0.0, 0)) == ("", "no change", "flat")


def test_every_direction_label_carries_a_word_not_only_a_color() -> None:
    """Grayscale printouts and color vision deficiency must lose no meaning."""
    cases = [
        impact(1.0, 2.0, 1),
        impact(-2.0, -1.0, -1),
        impact(-1.0, 1.0, 1),
        impact(0.0, 0.0, 0),
        impact(0.0, 0.0, 0, priced=False),
    ]
    for case in cases:
        _glyph, word, _css = fmt.direction_mark(case)
        assert word.strip()


def test_severity_bands_come_from_config(config: Config) -> None:
    assert fmt.severity_band(config, 89.8) == "High"
    assert fmt.severity_band(config, 60.0) == "Elevated"
    assert fmt.severity_band(config, 25.0) == "Moderate"
    assert fmt.severity_band(config, 0.0) == "Low"


def test_every_row_shows_a_severity_band(loaded: sqlite3.Connection, config: Config) -> None:
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    rows = html.build_rows(config, build_groups(loaded, config, result))
    assert rows
    labels = {b.label for b in config.report.severity_bands}
    for row in rows:
        assert row.severity_band in labels
        assert row.direction_word.strip()
