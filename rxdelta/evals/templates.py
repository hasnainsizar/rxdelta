"""Question templates and phrasing, as data.

Placeholders: {drug} {drug2} {month} {from} {to} {formulary} {other} {plan}
{plan_name} {class} {change} {change_casual} {change_question}. `scope` says
which of formulary, plan or neither the template names, so the builder fills
the right slots. `kind` ties a multi step or no answer template to the gold
query that answers it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from rxdelta.config import Category
from rxdelta.types import ChangeType

Register = Literal["formal", "casual"]
Scope = Literal["none", "formulary", "plan"]


@dataclass(frozen=True)
class Template:
    id: str
    text: str
    register: Register
    scope: Scope = "none"
    kind: str = ""


@dataclass(frozen=True)
class ChangePhrase:
    past: str  # "Which drugs ___ on formulary X?"
    casual: str  # "which drugs ___ on X?"
    question: str  # "Did Eliquis ___ on formulary X?"


CHANGE_PHRASES: dict[ChangeType, ChangePhrase] = {
    ChangeType.TIER_UP: ChangePhrase(
        "moved to a higher tier", "moved up a tier", "move to a higher tier"
    ),
    ChangeType.TIER_DOWN: ChangePhrase(
        "moved to a lower tier", "moved down a tier", "move to a lower tier"
    ),
    ChangeType.PRIOR_AUTH_ADDED: ChangePhrase(
        "had prior authorization added", "got PA added", "get prior authorization added"
    ),
    ChangeType.PRIOR_AUTH_REMOVED: ChangePhrase(
        "had prior authorization removed",
        "lost the PA requirement",
        "lose its prior authorization requirement",
    ),
    ChangeType.STEP_THERAPY_ADDED: ChangePhrase(
        "had step therapy added", "got step therapy added", "get step therapy added"
    ),
    ChangeType.STEP_THERAPY_REMOVED: ChangePhrase(
        "had step therapy removed", "dropped step therapy", "lose its step therapy requirement"
    ),
    ChangeType.QUANTITY_LIMIT_ADDED: ChangePhrase(
        "had a quantity limit added", "got a quantity limit", "get a quantity limit"
    ),
    ChangeType.QUANTITY_LIMIT_REMOVED: ChangePhrase(
        "had the quantity limit removed", "lost the quantity limit", "lose its quantity limit"
    ),
    ChangeType.QUANTITY_LIMIT_TIGHTENED: ChangePhrase(
        "had the quantity limit tightened",
        "got a tighter quantity limit",
        "get a tighter quantity limit",
    ),
    ChangeType.QUANTITY_LIMIT_LOOSENED: ChangePhrase(
        "had the quantity limit loosened",
        "got a looser quantity limit",
        "get a looser quantity limit",
    ),
    ChangeType.DRUG_ADDED: ChangePhrase(
        "were newly covered", "got added", "get added to the formulary"
    ),
    ChangeType.DRUG_REMOVED: ChangePhrase(
        "were dropped from coverage", "got dropped", "get dropped from the formulary"
    ),
}

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


def month_phrases(month: str, *, single_year: bool) -> list[str]:
    """Ways to say a month. Bare month names only when every loaded month is in
    one year, and "the start of the year" only for January then."""
    year, number = month.split("-")
    name = MONTH_NAMES[int(number) - 1]
    phrases = [f"{name} {year}", f"{name[:3]} {year}", month]
    if single_year:
        phrases += [name, name[:3].lower()]
        if number == "01":
            phrases.append("the start of the year")
    return phrases


TEMPLATES: dict[str, tuple[Template, ...]] = {
    "coverage_lookup": (
        Template(
            "coverage_lookup.formal.1",
            "What tier is {drug} on formulary {formulary} in {month}?",
            "formal",
            "formulary",
        ),
        Template(
            "coverage_lookup.formal.2",
            "How is {drug} covered on plan {plan} in {month}, including any prior "
            "authorization, step therapy or quantity limit?",
            "formal",
            "plan",
        ),
        Template(
            "coverage_lookup.casual.1",
            "whats the tier for {drug} on {formulary} in {month}",
            "casual",
            "formulary",
        ),
        Template(
            "coverage_lookup.casual.2",
            "is {drug} covered on plan {plan} in {month}? any restrictions?",
            "casual",
            "plan",
        ),
        Template(
            "coverage_lookup.casual.3",
            "does {drug} need prior auth on formulary {formulary} in {month}?",
            "casual",
            "formulary",
        ),
    ),
    "drug_change": (
        Template(
            "drug_change.formal.1",
            "Did anything change for {drug} between {from} and {to}?",
            "formal",
        ),
        Template(
            "drug_change.formal.2",
            "What changed in coverage for {drug} on formulary {formulary} from {from} to {to}?",
            "formal",
            "formulary",
        ),
        Template("drug_change.casual.1", "did {drug} change at all from {from} to {to}", "casual"),
        Template(
            "drug_change.casual.2",
            "any changes to {drug} on {formulary} between {from} and {to}?",
            "casual",
            "formulary",
        ),
        Template("drug_change.casual.3", "has {drug} changed lately?", "casual", kind="defaults"),
    ),
    "filtered_list": (
        Template(
            "filtered_list.formal.1",
            "Which drugs {change} on formulary {formulary} from {from} to {to}?",
            "formal",
            "formulary",
        ),
        Template(
            "filtered_list.formal.2",
            "List the drugs that {change} between {from} and {to}.",
            "formal",
        ),
        Template(
            "filtered_list.casual.1",
            "which drugs {change_casual} on {formulary} between {from} and {to}?",
            "casual",
            "formulary",
        ),
        Template(
            "filtered_list.casual.2",
            "what drugs {change_casual} on plan {plan} from {from} to {to}",
            "casual",
            "plan",
        ),
    ),
    "count": (
        Template("count.formal.1", "How many drugs {change} between {from} and {to}?", "formal"),
        Template(
            "count.formal.2",
            "How many drugs {change} on formulary {formulary} from {from} to {to}?",
            "formal",
            "formulary",
        ),
        Template("count.casual.1", "how many drugs {change_casual} from {from} to {to}", "casual"),
        Template(
            "count.casual.2",
            "number of drugs that {change_casual} on {formulary} between {from} and {to}?",
            "casual",
            "formulary",
        ),
        Template(
            "count.casual.3",
            "how many meds {change_casual} on plan {plan} between {from} and {to}",
            "casual",
            "plan",
        ),
    ),
    "over_cap": (
        Template(
            "over_cap.formal.1",
            "List all of the drugs that {change} between {from} and {to}.",
            "formal",
        ),
        Template(
            "over_cap.formal.2",
            "Which drugs {change} on formulary {formulary} from {from} to {to}? I need the "
            "complete list.",
            "formal",
            "formulary",
        ),
        Template(
            "over_cap.casual.1",
            "what are all the drugs that {change_casual} from {from} to {to}",
            "casual",
        ),
        Template(
            "over_cap.casual.2",
            "show me every drug that {change_casual} on {formulary} between {from} and {to}",
            "casual",
            "formulary",
        ),
    ),
    "cost": (
        Template(
            "cost.formal.1",
            "How much more might someone pay each month for {drug} on plan {plan} after the "
            "change from {from} to {to}?",
            "formal",
            "plan",
        ),
        Template(
            "cost.formal.2",
            "What is the estimated change in monthly cost for {drug} on plan {plan} between "
            "{from} and {to}?",
            "formal",
            "plan",
        ),
        Template(
            "cost.casual.1",
            "did {drug} get more expensive on plan {plan} from {from} to {to}?",
            "casual",
            "plan",
        ),
        Template(
            "cost.casual.2",
            "how much extra will {drug} cost on {plan} going from {from} to {to}",
            "casual",
            "plan",
        ),
    ),
    "multi_step": (
        Template(
            "multi_step.class.formal.1",
            "Of the {class} products, which ones {change} on formulary {formulary} between "
            "{from} and {to}, and what tier are they on now?",
            "formal",
            "formulary",
            "class_changes",
        ),
        Template(
            "multi_step.class.casual.1",
            "which {class} drugs {change_casual} on {formulary} from {from} to {to}, and what "
            "tier are they now",
            "casual",
            "formulary",
            "class_changes",
        ),
        Template(
            "multi_step.tier_pa.formal.1",
            "Which drugs moved to a higher tier on formulary {formulary} between {from} and "
            "{to} and also require prior authorization in {to}?",
            "formal",
            "formulary",
            "tier_up_with_pa",
        ),
        Template(
            "multi_step.removed_covered.formal.1",
            "Which drugs were dropped from formulary {formulary} between {from} and {to} but "
            "are still covered on formulary {other} in {to}?",
            "formal",
            "formulary",
            "removed_still_covered",
        ),
        Template(
            "multi_step.plan_name.casual.1",
            "on the {plan_name} plan, what drugs {change_casual} between {from} and {to}?",
            "casual",
            "plan",
            "plan_by_name",
        ),
    ),
    "no_answer": (
        Template(
            "no_answer.unknown_drug.formal.1",
            "What tier is {drug} on formulary {formulary} in {month}?",
            "formal",
            "formulary",
            "unknown_drug",
        ),
        Template(
            "no_answer.unknown_drug.casual.1",
            "did {drug} change from {from} to {to}?",
            "casual",
            "none",
            "unknown_drug",
        ),
        Template(
            "no_answer.month.formal.1",
            "What changed for {drug} between {from} and {to}?",
            "formal",
            "none",
            "month_not_loaded",
        ),
        Template(
            "no_answer.month.casual.1",
            "whats the tier for {drug} on {formulary} in {month}",
            "casual",
            "formulary",
            "month_not_loaded",
        ),
        Template(
            "no_answer.change.formal.1",
            "Did {drug} {change_question} on formulary {formulary} between {from} and {to}?",
            "formal",
            "formulary",
            "no_matching_changes",
        ),
        Template(
            "no_answer.change.casual.1",
            "did {drug} {change_question} from {from} to {to}?",
            "casual",
            "none",
            "no_matching_changes",
        ),
        Template(
            "no_answer.not_covered.formal.1",
            "What tier is {drug} on formulary {formulary} in {month}?",
            "formal",
            "formulary",
            "not_on_formulary",
        ),
        Template(
            "no_answer.not_covered.casual.1",
            "is {drug} on plan {plan} in {month}? what tier?",
            "casual",
            "plan",
            "not_on_formulary",
        ),
    ),
    "ambiguous": (
        Template("ambiguous.formal.1", "Did {drug} change between {from} and {to}?", "formal"),
        Template(
            "ambiguous.formal.2",
            "What changed for {drug} on formulary {formulary} from {from} to {to}?",
            "formal",
            "formulary",
        ),
        Template("ambiguous.casual.1", "any updates on {drug} from {from} to {to}?", "casual"),
        Template(
            "ambiguous.casual.2",
            "did anything happen with {drug} on {formulary} between {from} and {to}",
            "casual",
            "formulary",
        ),
    ),
    "out_of_scope": (
        Template("out_of_scope.formal.1", "Should I switch from {drug} to {drug2}?", "formal"),
        Template(
            "out_of_scope.formal.2", "Is it safe to take {drug} together with {drug2}?", "formal"
        ),
        Template(
            "out_of_scope.formal.3",
            "Which is better for my condition, {drug} or {drug2}?",
            "formal",
        ),
        Template("out_of_scope.casual.1", "what dose of {drug} should i be taking", "casual"),
        Template(
            "out_of_scope.casual.2",
            "should i stop taking {drug} since it costs more now",
            "casual",
        ),
        Template("out_of_scope.casual.3", "can i split my {drug} pills to save money", "casual"),
    ),
}

# Categories a misspelled drug name is planted into.
TYPO_BASES: tuple[Category, ...] = ("coverage_lookup", "drug_change", "cost")

# Drug names that do not exist. The builder still checks each one finds no match.
MADE_UP_DRUGS = (
    "Zorvalex", "Brintafen", "Quelmora", "Davexitol", "Norpelin", "Tavrocet",
    "Ulmazine", "Fexorbane", "Krystodar", "Velmatrin", "Opravin", "Sentrazol",
)  # fmt: skip

# Neighbours on a QWERTY keyboard, for nearby-key typos.
KEY_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")
