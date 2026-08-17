from __future__ import annotations

import pytest

from rxdelta.ingest.normalize import (
    normalize_ndc,
    normalize_plan_key,
    parse_flag,
    parse_int,
    parse_optional_float,
    parse_optional_int,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12345-6789-01", "12345678901"),
        ("1234-5678-90", "01234567890"),
        ("12345-678-90", "12345067890"),
        ("12345-6789-0", "12345678900"),
        ("12345678901", "12345678901"),
        ("  12345-6789-01  ", "12345678901"),
    ],
)
def test_normalize_ndc_accepts_known_forms(raw: str, expected: str) -> None:
    result = normalize_ndc(raw)
    assert result.ok
    assert result.ndc_11 == expected
    assert result.raw == raw


def test_hyphenated_forms_pad_the_short_segment() -> None:
    # 4-4-2 pads the first segment, 5-3-2 the second, 5-4-1 the third.
    assert normalize_ndc("1234-5678-90").ndc_11 == "0" + "1234" + "5678" + "90"
    assert normalize_ndc("12345-678-90").ndc_11 == "12345" + "0" + "678" + "90"
    assert normalize_ndc("12345-6789-0").ndc_11 == "12345" + "6789" + "0" + "0"


def test_unhyphenated_10_digit_is_rejected_by_default() -> None:
    result = normalize_ndc("1234567890")
    assert not result.ok
    assert result.ndc_11 is None
    assert result.reason is not None
    assert "ambiguous" in result.reason.lower()
    assert result.raw == "1234567890"


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("assume_4_4_2", "01234567890"),
        ("assume_5_3_2", "12345067890"),
        ("assume_5_4_1", "12345678900"),
    ],
)
def test_unhyphenated_10_digit_honours_the_configured_policy(policy: str, expected: str) -> None:
    result = normalize_ndc("1234567890", unhyphenated_10_policy=policy)
    assert result.ok
    assert result.ndc_11 == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not-an-ndc", "12345-6789-XY", "123", "123456789012", "12345--01", "1-2-3-4"],
)
def test_junk_input_is_rejected_with_a_reason(raw: str) -> None:
    result = normalize_ndc(raw)
    assert not result.ok
    assert result.reason


def test_plan_key_pads_plan_and_segment() -> None:
    key = normalize_plan_key(" h1234 ", "5", "0")
    assert (key.contract_id, key.plan_id, key.segment_id) == ("H1234", "005", "000")
    assert str(key) == "H1234-005-000"


def test_plan_key_is_one_unit() -> None:
    a = normalize_plan_key("H1234", "001", "000")
    b = normalize_plan_key("H1234", "001", "001")
    assert a != b


@pytest.mark.parametrize("raw", ["Y", "y", "yes", "1", "TRUE", "t"])
def test_parse_flag_true(raw: str) -> None:
    assert parse_flag(raw, field="X") is True


@pytest.mark.parametrize("raw", ["N", "n", "no", "0", "FALSE", "", "  "])
def test_parse_flag_false(raw: str) -> None:
    assert parse_flag(raw, field="X") is False


def test_parse_flag_rejects_anything_else() -> None:
    with pytest.raises(ValueError, match="QUANTITY_LIMIT_YN"):
        parse_flag("maybe", field="QUANTITY_LIMIT_YN")


def test_parse_int_rejects_empty_and_junk() -> None:
    with pytest.raises(ValueError, match="TIER"):
        parse_int("", field="TIER")
    with pytest.raises(ValueError, match="TIER"):
        parse_int("high", field="TIER")


def test_optional_parsers_return_none_on_blank_or_junk() -> None:
    assert parse_optional_int("") is None
    assert parse_optional_int("junk") is None
    assert parse_optional_int("30") == 30
    assert parse_optional_float("") is None
    assert parse_optional_float("junk") is None
    assert parse_optional_float("12.5") == 12.5
