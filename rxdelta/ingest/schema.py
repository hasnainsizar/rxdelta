"""Expected column layout per source file type.

CMS revises these layouts between contract years. A mismatch is a hard failure
with the missing and unexpected columns named separately, because a silent
coercion here becomes a wrong number in a report three steps later.
"""

from __future__ import annotations

from dataclasses import dataclass

from rxdelta.types import SchemaError

FORMULARY = "formulary"
PLAN_INFO = "plan_info"
BENEFICIARY_COST = "beneficiary_cost"


@dataclass(frozen=True)
class FileSpec:
    """Declared layout for one source file type."""

    file_type: str
    required_columns: tuple[str, ...]
    # Columns CMS ships that rxdelta does not store. Present in the file but
    # not required; listed so they are not reported as unexpected.
    optional_columns: tuple[str, ...] = ()

    @property
    def known_columns(self) -> frozenset[str]:
        return frozenset(self.required_columns) | frozenset(self.optional_columns)


FILE_SPECS: dict[str, FileSpec] = {
    FORMULARY: FileSpec(
        file_type=FORMULARY,
        required_columns=(
            "FORMULARY_ID",
            "RXCUI",
            "NDC",
            "TIER_LEVEL_VALUE",
            "QUANTITY_LIMIT_YN",
            "QUANTITY_LIMIT_AMOUNT",
            "QUANTITY_LIMIT_DAYS",
            "PRIOR_AUTHORIZATION_YN",
            "STEP_THERAPY_YN",
        ),
        # SELECTED_DRUG_YN marks a drug selected for negotiation under the
        # Medicare Drug Price Negotiation Program (record layout page 7). Not
        # stored: it says nothing about what a member pays this month.
        optional_columns=("FORMULARY_VERSION", "CONTRACT_YEAR", "SELECTED_DRUG_YN"),
    ),
    PLAN_INFO: FileSpec(
        file_type=PLAN_INFO,
        required_columns=(
            "CONTRACT_ID",
            "PLAN_ID",
            "SEGMENT_ID",
            "CONTRACT_NAME",
            "FORMULARY_ID",
            "PLAN_NAME",
        ),
        # The plan information file has no plan type field; record layout page 3
        # lists these instead. SNP and PLAN_SUPPRESSED_YN are read but not
        # stored, since neither changes what a member pays for a drug.
        optional_columns=(
            "PREMIUM",
            "DEDUCTIBLE",
            "MA_REGION_CODE",
            "PDP_REGION_CODE",
            "STATE",
            "COUNTY_CODE",
            "SNP",
            "PLAN_SUPPRESSED_YN",
            "CONTRACT_YEAR",
        ),
    ),
    BENEFICIARY_COST: FileSpec(
        file_type=BENEFICIARY_COST,
        required_columns=(
            "CONTRACT_ID",
            "PLAN_ID",
            "SEGMENT_ID",
            "COVERAGE_LEVEL",
            "TIER",
            "DAYS_SUPPLY",
            "COST_TYPE_PREF",
            "COST_AMT_PREF",
            "COST_MIN_AMT_PREF",
            "COST_MAX_AMT_PREF",
            "COST_TYPE_NONPREF",
            "COST_AMT_NONPREF",
            "COST_MIN_AMT_NONPREF",
            "COST_MAX_AMT_NONPREF",
            "COST_TYPE_MAIL_PREF",
            "COST_AMT_MAIL_PREF",
            "COST_MIN_AMT_MAIL_PREF",
            "COST_MAX_AMT_MAIL_PREF",
            "COST_TYPE_MAIL_NONPREF",
            "COST_AMT_MAIL_NONPREF",
            "COST_MIN_AMT_MAIL_NONPREF",
            "COST_MAX_AMT_MAIL_NONPREF",
        ),
        optional_columns=("TIER_SPECIALTY_YN", "DED_APPLIES_YN", "CONTRACT_YEAR"),
    ),
}


def validate_columns(
    file_type: str, actual_columns: list[str], *, file_name: str, month: str
) -> None:
    """Compare a header row to the declared layout and fail loudly on drift."""
    spec = FILE_SPECS.get(file_type)
    if spec is None:
        raise SchemaError(f"No column layout declared for file type {file_type!r}")

    actual = list(actual_columns)
    actual_set = set(actual)
    missing = [c for c in spec.required_columns if c not in actual_set]
    unexpected = [c for c in actual if c not in spec.known_columns]
    duplicates = sorted({c for c in actual if actual.count(c) > 1})

    if not (missing or unexpected or duplicates):
        return

    parts = [f"Column layout mismatch in {file_name} (month {month}, file type {file_type})."]
    parts.append(f"  Missing columns ({len(missing)}): {', '.join(missing) if missing else 'none'}")
    parts.append(
        f"  Unexpected columns ({len(unexpected)}): "
        f"{', '.join(unexpected) if unexpected else 'none'}"
    )
    if duplicates:
        parts.append(f"  Duplicate columns ({len(duplicates)}): {', '.join(duplicates)}")
    parts.append(
        "  Update the layout in rxdelta/ingest/schema.py if CMS changed the record layout."
    )
    raise SchemaError("\n".join(parts))
