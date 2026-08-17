"""Generate two months of synthetic CMS style snapshots.

The output is deterministic: the RNG is seeded and every planted change is
placed by index, so the demo report is reproducible. The second month carries
at least one drug for every change classification rxdelta can emit.

Usage: python scripts/generate_sample_data.py --out data
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, replace
from pathlib import Path

DELIMITER = "|"
SEED = 20250101

FORMULARY_COUNT = 5
PLAN_COUNT = 40
# How many of the 40 plans adopt each formulary. Uneven on purpose, so the
# affected plan count varies between changes instead of always being 40 / 5.
FORMULARY_PLAN_SHARES = (3, 5, 8, 11, 13)
DRUGS_PER_FORMULARY = 5000
TIERS = (1, 2, 3, 4, 5)
CONTRACTS = ("H1001", "H2002", "H3003", "S4004")
STATES = ("NE", "FL", "CA", "TX", "NY")

NAME_STAMP = "2025-02-01T00:00:00+00:00"

MONTH_FROM = "2025-01"
MONTH_TO = "2025-02"

FORMULARY_COLUMNS = (
    "FORMULARY_ID",
    "FORMULARY_VERSION",
    "CONTRACT_YEAR",
    "RXCUI",
    "NDC",
    "TIER_LEVEL_VALUE",
    "QUANTITY_LIMIT_YN",
    "QUANTITY_LIMIT_AMOUNT",
    "QUANTITY_LIMIT_DAYS",
    "PRIOR_AUTHORIZATION_YN",
    "STEP_THERAPY_YN",
    "SELECTED_DRUG_YN",
)

# Column order follows the PLAN INFORMATION FILE table on page 3 of
# docs/cms-reference/PUFRecordLayout-2026.pdf. There is no plan type field.
PLAN_COLUMNS = (
    "CONTRACT_ID",
    "PLAN_ID",
    "SEGMENT_ID",
    "CONTRACT_NAME",
    "PLAN_NAME",
    "FORMULARY_ID",
    "PREMIUM",
    "DEDUCTIBLE",
    "MA_REGION_CODE",
    "PDP_REGION_CODE",
    "STATE",
    "COUNTY_CODE",
    "SNP",
    "PLAN_SUPPRESSED_YN",
)

COST_COLUMNS = (
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
    "TIER_SPECIALTY_YN",
    "DED_APPLIES_YN",
)

# Copay dollars per 30 day supply by tier, and coinsurance dollar bounds for the
# specialty tier. Shaped to look like a real plan without copying one.
TIER_COPAY = {1: 2.0, 2: 12.0, 3: 45.0, 4: 95.0}
TIER_COINSURANCE_BOUNDS = {5: (120.0, 780.0)}
# Codes and lengths per record layout page 10: 1=30, 2=90, 4=60 days.
DAYS_SUPPLY_CODES = {"1": 30, "2": 90, "4": 60}
# 0=pre-deductible, 1=initial coverage, 3=catastrophic (page 10).
COVERAGE_LEVELS = ("0", "1", "3")
COPAY_CODE = "1"
COINSURANCE_CODE = "2"

# Synthetic drug names. These are invented ingredient stems combined with real
# dose forms and strengths, so the demo report reads like a formulary without
# claiming anything about a real product. The committed name cache is built from
# them, which is what lets `make demo` render names with no network.
NAME_STEMS = (
    "Amlodrine",
    "Benzatide",
    "Carvistan",
    "Dexoprofen",
    "Elvatinib",
    "Fenoxadil",
    "Glimeprost",
    "Hydralium",
    "Irbesartil",
    "Ketorolan",
    "Lansoprine",
    "Metfornix",
    "Naltrexil",
    "Olanzepine",
    "Pravastene",
    "Quetiaprin",
    "Rosuvadex",
    "Sitaglinor",
    "Telmisarox",
    "Ursodiol",
    "Valcyclor",
    "Warfarixin",
    "Xylometrin",
    "Zafirlukan",
)
NAME_FORMS = (
    "tablet",
    "capsule",
    "extended release tablet",
    "oral solution",
    "delayed release capsule",
    "injection",
    "oral suspension",
    "topical cream",
)
NAME_STRENGTHS = (
    "2.5 MG",
    "5 MG",
    "10 MG",
    "20 MG",
    "25 MG",
    "40 MG",
    "50 MG",
    "75 MG",
    "100 MG",
    "200 MG",
    "250 MG",
    "500 MG",
    "5 MG/ML",
    "10 MG/ML",
)


@dataclass(frozen=True)
class Drug:
    ndc_raw: str
    rxcui: str
    tier: int
    prior_auth: bool
    step_therapy: bool
    quantity_limit: bool
    quantity_limit_amount: float | None
    quantity_limit_days: int | None


@dataclass(frozen=True)
class Plan:
    contract_id: str
    plan_id: str
    segment_id: str
    plan_name: str
    contract_name: str
    formulary_id: str


def _yn(value: bool) -> str:
    return "Y" if value else "N"


def drug_name(rxcui: str) -> str:
    """Deterministic plausible name for a synthetic RXCUI."""
    n = int(rxcui)
    stem = NAME_STEMS[n % len(NAME_STEMS)]
    strength = NAME_STRENGTHS[(n // len(NAME_STEMS)) % len(NAME_STRENGTHS)]
    form = NAME_FORMS[(n // (len(NAME_STEMS) * len(NAME_STRENGTHS))) % len(NAME_FORMS)]
    return f"{stem} {strength} oral {form}" if "oral" not in form else f"{stem} {strength} {form}"


def _ndc(rng: random.Random, index: int) -> str:
    """Mostly clean 5-4-2 codes, with a few of each awkward form on purpose.

    The generator plants the shapes the normalizer has to handle, including the
    ambiguous unhyphenated 10 digit case that ends up in rejected_rows.
    """
    labeler = 10000 + (index * 7) % 89999
    product = 1000 + (index * 13) % 8999
    package = 10 + (index * 3) % 89
    shape = index % 97
    if shape == 11:
        return f"{labeler % 10000:04d}-{product:04d}-{package:02d}"
    if shape == 29:
        return f"{labeler:05d}-{product % 1000:03d}-{package:02d}"
    if shape == 53:
        return f"{labeler:05d}-{product:04d}-{package % 10:01d}"
    if shape == 71:
        return f"{labeler:05d}{product:04d}{rng.randint(0, 9)}"
    return f"{labeler:05d}-{product:04d}-{package:02d}"


def _build_drugs(rng: random.Random, formulary_index: int) -> list[Drug]:
    drugs: list[Drug] = []
    for i in range(DRUGS_PER_FORMULARY):
        index = formulary_index * DRUGS_PER_FORMULARY + i
        tier = rng.choices(TIERS, weights=(28, 30, 22, 14, 6))[0]
        quantity_limit = rng.random() < 0.22
        drugs.append(
            Drug(
                ndc_raw=_ndc(rng, index),
                rxcui=str(200000 + index),
                tier=tier,
                prior_auth=rng.random() < 0.12,
                step_therapy=rng.random() < 0.07,
                quantity_limit=quantity_limit,
                quantity_limit_amount=float(rng.choice((30, 60, 90, 120)))
                if quantity_limit
                else None,
                quantity_limit_days=rng.choice((30, 90)) if quantity_limit else None,
            )
        )
    return drugs


def _plant_preconditions(drugs: list[Drug]) -> list[Drug]:
    """Force the baseline state the planted changes need.

    Without this a "step therapy removed" plant lands on a drug that never had
    step therapy, and the classification never fires.
    """
    out = list(drugs)
    out[10] = replace(out[10], tier=2)
    out[11] = replace(out[11], tier=3)
    out[20] = replace(out[20], prior_auth=False)
    out[21] = replace(out[21], prior_auth=True)
    out[30] = replace(out[30], step_therapy=False)
    out[31] = replace(out[31], step_therapy=True)
    out[40] = replace(
        out[40], quantity_limit=False, quantity_limit_amount=None, quantity_limit_days=None
    )
    out[41] = replace(
        out[41], quantity_limit=True, quantity_limit_amount=90.0, quantity_limit_days=30
    )
    out[42] = replace(
        out[42], quantity_limit=True, quantity_limit_amount=90.0, quantity_limit_days=30
    )
    out[43] = replace(
        out[43], quantity_limit=True, quantity_limit_amount=60.0, quantity_limit_days=30
    )
    out[50] = replace(
        out[50],
        tier=2,
        prior_auth=False,
        quantity_limit=False,
        quantity_limit_amount=None,
        quantity_limit_days=None,
    )
    out[60] = replace(out[60], tier=3, prior_auth=False)
    out[61] = replace(out[61], tier=2, prior_auth=False)
    return out


def _plant_changes(drugs: list[Drug], formulary_index: int) -> list[Drug]:
    """Apply one planted change of each classification, then some organic drift.

    Indexes are fixed rather than random so the demo report is stable across
    runs and across machines.
    """
    out = list(drugs)
    # Each formulary moves drugs by its own amount, so the demo shows a spread of
    # tier pairs rather than five copies of the same one.
    step = 1 + formulary_index % 3
    top = min(2 + formulary_index, 5)

    # Tier moved up, and tier moved down.
    out[10] = replace(out[10], tier=min(2 + step, 5))
    out[11] = replace(out[11], tier=1)

    # Prior authorization added, then removed.
    out[20] = replace(out[20], prior_auth=True)
    out[21] = replace(out[21], prior_auth=False)

    # Step therapy added, then removed.
    out[30] = replace(out[30], step_therapy=True)
    out[31] = replace(out[31], step_therapy=False)

    # Quantity limit added, removed, tightened, loosened.
    out[40] = replace(
        out[40], quantity_limit=True, quantity_limit_amount=60.0, quantity_limit_days=30
    )
    out[41] = replace(
        out[41], quantity_limit=False, quantity_limit_amount=None, quantity_limit_days=None
    )
    out[42] = replace(
        out[42], quantity_limit=True, quantity_limit_amount=30.0, quantity_limit_days=30
    )
    out[43] = replace(
        out[43], quantity_limit=True, quantity_limit_amount=180.0, quantity_limit_days=30
    )

    # A drug carrying several changes at once.
    out[50] = replace(
        out[50],
        tier=4,
        prior_auth=True,
        quantity_limit=True,
        quantity_limit_amount=30.0,
        quantity_limit_days=30,
    )

    # A high cost specialty drug moving up, so the report has a headline row.
    out[60] = replace(out[60], tier=5, prior_auth=True)

    # A second headline whose size varies by formulary.
    out[61] = replace(out[61], tier=top, prior_auth=formulary_index % 2 == 0)

    # Drug removed from the formulary.
    del out[70]

    # Organic drift on a slice of the rest, deterministic by index. The stride
    # and the size of each move vary by formulary so the drift does not repeat.
    stride = 31 + formulary_index * 6
    for i in range(100, len(out), stride):
        drug = out[i]
        bucket = (i + formulary_index) % 5
        if bucket == 0:
            out[i] = replace(drug, tier=min(drug.tier + 1, 5))
        elif bucket == 1:
            out[i] = replace(drug, tier=min(drug.tier + 2, 5))
        elif bucket == 2 and drug.tier > 1:
            out[i] = replace(drug, tier=max(drug.tier - 2, 1))
        elif bucket == 3:
            out[i] = replace(drug, prior_auth=not drug.prior_auth)
        else:
            out[i] = replace(drug, step_therapy=not drug.step_therapy)
    return out


def _added_drug(formulary_index: int, offset: int) -> Drug:
    index = 900000 + formulary_index * 100 + offset
    return Drug(
        # Labeler segment starts with a zero, which the generated codes never do,
        # so a newly added drug can never collide with an existing one.
        ndc_raw=f"0{formulary_index}999-{1000 + offset:04d}-{offset % 90 + 10:02d}",
        rxcui=str(900000 + index % 100000),
        tier=4 if offset % 2 == 0 else 5,
        prior_auth=offset % 2 == 0,
        step_therapy=False,
        quantity_limit=offset % 3 == 0,
        quantity_limit_amount=60.0 if offset % 3 == 0 else None,
        quantity_limit_days=30 if offset % 3 == 0 else None,
    )


def _build_plans() -> list[Plan]:
    """Formulary adoption is deliberately uneven.

    An even split gave every change the same affected plan count, which made the
    reach term of the severity score a constant and cost the ranking one of its
    four inputs.
    """
    plans: list[Plan] = []
    assignment: list[int] = []
    for formulary_index, share in enumerate(FORMULARY_PLAN_SHARES):
        assignment.extend([formulary_index] * share)
    for i in range(PLAN_COUNT):
        contract = CONTRACTS[i % len(CONTRACTS)]
        formulary_id = f"{20250 + assignment[i]:05d}"
        plans.append(
            Plan(
                contract_id=contract,
                plan_id=f"{(i // len(CONTRACTS)) + 1:03d}",
                segment_id=f"{i % 3:03d}",
                plan_name=f"{contract} Plan {i + 1:02d}",
                contract_name=f"{contract} HEALTH, INC.",
                formulary_id=formulary_id,
            )
        )
    return plans


def _formulary_rows(month: str, formulary_id: str, drugs: list[Drug]) -> list[list[str]]:
    year = month.split("-")[0]
    version = "1" if month == MONTH_FROM else "2"
    return [
        [
            formulary_id,
            version,
            year,
            drug.rxcui,
            drug.ndc_raw,
            str(drug.tier),
            _yn(drug.quantity_limit),
            f"{drug.quantity_limit_amount:.1f}" if drug.quantity_limit_amount is not None else "",
            str(drug.quantity_limit_days) if drug.quantity_limit_days is not None else "",
            _yn(drug.prior_auth),
            _yn(drug.step_therapy),
            _yn(drug.tier == 5 and drug.prior_auth),
        ]
        for drug in drugs
    ]


def _plan_rows(plans: list[Plan]) -> list[list[str]]:
    return [
        [
            plan.contract_id,
            plan.plan_id,
            plan.segment_id,
            plan.contract_name,
            plan.plan_name,
            plan.formulary_id,
            "34.20",
            "545.00",
            "",
            "",
            STATES[index % len(STATES)],
            f"{10000 + index * 7:05d}",
            "0",
            "N",
        ]
        for index, plan in enumerate(plans)
    ]


def _plan_cost_factor(plan: Plan) -> float:
    """Each plan prices its own benefit.

    Identical cost sharing across all 40 plans made the estimated impact a pure
    function of the tier pair, which collapsed 721 change groups onto 22 distinct
    dollar ranges. Real plans differ, and so do these.
    """
    seed = int(plan.plan_id) * 7 + int(plan.segment_id) * 3 + sum(ord(c) for c in plan.contract_id)
    return 0.72 + (seed % 23) * 0.038


def _cost_rows(month: str, plans: list[Plan]) -> list[list[str]]:
    """Cost sharing rises slightly in the second month, as real plans do."""
    bump = 1.0 if month == MONTH_FROM else 1.08
    rows: list[list[str]] = []
    for plan in plans:
        plan_factor = _plan_cost_factor(plan)
        for coverage_level in COVERAGE_LEVELS:
            for tier in TIERS:
                for days_code, days in DAYS_SUPPLY_CODES.items():
                    scale = days / 30.0
                    rows.append(
                        [
                            plan.contract_id,
                            plan.plan_id,
                            plan.segment_id,
                            coverage_level,
                            str(tier),
                            days_code,
                            *_cost_legs(tier, scale, bump * plan_factor),
                            _yn(tier == 5),
                            _yn(tier >= 3),
                        ]
                    )
    return rows


def _cost_legs(tier: int, scale: float, bump: float) -> list[str]:
    """Four channels: retail preferred, retail non preferred, mail, mail non preferred."""
    legs: list[str] = []
    channel_multipliers = (1.0, 1.35, 0.85, 1.15)
    for multiplier in channel_multipliers:
        if tier in TIER_COPAY:
            amount = TIER_COPAY[tier] * scale * bump * multiplier
            legs.extend([COPAY_CODE, f"{amount:.2f}", "", ""])
        else:
            low, high = TIER_COINSURANCE_BOUNDS[tier]
            legs.extend(
                [
                    COINSURANCE_CODE,
                    "33.0",
                    f"{low * scale * bump * multiplier:.2f}",
                    f"{high * scale * bump * multiplier:.2f}",
                ]
            )
    return legs


def _write(path: Path, columns: tuple[str, ...], rows: list[list[str]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [DELIMITER.join(columns)]
    lines.extend(DELIMITER.join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def generate(out_dir: Path) -> dict[str, int]:
    rng = random.Random(SEED)
    plans = _build_plans()
    formulary_ids = sorted({plan.formulary_id for plan in plans})

    month_from: dict[str, list[Drug]] = {}
    for index, formulary_id in enumerate(formulary_ids):
        month_from[formulary_id] = _plant_preconditions(_build_drugs(rng, index))

    month_to: dict[str, list[Drug]] = {}
    for index, formulary_id in enumerate(formulary_ids):
        changed = _plant_changes(month_from[formulary_id], index)
        changed.extend(_added_drug(index, offset) for offset in range(3))
        month_to[formulary_id] = changed

    counts: dict[str, int] = {}
    # Only drugs whose coverage actually changed can ever appear in a report, so
    # the committed cache covers exactly that set and stays small.
    rxcuis = _changed_rxcuis(month_from, month_to)

    for month, snapshot in ((MONTH_FROM, month_from), (MONTH_TO, month_to)):
        month_dir = out_dir / month
        formulary_rows: list[list[str]] = []
        for formulary_id in formulary_ids:
            formulary_rows.extend(_formulary_rows(month, formulary_id, snapshot[formulary_id]))
        counts[f"{month}/formulary"] = _write(
            month_dir / "basic drugs formulary file.txt", FORMULARY_COLUMNS, formulary_rows
        )
        counts[f"{month}/plan_info"] = _write(
            month_dir / "plan information.txt", PLAN_COLUMNS, _plan_rows(plans)
        )
        counts[f"{month}/beneficiary_cost"] = _write(
            month_dir / "beneficiary cost file.txt", COST_COLUMNS, _cost_rows(month, plans)
        )
    counts["reference/drug_names"] = _write_names(out_dir, sorted(rxcuis, key=int))
    return counts


def _changed_rxcuis(month_from: dict[str, list[Drug]], month_to: dict[str, list[Drug]]) -> set[str]:
    """RXCUIs whose coverage differs between the two months, in either direction."""
    changed: set[str] = set()
    for formulary_id, before in month_from.items():
        after = month_to[formulary_id]
        by_ndc_before = {d.ndc_raw: d for d in before}
        by_ndc_after = {d.ndc_raw: d for d in after}
        for ndc in set(by_ndc_before) | set(by_ndc_after):
            a, b = by_ndc_before.get(ndc), by_ndc_after.get(ndc)
            if a != b:
                changed.update(d.rxcui for d in (a, b) if d is not None)
    return changed


def _write_names(out_dir: Path, rxcuis: list[str]) -> int:
    """Write the committed name cache the report reads offline.

    A slice of RXCUIs is left out on purpose so the demo exercises the fallback
    to the NDC, which is the path a real run hits whenever RxNav has no concept.
    """
    path = out_dir / "reference" / "drug_names.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["rxcui,name,source,fetched_at"]
    for rxcui in rxcuis:
        if int(rxcui) % 29 == 0:
            continue
        lines.append(f"{rxcui},{drug_name(rxcui)},synthetic,{NAME_STAMP}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines) - 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("data"), help="Directory to write month folders into."
    )
    args = parser.parse_args()
    counts = generate(args.out)
    for name, count in counts.items():
        print(f"{count:>8,}  {name}")
    print(f"\nWrote {sum(counts.values()):,} rows to {args.out}")


if __name__ == "__main__":
    main()
