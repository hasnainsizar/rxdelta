"""Write the three month fixture set used by the tool layer and the eval set.

Product names are real drug names so questions read naturally. RXCUIs and NDCs
are synthetic (88xxxxx and labeler 88000) and are not real RxNorm or FDA codes.
Every change is planted by hand below, so the expected diff can be read off this
file. The output is deterministic.

Usage: python scripts/build_ask_fixtures.py --out tests/fixtures/ask
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, replace
from pathlib import Path

MONTHS = ("2026-01", "2026-02", "2026-03")
F1, F2, F3 = "00026123", "00026124", "00026125"
FORMULARIES = (F1, F2, F3)
CONTRACT_YEAR = "2026"


@dataclass(frozen=True)
class Product:
    rxcui: str
    full: str
    ingredients: tuple[str, ...]
    brand: str | None
    tier: int
    pa: bool = False
    st: bool = False
    ql: tuple[float, int] | None = None
    # Formularies the drug is on in January.
    on: tuple[str, ...] = FORMULARIES
    ndcs: int = 1


@dataclass(frozen=True)
class Row:
    tier: int
    pa: bool
    st: bool
    ql: tuple[float, int] | None


PRODUCTS: tuple[Product, ...] = (
    Product(
        "8800101",
        "semaglutide 0.25 MG in 0.19 ML Pen Injector [Ozempic]",
        ("semaglutide",),
        "Ozempic",
        3,
    ),
    Product(
        "8800102",
        "semaglutide 1 MG in 0.75 ML Pen Injector [Ozempic]",
        ("semaglutide",),
        "Ozempic",
        3,
    ),
    Product(
        "8800103",
        "semaglutide 2 MG in 0.75 ML Pen Injector [Ozempic]",
        ("semaglutide",),
        "Ozempic",
        4,
    ),
    Product(
        "8800111",
        "semaglutide 2.4 MG in 0.75 ML Auto-Injector [Wegovy]",
        ("semaglutide",),
        "Wegovy",
        4,
        on=(),
    ),
    Product(
        "8800121",
        "tirzepatide 5 MG in 0.5 ML Auto-Injector [Mounjaro]",
        ("tirzepatide",),
        "Mounjaro",
        3,
    ),
    Product(
        "8800122",
        "tirzepatide 10 MG in 0.5 ML Auto-Injector [Mounjaro]",
        ("tirzepatide",),
        "Mounjaro",
        3,
    ),
    Product(
        "8800131",
        "dulaglutide 1.5 MG in 0.5 ML Pen Injector [Trulicity]",
        ("dulaglutide",),
        "Trulicity",
        3,
        st=True,
        pa=True,
    ),
    Product("8800141", "apixaban 5 MG Oral Tablet [Eliquis]", ("apixaban",), "Eliquis", 3, ndcs=2),
    Product("8800151", "sitagliptin 100 MG Oral Tablet [Januvia]", ("sitagliptin",), "Januvia", 3),
    Product(
        "8800161", "empagliflozin 10 MG Oral Tablet [Jardiance]", ("empagliflozin",), "Jardiance", 3
    ),
    Product(
        "8800171", "dapagliflozin 10 MG Oral Tablet [Farxiga]", ("dapagliflozin",), "Farxiga", 4
    ),
    Product("8800181", "metformin hydrochloride 500 MG Oral Tablet", ("metformin",), None, 1),
    Product("8800182", "metformin hydrochloride 1000 MG Oral Tablet", ("metformin",), None, 1),
    Product(
        "8800191",
        "insulin lispro 100 UNT/ML Injectable Solution [Humalog]",
        ("insulin lispro",),
        "Humalog",
        3,
        on=(F1, F3),
    ),
    Product(
        "8800192",
        "insulin lispro protamine, human 75 UNT/ML / insulin lispro 25 UNT/ML "
        "Injectable Suspension [Humalog Mix 75/25]",
        ("insulin lispro protamine, human", "insulin lispro"),
        "Humalog Mix 75/25",
        3,
    ),
    Product(
        "8800201",
        "insulin glargine 100 UNT/ML Pen Injector [Lantus]",
        ("insulin glargine",),
        "Lantus",
        3,
    ),
    Product(
        "8800211",
        "insulin aspart 100 UNT/ML Injectable Solution [Novolog]",
        ("insulin aspart",),
        "Novolog",
        3,
    ),
    Product(
        "8800221",
        "insulin degludec 100 UNT/ML Pen Injector [Tresiba]",
        ("insulin degludec",),
        "Tresiba",
        4,
    ),
    Product(
        "8800231",
        "insulin glargine 100 UNT/ML Pen Injector [Basaglar]",
        ("insulin glargine",),
        "Basaglar",
        3,
    ),
    Product(
        "8800241",
        "rivaroxaban 20 MG Oral Tablet [Xarelto]",
        ("rivaroxaban",),
        "Xarelto",
        3,
        pa=True,
    ),
    Product(
        "8800251",
        "sacubitril 97 MG / valsartan 103 MG Oral Tablet [Entresto]",
        ("sacubitril", "valsartan"),
        "Entresto",
        3,
        st=True,
    ),
    Product("8800261", "atorvastatin 40 MG Oral Tablet", ("atorvastatin",), None, 1),
    Product("8800271", "lisinopril 10 MG Oral Tablet", ("lisinopril",), None, 1),
    Product("8800281", "amlodipine 5 MG Oral Tablet", ("amlodipine",), None, 1, ql=(30, 30)),
    Product("8800291", "levothyroxine sodium 0.05 MG Oral Tablet", ("levothyroxine",), None, 2),
    Product(
        "8800292",
        "levothyroxine sodium 0.05 MG Oral Tablet [Synthroid]",
        ("levothyroxine",),
        "Synthroid",
        3,
    ),
    Product("8800301", "omeprazole 20 MG Delayed Release Oral Capsule", ("omeprazole",), None, 1),
    Product("8800311", "sertraline 50 MG Oral Tablet", ("sertraline",), None, 1, ql=(30, 30)),
    Product("8800321", "gabapentin 300 MG Oral Capsule", ("gabapentin",), None, 2, ql=(90, 30)),
    Product(
        "8800331",
        "adalimumab 40 MG in 0.4 ML Pen Injector [Humira]",
        ("adalimumab",),
        "Humira",
        5,
    ),
    Product(
        "8800341",
        "etanercept 50 MG/ML Auto-Injector [Enbrel]",
        ("etanercept",),
        "Enbrel",
        5,
        pa=True,
    ),
    Product(
        "8800351",
        "dupilumab 300 MG in 2 ML Pen Injector [Dupixent]",
        ("dupilumab",),
        "Dupixent",
        5,
        pa=True,
        ql=(2, 28),
    ),
    Product(
        "8800361",
        "albuterol 0.09 MG/ACTUAT Metered Dose Inhaler [ProAir]",
        ("albuterol",),
        "ProAir",
        3,
        ql=(1, 30),
    ),
    Product("8800362", "albuterol 0.09 MG/ACTUAT Metered Dose Inhaler", ("albuterol",), None, 2),
    Product(
        "8800371",
        "budesonide 0.16 MG/ACTUAT / formoterol 0.0045 MG/ACTUAT Metered Dose Inhaler [Symbicort]",
        ("budesonide", "formoterol"),
        "Symbicort",
        3,
        st=True,
        ql=(2, 30),
    ),
    Product(
        "8800381",
        "linaclotide 145 MCG Oral Capsule [Linzess]",
        ("linaclotide",),
        "Linzess",
        3,
        on=(F3,),
        ql=(30, 30),
    ),
    Product(
        "8800391",
        "cariprazine 1.5 MG Oral Capsule [Vraylar]",
        ("cariprazine",),
        "Vraylar",
        4,
        pa=True,
        ql=(30, 30),
    ),
    Product(
        "8800401",
        "cyclosporine 0.5 MG/ML Ophthalmic Emulsion [Restasis]",
        ("cyclosporine",),
        "Restasis",
        3,
        on=(F1, F3),
    ),
    Product(
        "8800411",
        "ticagrelor 90 MG Oral Tablet [Brilinta]",
        ("ticagrelor",),
        "Brilinta",
        3,
        ql=(60, 30),
    ),
    Product(
        "8800421",
        "semaglutide 7 MG Oral Tablet [Rybelsus]",
        ("semaglutide",),
        "Rybelsus",
        3,
        pa=True,
    ),
    Product(
        "8800431",
        "liraglutide 6 MG/ML Pen Injector [Victoza]",
        ("liraglutide",),
        "Victoza",
        3,
        ql=(3, 30),
    ),
    Product(
        "8800441",
        "fluticasone furoate 0.1 MG/ACTUAT / umeclidinium 0.0625 MG/ACTUAT / vilanterol "
        "0.025 MG/ACTUAT Dry Powder Inhaler [Trelegy Ellipta]",
        ("fluticasone furoate", "umeclidinium", "vilanterol"),
        "Trelegy Ellipta",
        3,
        on=(F1, F2),
    ),
    Product(
        "8800451",
        "tirzepatide 5 MG in 0.5 ML Auto-Injector [Zepbound]",
        ("tirzepatide",),
        "Zepbound",
        4,
        on=(),
    ),
    Product("8800461", "palbociclib 125 MG Oral Capsule [Ibrance]", ("palbociclib",), "Ibrance", 5),
    Product(
        "8800471",
        "enzalutamide 40 MG Oral Capsule [Xtandi]",
        ("enzalutamide",),
        "Xtandi",
        4,
        ql=(120, 30),
    ),
    Product(
        "8800481",
        "losartan potassium 50 MG Oral Tablet",
        ("losartan",),
        None,
        1,
        on=(F1, F2),
        ql=(30, 30),
    ),
    Product("8800491", "rosuvastatin calcium 10 MG Oral Tablet", ("rosuvastatin",), None, 1),
    Product(
        "8800492",
        "rosuvastatin calcium 10 MG Oral Tablet [Crestor]",
        ("rosuvastatin",),
        "Crestor",
        4,
    ),
)

# (month the change takes effect, formulary, rxcui, field, value). Field is one
# of tier, pa, st, ql (value None removes the limit), add (value is a Row) or
# remove.
_Event = tuple[str, str, str, str, object]

EVENTS: tuple[_Event, ...] = (
    # January to February
    ("2026-02", F1, "8800101", "tier", 4),
    ("2026-02", F1, "8800102", "pa", True),
    ("2026-02", F1, "8800141", "tier", 2),
    ("2026-02", F1, "8800191", "ql", (3, 30)),
    ("2026-02", F1, "8800201", "ql", (5, 30)),
    ("2026-02", F1, "8800211", "tier", 2),
    ("2026-02", F1, "8800331", "st", True),
    ("2026-02", F1, "8800261", "remove", None),
    ("2026-02", F1, "8800241", "pa", False),
    ("2026-02", F1, "8800181", "ql", (60, 30)),
    ("2026-02", F1, "8800351", "ql", (1, 28)),
    ("2026-02", F1, "8800361", "ql", (2, 30)),
    ("2026-02", F1, "8800381", "add", Row(3, False, False, None)),
    ("2026-02", F1, "8800371", "st", False),
    ("2026-02", F2, "8800121", "tier", 4),
    ("2026-02", F2, "8800122", "pa", True),
    ("2026-02", F2, "8800161", "pa", True),
    ("2026-02", F2, "8800171", "tier", 3),
    ("2026-02", F2, "8800221", "ql", (5, 30)),
    ("2026-02", F2, "8800271", "tier", 2),
    ("2026-02", F2, "8800341", "remove", None),
    ("2026-02", F2, "8800401", "add", Row(3, True, False, None)),
    ("2026-02", F2, "8800101", "st", True),
    ("2026-02", F2, "8800321", "ql", (60, 30)),
    ("2026-02", F2, "8800311", "ql", None),
    ("2026-02", F2, "8800391", "pa", False),
    ("2026-02", F2, "8800411", "st", True),
    ("2026-02", F2, "8800251", "tier", 4),
    ("2026-02", F3, "8800191", "tier", 4),
    ("2026-02", F3, "8800231", "ql", (5, 30)),
    ("2026-02", F3, "8800461", "pa", True),
    ("2026-02", F3, "8800301", "remove", None),
    ("2026-02", F3, "8800481", "add", Row(1, False, False, None)),
    ("2026-02", F3, "8800492", "tier", 3),
    ("2026-02", F3, "8800131", "st", False),
    ("2026-02", F3, "8800281", "ql", (60, 30)),
    ("2026-02", F3, "8800292", "pa", True),
    ("2026-02", F3, "8800421", "pa", False),
    ("2026-02", F3, "8800471", "ql", None),
    ("2026-02", F3, "8800431", "ql", (2, 30)),
    # February to March
    ("2026-03", F1, "8800151", "tier", 4),
    ("2026-03", F1, "8800141", "pa", True),
    ("2026-03", F1, "8800103", "tier", 5),
    ("2026-03", F1, "8800441", "tier", 4),
    ("2026-03", F1, "8800201", "tier", 2),
    ("2026-03", F1, "8800261", "add", Row(1, False, False, None)),
    ("2026-03", F1, "8800331", "pa", True),
    ("2026-03", F1, "8800431", "remove", None),
    ("2026-03", F1, "8800351", "st", True),
    ("2026-03", F1, "8800491", "tier", 2),
    ("2026-03", F1, "8800182", "tier", 2),
    ("2026-03", F1, "8800211", "ql", (10, 30)),
    ("2026-03", F1, "8800281", "remove", None),
    ("2026-03", F1, "8800241", "ql", (30, 30)),
    ("2026-03", F1, "8800481", "ql", None),
    ("2026-03", F1, "8800411", "ql", (90, 30)),
    ("2026-03", F1, "8800251", "st", False),
    ("2026-03", F1, "8800161", "tier", 2),
    ("2026-03", F2, "8800102", "tier", 4),
    ("2026-03", F2, "8800121", "pa", True),
    ("2026-03", F2, "8800191", "add", Row(3, False, False, (3, 30))),
    ("2026-03", F2, "8800201", "pa", True),
    ("2026-03", F2, "8800311", "tier", 2),
    ("2026-03", F2, "8800471", "tier", 5),
    ("2026-03", F2, "8800461", "ql", (21, 28)),
    ("2026-03", F2, "8800171", "st", True),
    ("2026-03", F2, "8800131", "pa", False),
    ("2026-03", F2, "8800291", "tier", 1),
    ("2026-03", F2, "8800371", "ql", (1, 30)),
    ("2026-03", F2, "8800361", "remove", None),
    ("2026-03", F2, "8800391", "ql", None),
    ("2026-03", F2, "8800211", "ql", (5, 30)),
    ("2026-03", F2, "8800481", "ql", (60, 30)),
    ("2026-03", F2, "8800251", "st", False),
    ("2026-03", F3, "8800101", "tier", 4),
    ("2026-03", F3, "8800102", "tier", 4),
    ("2026-03", F3, "8800103", "pa", True),
    ("2026-03", F3, "8800192", "ql", (10, 30)),
    ("2026-03", F3, "8800221", "ql", (5, 30)),
    ("2026-03", F3, "8800151", "pa", True),
    ("2026-03", F3, "8800361", "tier", 2),
    ("2026-03", F3, "8800331", "remove", None),
    ("2026-03", F3, "8800441", "add", Row(4, True, False, None)),
    ("2026-03", F3, "8800371", "st", False),
    ("2026-03", F3, "8800311", "ql", (60, 30)),
    ("2026-03", F3, "8800391", "ql", (15, 30)),
    ("2026-03", F3, "8800341", "pa", False),
    ("2026-03", F3, "8800181", "tier", 2),
)


@dataclass(frozen=True)
class Plan:
    contract_id: str
    plan_id: str
    contract_name: str
    plan_name: str
    formulary_id: str
    state: str
    # Scales every copay so plans on one formulary still price differently.
    factor: float
    # Coinsurance dollar bounds for tier 5; zero means none published.
    specialty_bounds: tuple[float, float]
    months: tuple[str, ...] = MONTHS


PLANS: tuple[Plan, ...] = (
    Plan(
        "H1234",
        "001",
        "Sunrise Health Plans, Inc.",
        "Sunrise Basic Rx (HMO)",
        F1,
        "OH",
        1.0,
        (50.0, 250.0),
    ),
    Plan(
        "H1234",
        "002",
        "Sunrise Health Plans, Inc.",
        "Sunrise Plus (HMO-POS)",
        F1,
        "OH",
        0.8,
        (40.0, 200.0),
    ),
    Plan(
        "H5678",
        "001",
        "Lakeside Health, Inc.",
        "Lakeside Value Rx (PPO)",
        F2,
        "MI",
        1.1,
        (60.0, 300.0),
    ),
    Plan(
        "S9012",
        "001",
        "Prairie Insurance Company",
        "Prairie Part D Saver (PDP)",
        F3,
        "KS",
        0.9,
        (0.0, 0.0),
    ),
    Plan(
        "S9012",
        "002",
        "Prairie Insurance Company",
        "Prairie Part D Choice (PDP)",
        F3,
        "KS",
        1.2,
        (45.0, 225.0),
    ),
    Plan(
        "S9012",
        "003",
        "Prairie Insurance Company",
        "Prairie Part D Select (PDP)",
        F3,
        "KS",
        1.0,
        (45.0, 225.0),
        months=("2026-03",),
    ),
)

# Thirty day preferred retail copay per tier before the plan factor.
BASE_COPAY = {1: 0.0, 2: 10.0, 3: 45.0, 4: 95.0}
SPECIALTY_COINSURANCE = 0.25

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
CHANNELS = ("PREF", "NONPREF", "MAIL_PREF", "MAIL_NONPREF")
COST_COLUMNS = (
    "CONTRACT_ID",
    "PLAN_ID",
    "SEGMENT_ID",
    "COVERAGE_LEVEL",
    "TIER",
    "DAYS_SUPPLY",
    *[
        f"{p}_{c}"
        for c in CHANNELS
        for p in ("COST_TYPE", "COST_AMT", "COST_MIN_AMT", "COST_MAX_AMT")
    ],
    "TIER_SPECIALTY_YN",
    "DED_APPLIES_YN",
)


def ndc_for(rxcui: str, index: int) -> str:
    return f"88000-{rxcui[-4:]}-{index + 1:02d}"


def january_state() -> dict[tuple[str, str], Row]:
    state: dict[tuple[str, str], Row] = {}
    for product in PRODUCTS:
        for formulary in product.on:
            state[(formulary, product.rxcui)] = Row(
                product.tier, product.pa, product.st, product.ql
            )
    return state


def apply(state: dict[tuple[str, str], Row], month: str) -> dict[tuple[str, str], Row]:
    out = dict(state)
    for when, formulary, rxcui, field_name, value in EVENTS:
        if when != month:
            continue
        key = (formulary, rxcui)
        if field_name == "add":
            if key in out:
                raise ValueError(f"{key} is already on the formulary in {month}")
            assert isinstance(value, Row)
            out[key] = value
            continue
        if key not in out:
            raise ValueError(f"{key} is not on the formulary in {month}")
        if field_name == "remove":
            del out[key]
        elif field_name == "tier":
            assert isinstance(value, int)
            out[key] = replace(out[key], tier=value)
        elif field_name == "pa":
            assert isinstance(value, bool)
            out[key] = replace(out[key], pa=value)
        elif field_name == "st":
            assert isinstance(value, bool)
            out[key] = replace(out[key], st=value)
        elif field_name == "ql":
            out[key] = replace(out[key], ql=value)  # type: ignore[arg-type]
        else:
            raise ValueError(f"unknown event field {field_name}")
    return out


def states() -> dict[str, dict[tuple[str, str], Row]]:
    by_month: dict[str, dict[tuple[str, str], Row]] = {}
    current = january_state()
    for month in MONTHS:
        current = apply(current, month) if month != MONTHS[0] else current
        by_month[month] = current
    return by_month


def _yn(flag: bool) -> str:
    return "Y" if flag else "N"


def _amount(value: float) -> str:
    return f"{value:g}"


def formulary_rows(month: str, state: dict[tuple[str, str], Row]) -> list[list[str]]:
    ndc_counts = {p.rxcui: p.ndcs for p in PRODUCTS}
    rows = []
    for (formulary, rxcui), row in sorted(state.items()):
        for index in range(ndc_counts[rxcui]):
            rows.append(
                [
                    formulary,
                    str(MONTHS.index(month) + 1),
                    CONTRACT_YEAR,
                    rxcui,
                    ndc_for(rxcui, index),
                    str(row.tier),
                    _yn(row.ql is not None),
                    _amount(row.ql[0]) if row.ql else "",
                    str(row.ql[1]) if row.ql else "",
                    _yn(row.pa),
                    _yn(row.st),
                    "N",
                ]
            )
    return rows


def plan_rows(month: str) -> list[list[str]]:
    return [
        [
            p.contract_id,
            p.plan_id,
            "000",
            p.contract_name,
            p.plan_name,
            p.formulary_id,
            "35.00",
            "590.00",
            "",
            "",
            p.state,
            "",
            "0",
            "N",
        ]
        for p in PLANS
        if month in p.months
    ]


def _money(value: float) -> str:
    return f"{value:.2f}"


def cost_rows(month: str) -> list[list[str]]:
    rows = []
    for plan in PLANS:
        if month not in plan.months:
            continue
        for coverage_level in ("1", "3"):
            for tier in (1, 2, 3, 4, 5):
                for days_supply, multiplier in (("1", 1.0), ("2", 2.5)):
                    legs: list[str] = []
                    for channel in CHANNELS:
                        if channel == "MAIL_NONPREF":
                            legs += ["0", "0", "0", "0"]
                            continue
                        if coverage_level == "3":
                            legs += ["1", "0.00", "", ""]
                            continue
                        if tier == 5:
                            low, high = plan.specialty_bounds
                            legs += [
                                "2",
                                f"{SPECIALTY_COINSURANCE:.2f}",
                                _money(low * multiplier),
                                _money(high * multiplier),
                            ]
                            continue
                        channel_factor = {"PREF": 1.0, "NONPREF": 1.2, "MAIL_PREF": 0.9}[channel]
                        amount = BASE_COPAY[tier] * plan.factor * channel_factor * multiplier
                        legs += ["1", _money(amount), "", ""]
                    rows.append(
                        [
                            plan.contract_id,
                            plan.plan_id,
                            "000",
                            coverage_level,
                            str(tier),
                            days_supply,
                            *legs,
                            _yn(tier == 5),
                            _yn(tier >= 3),
                        ]
                    )
    return rows


def name_rows() -> list[list[str]]:
    rows: list[list[str]] = []
    for product in PRODUCTS:
        rows.append([product.rxcui, "full", product.full])
        rows.extend([product.rxcui, "ingredient", i] for i in product.ingredients)
        if product.brand:
            rows.append([product.rxcui, "brand", product.brand])
    # The diff and loader tests use RXCUIs 100001 to 100018 with no names of
    # their own. Neutral names keep every fixture RXCUI resolvable.
    for index in range(1, 19):
        rows.append([f"1000{index:02d}", "full", f"fixture compound {index:02d} 10 MG Oral Tablet"])
        rows.append([f"1000{index:02d}", "ingredient", f"fixture compound {index:02d}"])
    return rows


def _write(
    path: Path, header: tuple[str, ...] | list[str], rows: list[list[str]], delimiter: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_all(out: Path) -> None:
    for month, state in states().items():
        base = out / month
        _write(
            base / "basic drugs formulary file.txt",
            FORMULARY_COLUMNS,
            formulary_rows(month, state),
            "|",
        )
        _write(base / "plan information.txt", PLAN_COLUMNS, plan_rows(month), "|")
        _write(base / "beneficiary cost file.txt", COST_COLUMNS, cost_rows(month), "|")
    _write(out / "drug_names.csv", ["rxcui", "name_type", "name"], name_rows(), ",")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/ask"))
    args = parser.parse_args()
    write_all(args.out)
    print(f"Wrote {len(MONTHS)} months and drug_names.csv to {args.out}")


if __name__ == "__main__":
    main()
