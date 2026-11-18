"""`rxdelta evalset build`: a deterministic eval set with independent gold answers.

Facts are drawn from the gold module's own SQL. The drug matcher is used only to
choose drug mentions that resolve the way the question intends (a brand that
maps to exactly one RXCUI, a misspelling that still matches, a made-up name that
matches nothing). Same database, config and seed give a byte identical file.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rxdelta.ask.answer import (
    ChangeAnswer,
    ClarifyAnswer,
    DrugListAnswer,
    NoDataAnswer,
    QuestionContext,
    StructuredAnswer,
)
from rxdelta.config import CATEGORIES, Category, Config
from rxdelta.db import queries
from rxdelta.evals import gold
from rxdelta.evals.items import (
    Difficulty,
    EvalItem,
    GoldSpec,
    HandwrittenItem,
    manifest_path,
    read_handwritten,
    sha256_file,
    write_items,
)
from rxdelta.evals.templates import (
    CHANGE_PHRASES,
    KEY_ROWS,
    MADE_UP_DRUGS,
    TEMPLATES,
    TYPO_BASES,
    Template,
    month_phrases,
)
from rxdelta.names.match import DrugMatcher
from rxdelta.names.normalize import normalize_name
from rxdelta.types import ChangeType, RxdeltaError

SET_PREFIX = "rxd-v1"
_STRENGTH = re.compile(r"(\d+(?:\.\d+)?)\s*(MG|MCG|UNT/ML|MG/ML)\b")
# The dose form words after the strength, up to a bracketed brand: "Oral Tablet".
_FORM = re.compile(r"(?:MG|MCG|UNT/ML|MG/ML)\s+((?:[A-Za-z]+\s?){1,3}?)\s*(?:\[|$)")
_PARENS = re.compile(r"\s*\([^)]*\)\s*")


class BuildError(RxdeltaError):
    """The eval set cannot be built from this database."""


@dataclass
class Candidate:
    category: Category
    template_id: str
    register: str
    question: str
    query: str
    args: dict[str, Any]
    context: QuestionContext = field(default_factory=QuestionContext)
    tags: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        canonical = {k: (v.lower() if isinstance(v, str) else v) for k, v in self.args.items()}
        return self.template_id + json.dumps(canonical, sort_keys=True)


@dataclass(frozen=True)
class PlanRef:
    plan_id: str
    name: str
    formulary_id: str


class Facts:
    """Everything the generators sample from, read once."""

    def __init__(
        self, conn: sqlite3.Connection, config: Config, month_from: str, month_to: str
    ) -> None:
        self.conn = conn
        self.config = config
        self.matcher = DrugMatcher(conn, config.match)
        loaded = gold.months(conn)
        for month in (month_from, month_to):
            if month not in loaded:
                raise BuildError(f"Month {month} is not loaded. Loaded: {', '.join(loaded)}.")
        self.loaded = loaded
        self.months = [m for m in loaded if month_from <= m <= month_to]
        if len(self.months) < 2:
            raise BuildError("The eval set needs at least two loaded months in range.")
        self.pairs = [(a, b) for i, a in enumerate(self.months) for b in self.months[i + 1 :]]
        self.single_year = len({m[:4] for m in loaded}) == 1
        self.changes = {p: gold.changes(conn, *p) for p in self.pairs}
        self.coverage: dict[tuple[str, str], list[str]] = {}
        self.formularies: dict[str, list[str]] = {}
        for month in self.months:
            rows = conn.execute(
                "SELECT formulary_id, rxcui FROM formulary WHERE snapshot_month = ? "
                "GROUP BY formulary_id, rxcui ORDER BY formulary_id, rxcui",
                (month,),
            ).fetchall()
            for formulary, rxcui in rows:
                self.coverage.setdefault((month, str(formulary)), []).append(str(rxcui))
            self.formularies[month] = sorted({str(r[0]) for r in rows})
        self.plan_words = {
            month: [
                (
                    f"{p.key.contract_id}-{p.key.plan_id}",
                    frozenset(normalize_name(f"{p.plan_name} {p.contract_name}").split()),
                )
                for p in queries.plans(conn, month)
            ]
            for month in self.months
        }
        self.plans = {
            month: [
                PlanRef(
                    f"{p.key.contract_id}-{p.key.plan_id}",
                    _PARENS.sub(" ", p.plan_name).strip(),
                    p.formulary_id,
                )
                for p in queries.plans(conn, month)
                if p.key.segment_id == "000"
            ]
            for month in self.months
        }
        named = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT rxcui FROM drug_names WHERE rxcui IN "
                "(SELECT rxcui FROM formulary) OR rxcui IN (SELECT rxcui FROM drug_names "
                "WHERE name_type = 'brand') ORDER BY rxcui"
            ).fetchall()
        ]
        self.mentions = {r: self._name_mentions(r) for r in named}
        self.mentions = {r: m for r, m in self.mentions.items() if m}
        self.ndcs = {
            str(r[0]): str(r[1])
            for r in conn.execute(
                "SELECT rxcui, MIN(ndc_raw) FROM formulary GROUP BY rxcui ORDER BY rxcui"
            ).fetchall()
        }
        self.ambiguous = self._ambiguous_names()
        self.classes = self._class_tokens()
        self.made_up = [n for n in MADE_UP_DRUGS if not self.matcher.match_all(n)]

    def resolves(self, mention: str, *, fuzzy: bool = True) -> set[str]:
        return {m.rxcui for m in self.matcher.match_all(mention, fuzzy=fuzzy)}

    def _name_mentions(self, rxcui: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT name_type, name FROM drug_names WHERE rxcui = ? ORDER BY name_type, name",
            (rxcui,),
        ).fetchall()
        brands = [str(n) for t, n in rows if t == "brand"]
        ingredients = [str(n) for t, n in rows if t == "ingredient"]
        full = next((str(n) for t, n in rows if t == "full"), "")
        strength = _STRENGTH.search(full)
        suffix = f" {strength.group(1)} {strength.group(2).lower()}" if strength else ""
        form = _FORM.search(full)
        candidates = brands + ingredients
        if suffix:
            candidates += [b + suffix for b in brands] + [i + suffix for i in ingredients]
            if form:
                with_form = f"{suffix} {form.group(1).strip().lower()}"
                candidates += [i + with_form for i in ingredients]
                candidates += [b + with_form for b in brands]
        # A mention must resolve exactly, never by resemblance to another name.
        return [c for c in dict.fromkeys(candidates) if self.resolves(c, fuzzy=False) == {rxcui}]

    def _ambiguous_names(self) -> list[tuple[str, list[str]]]:
        rows = self.conn.execute(
            "SELECT DISTINCT name FROM drug_names WHERE name_type IN ('brand', 'ingredient') "
            "ORDER BY name"
        ).fetchall()
        found = []
        for (name,) in rows:
            matched = self.resolves(str(name), fuzzy=False)
            by_sql = set(gold.rxcuis_named(self.conn, normalize_name(str(name))))
            # The tool returns at most its row cap, so a name with more drugs
            # than that cannot be clarified in full.
            if 1 < len(matched) <= self.config.tools.cap("find_drug") and matched == by_sql:
                found.append((str(name), sorted(matched)))
        return found

    def _class_tokens(self) -> list[tuple[str, list[str]]]:
        tokens = sorted(
            {
                token
                for (name,) in self.conn.execute(
                    "SELECT name_norm FROM drug_names WHERE name_type = 'ingredient'"
                ).fetchall()
                for token in str(name).split()
                if token.isalpha() and len(token) > 4
            }
        )
        found = []
        for token in tokens:
            members = gold.rxcuis_with_token(self.conn, token)
            products = {gold.product_of(self.conn, r) for r in members}
            if (
                3 <= len(members) <= self.config.tools.cap("find_drug")
                and len(products) >= 3
                and self.resolves(token, fuzzy=False) == set(members)
            ):
                found.append((token, members))
        return found

    def names_one_plan(self, month: str, name: str) -> bool:
        """Whether a plan name, matched the way find_plan matches names, picks
        exactly one plan. Real plan names repeat across contracts."""
        wanted = frozenset(normalize_name(name).split())
        return sum(1 for _, words in self.plan_words[month] if wanted <= words) == 1

    def plans_on(self, month: str, formulary: str) -> list[PlanRef]:
        return [p for p in self.plans[month] if p.formulary_id == formulary]


class Generator:
    def __init__(self, facts: Facts, rng: random.Random) -> None:
        self.f = facts
        self.rng = rng
        self.settings = facts.config.evalset
        self.id_share = self.settings.id_share

    # Slot helpers

    def month_text(self, month: str) -> str:
        if month not in self.f.loaded:
            return month_phrases(month, single_year=False)[self.rng.randrange(2)]
        return self.rng.choice(month_phrases(month, single_year=self.f.single_year))

    def pair_text(self, pair: tuple[str, str]) -> tuple[str, str]:
        while True:
            a, b = self.month_text(pair[0]), self.month_text(pair[1])
            if a != b and "start of the year" not in b:
                return a, b

    def mention(self, rxcui: str, *, allow_id: bool = True) -> str | None:
        names = self.f.mentions.get(rxcui, [])
        # An ID only at the configured share, never as a fallback for a drug with
        # no unique name; such a drug is skipped instead.
        if allow_id and self.rng.random() < self.id_share:
            if self.rng.random() < 0.5 or rxcui not in self.f.ndcs:
                return f"RXCUI {rxcui}"
            return f"NDC {self.f.ndcs[rxcui]}"
        if not names:
            return None
        weights = [3] + [1] * (len(names) - 1)
        return self.rng.choices(names, weights=weights)[0]

    def render(self, template: Template, slots: dict[str, str]) -> str:
        values = dict(slots)
        if template.register == "casual":
            for key in ("drug", "drug2", "class"):
                if key in values and not values[key].upper().startswith(("RXCUI", "NDC")):
                    values[key] = values[key].lower()
        return template.text.format(**values)

    def candidate(
        self,
        category: Category,
        template: Template,
        slots: dict[str, str],
        query: str,
        args: dict[str, Any],
        **extra: Any,
    ) -> Candidate:
        return Candidate(
            category=category,
            template_id=template.id,
            register=template.register,
            question=self.render(template, slots),
            query=query,
            args=args,
            **extra,
        )

    def change_slots(self, change_type: ChangeType) -> dict[str, str]:
        phrase = CHANGE_PHRASES[change_type]
        return {
            "change": phrase.past,
            "change_casual": phrase.casual,
            "change_question": phrase.question,
        }

    # Categories

    def coverage_lookup(self, t: Template, *, allow_id: bool = True) -> Candidate | None:
        month = self.rng.choice(self.f.months)
        formulary = self.rng.choice(self.f.formularies[month])
        rxcui = self.rng.choice(self.f.coverage[(month, formulary)])
        drug = self.mention(rxcui, allow_id=allow_id)
        if drug is None:
            return None
        args: dict[str, Any] = {"drug": drug, "rxcui": rxcui, "month": month}
        slots = {"drug": drug, "month": self.month_text(month), "formulary": formulary}
        if t.scope == "plan":
            plans = self.f.plans_on(month, formulary)
            if not plans:
                return None
            plan = self.rng.choice(plans)
            args["plan_id"] = plan.plan_id
            slots["plan"] = plan.plan_id
        else:
            args["formulary_id"] = formulary
        return self.candidate("coverage_lookup", t, slots, "coverage", args)

    def drug_change(self, t: Template, *, allow_id: bool = True) -> Candidate | None:
        context = QuestionContext()
        if t.kind == "defaults":
            pair = (self.f.months[0], self.f.months[-1])
            context = QuestionContext(default_from=pair[0], default_to=pair[1])
        else:
            pair = self.rng.choice(self.f.pairs)
        row = self.rng.choice(self.f.changes[pair])
        drug = self.mention(row.rxcui, allow_id=allow_id)
        if drug is None:
            return None
        start, end = self.pair_text(pair)
        args: dict[str, Any] = {
            "drug": drug,
            "rxcuis": [row.rxcui],
            "from": pair[0],
            "to": pair[1],
        }
        if t.scope == "formulary":
            args["formulary_id"] = row.formulary_to
        slots = {"drug": drug, "from": start, "to": end, "formulary": row.formulary_to}
        return self.candidate("drug_change", t, slots, "drug_change", args, context=context)

    def _list_args(
        self, t: Template, pair: tuple[str, str], row: gold.GoldChange
    ) -> tuple[dict[str, Any], dict[str, str]] | None:
        start, end = self.pair_text(pair)
        args: dict[str, Any] = {
            "from": pair[0],
            "to": pair[1],
            "change_types": [row.change_type.value],
        }
        slots = {"from": start, "to": end, **self.change_slots(row.change_type)}
        if t.scope == "formulary":
            args["formulary_id"] = row.formulary_to
            slots["formulary"] = row.formulary_to
        elif t.scope == "plan":
            plans = self.f.plans_on(pair[1], row.formulary_to)
            if not plans:
                return None
            plan = self.rng.choice(plans)
            args["plan_id"] = plan.plan_id
            args["plan_query"] = plan.plan_id
            slots["plan"] = plan.plan_id
        return args, slots

    def filtered_list(self, t: Template) -> Candidate | None:
        pair = self.rng.choice(self.f.pairs)
        built = self._list_args(t, pair, self.rng.choice(self.f.changes[pair]))
        if built is None:
            return None
        args, slots = built
        return self.candidate("filtered_list", t, slots, "filtered_list", args)

    def count(self, t: Template) -> Candidate | None:
        pair = self.rng.choice(self.f.pairs)
        built = self._list_args(t, pair, self.rng.choice(self.f.changes[pair]))
        if built is None:
            return None
        args, slots = built
        return self.candidate("count", t, slots, "count_drugs", args)

    def cost(self, t: Template, *, allow_id: bool = True) -> Candidate | None:
        pair = self.rng.choice(self.f.pairs)
        if self.rng.random() < self.settings.cost_tier_move_share:
            moves = [
                r
                for r in self.f.changes[pair]
                if r.change_type in (ChangeType.TIER_UP, ChangeType.TIER_DOWN)
            ]
            if not moves:
                return None
            row = self.rng.choice(moves)
            rxcui, formulary = row.rxcui, row.formulary_to
        else:
            formulary = self.rng.choice(self.f.formularies[pair[1]])
            rxcui = self.rng.choice(self.f.coverage[(pair[1], formulary)])
        plans = self.f.plans_on(pair[1], formulary)
        drug = self.mention(rxcui, allow_id=allow_id)
        if not plans or drug is None:
            return None
        plan = self.rng.choice(plans)
        start, end = self.pair_text(pair)
        args = {
            "drug": drug,
            "rxcui": rxcui,
            "from": pair[0],
            "to": pair[1],
            "plan_id": plan.plan_id,
        }
        slots = {"drug": drug, "from": start, "to": end, "plan": plan.plan_id}
        return self.candidate("cost", t, slots, "cost_change", args)

    def multi_step(self, t: Template) -> Candidate | None:
        pair = self.rng.choice(self.f.pairs)
        start, end = self.pair_text(pair)
        slots: dict[str, str] = {"from": start, "to": end}
        rows = self.f.changes[pair]
        if t.kind == "class_changes":
            if not self.f.classes:
                return None
            token, members = self.rng.choice(self.f.classes)
            formulary = self.rng.choice(self.f.formularies[pair[1]])
            types = sorted(
                {
                    r.change_type.value
                    for r in rows
                    if r.rxcui in members
                    and r.formulary_to == formulary
                    and r.change_type is not ChangeType.DRUG_REMOVED
                }
            )
            if not types:
                return None
            change_type = ChangeType(self.rng.choice(types))
            args: dict[str, Any] = {
                "class_token": token,
                "from": pair[0],
                "to": pair[1],
                "change_types": [change_type.value],
                "formulary_id": formulary,
                "with_tiers": True,
            }
            slots |= {"class": token, "formulary": formulary, **self.change_slots(change_type)}
            return self.candidate("multi_step", t, slots, "class_changes", args)
        if t.kind == "tier_up_with_pa":
            formulary = self.rng.choice(self.f.formularies[pair[1]])
            args = {
                "from": pair[0],
                "to": pair[1],
                "change_types": ["tier_up"],
                "formulary_id": formulary,
                "coverage": {"prior_auth": True},
            }
            slots["formulary"] = formulary
            return self.candidate("multi_step", t, slots, "changes_where_coverage", args)
        if t.kind == "removed_still_covered":
            formulary, other = self.rng.sample(self.f.formularies[pair[1]], 2)
            args = {
                "from": pair[0],
                "to": pair[1],
                "change_types": ["drug_removed"],
                "formulary_id": formulary,
                "coverage_formulary_id": other,
                "coverage": {"covered": True},
            }
            slots |= {"formulary": formulary, "other": other}
            return self.candidate("multi_step", t, slots, "changes_where_coverage", args)
        row = self.rng.choice(rows)
        plans = [
            p
            for p in self.f.plans_on(pair[1], row.formulary_to)
            if self.f.names_one_plan(pair[1], p.name)
        ]
        if not plans:
            return None
        plan = self.rng.choice(plans)
        args = {
            "from": pair[0],
            "to": pair[1],
            "change_types": [row.change_type.value],
            "plan_id": plan.plan_id,
            "plan_query": plan.name,
        }
        slots |= {"plan_name": plan.name, **self.change_slots(row.change_type)}
        return self.candidate("multi_step", t, slots, "filtered_list", args)

    def _unloaded_month(self) -> str:
        first, last = self.f.loaded[0], self.f.loaded[-1]
        options = [_shift(first, -1), _shift(first, -6), _shift(last, 1), _shift(last, 3)]
        return self.rng.choice(options)

    def no_answer(self, _t: Template) -> Candidate | None:
        weights = self.settings.no_answer_mix
        kinds = list(weights)
        kind = self.rng.choices(kinds, weights=[weights[k] for k in kinds])[0]
        t = self.rng.choice([x for x in TEMPLATES["no_answer"] if x.kind == kind])
        pair = self.rng.choice(self.f.pairs)
        start, end = self.pair_text(pair)
        month = self.rng.choice(self.f.months)
        drug: str | None
        if kind == "unknown_drug":
            if not self.f.made_up:
                return None
            drug = self.rng.choice(self.f.made_up)
            formulary = self.rng.choice(self.f.formularies[month])
            slots = {
                "drug": drug,
                "month": self.month_text(month),
                "formulary": formulary,
                "from": start,
                "to": end,
            }
            args: dict[str, Any] = {"drug": drug}
            if t.scope == "formulary":
                args |= {"month": month, "formulary_id": formulary}
            else:
                args |= {"from": pair[0], "to": pair[1]}
            return self.candidate(
                "no_answer", t, slots, "unknown_drug", args, tags=["unknown_drug"]
            )
        if kind == "month_not_loaded":
            formulary = self.rng.choice(self.f.formularies[month])
            rxcui = self.rng.choice(self.f.coverage[(month, formulary)])
            drug = self.mention(rxcui)
            if drug is None:
                return None
            missing = self._unloaded_month()
            if t.scope == "formulary":
                slots = {"drug": drug, "formulary": formulary, "month": self.month_text(missing)}
                args = {
                    "drug": drug,
                    "rxcui": rxcui,
                    "month": missing,
                    "formulary_id": formulary,
                    "months": [missing],
                }
            else:
                real = self.rng.choice(self.f.months)
                ordered = sorted([real, missing])
                slots = {
                    "drug": drug,
                    "from": self.month_text(ordered[0]),
                    "to": self.month_text(ordered[1]),
                }
                args = {
                    "drug": drug,
                    "rxcuis": [rxcui],
                    "from": ordered[0],
                    "to": ordered[1],
                    "months": [missing],
                }
            return self.candidate(
                "no_answer", t, slots, "month_not_loaded", args, tags=["month_not_loaded"]
            )
        if kind == "no_matching_changes":
            changed = sorted({r.rxcui for r in self.f.changes[pair]})
            formulary = self.rng.choice(self.f.formularies[pair[1]])
            pool = [r for r in self.f.coverage.get((pair[0], formulary), []) if r in changed]
            if not pool:
                return None
            rxcui = self.rng.choice(pool)
            drug = self.mention(rxcui)
            if drug is None:
                return None
            change_type = self.rng.choice(list(ChangeType))
            args = {
                "drug": drug,
                "rxcuis": [rxcui],
                "from": pair[0],
                "to": pair[1],
                "change_types": [change_type.value],
            }
            if t.scope == "formulary":
                args["formulary_id"] = formulary
            slots = {
                "drug": drug,
                "formulary": formulary,
                "from": start,
                "to": end,
                **self.change_slots(change_type),
            }
            return self.candidate(
                "no_answer", t, slots, "drug_change", args, tags=["no_matching_changes"]
            )
        formulary = self.rng.choice(self.f.formularies[month])
        on = set(self.f.coverage[(month, formulary)])
        pool = sorted(r for r in self.f.mentions if r not in on)
        if not pool:
            return None
        rxcui = self.rng.choice(pool)
        drug = self.mention(rxcui, allow_id=False)
        if drug is None:
            return None
        args = {"drug": drug, "rxcui": rxcui, "month": month}
        slots = {"drug": drug, "month": self.month_text(month), "formulary": formulary}
        if t.scope == "plan":
            plans = self.f.plans_on(month, formulary)
            if not plans:
                return None
            plan = self.rng.choice(plans)
            args["plan_id"] = plan.plan_id
            slots["plan"] = plan.plan_id
        else:
            args["formulary_id"] = formulary
        return self.candidate("no_answer", t, slots, "coverage", args, tags=["not_on_formulary"])

    def ambiguous(self, t: Template) -> Candidate | None:
        if not self.f.ambiguous:
            return None
        name, rxcuis = self.rng.choice(self.f.ambiguous)
        pair = self.rng.choice(self.f.pairs)
        start, end = self.pair_text(pair)
        args: dict[str, Any] = {"drug": name, "rxcuis": rxcuis, "from": pair[0], "to": pair[1]}
        formulary = self.rng.choice(self.f.formularies[pair[1]])
        if t.scope == "formulary":
            args["formulary_id"] = formulary
        slots = {"drug": name, "from": start, "to": end, "formulary": formulary}
        return self.candidate("ambiguous", t, slots, "clarify", args)

    def out_of_scope(self, t: Template) -> Candidate | None:
        pool = sorted(self.f.mentions)
        first, second = self.rng.sample(pool, 2)
        drug, drug2 = self.f.mentions[first][0], self.f.mentions[second][0]
        if normalize_name(drug) == normalize_name(drug2):
            return None
        args = {"drug": drug, "reason": "medical_advice"}
        if "{drug2}" in t.text:
            args["drug2"] = drug2
        return self.candidate("out_of_scope", t, {"drug": drug, "drug2": drug2}, "refusal", args)

    def typo(self, base: Category) -> tuple[Candidate | None, Candidate | None]:
        """A misspelled version of a base item, or an unresolved spill to no_answer."""
        template = self.rng.choice([t for t in TEMPLATES[base] if t.kind != "defaults"])
        make: dict[str, Callable[..., Candidate | None]] = {
            "coverage_lookup": self.coverage_lookup,
            "drug_change": self.drug_change,
            "cost": self.cost,
        }
        base_item = make[base](template, allow_id=False)
        if base_item is None:
            return None, None
        mention = str(base_item.args["drug"])
        word = mention.split()[0]
        if not word.isalpha() or len(word) < 5:
            return None, None
        intended = self.f.resolves(mention)
        for _ in range(self.settings.typo_attempts):
            wrong = _misspell(word, self.rng)
            if normalize_name(wrong) == normalize_name(word):
                continue
            typo_mention = mention.replace(word, wrong, 1)
            question = base_item.question.replace(
                word if base_item.register == "formal" else word.lower(),
                wrong if base_item.register == "formal" else wrong.lower(),
                1,
            )
            args = {**base_item.args, "drug": typo_mention}
            found = self.f.resolves(typo_mention)
            if found == intended:
                return (
                    Candidate(
                        "typo",
                        f"typo.{base_item.template_id}",
                        base_item.register,
                        question,
                        base_item.query,
                        args,
                        base_item.context,
                        ["typo", f"base:{base}"],
                    ),
                    None,
                )
            if not found:
                spill = Candidate(
                    "no_answer",
                    f"no_answer.typo_unresolved.{base_item.template_id}",
                    base_item.register,
                    question,
                    "unknown_drug",
                    {"drug": typo_mention},
                    base_item.context,
                    ["unknown_drug", "typo_unresolved"],
                )
                return None, spill
        return None, None


def _shift(month: str, delta: int) -> str:
    year, number = (int(p) for p in month.split("-"))
    index = year * 12 + number - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _neighbours(letter: str) -> str:
    for row in KEY_ROWS:
        index = row.find(letter)
        if index >= 0:
            return row[max(index - 1, 0) : index] + row[index + 1 : index + 2]
    return ""


def _misspell(word: str, rng: random.Random) -> str:
    """One edit: swap, drop, double or a nearby key, never on the first letter."""
    position = rng.randrange(1, len(word))
    operation = rng.choice(("swap", "drop", "double", "nearby"))
    if operation == "swap" and position < len(word) - 1:
        return word[:position] + word[position + 1] + word[position] + word[position + 2 :]
    if operation == "drop":
        return word[:position] + word[position + 1 :]
    if operation == "double":
        return word[: position + 1] + word[position] + word[position + 1 :]
    near = _neighbours(word[position].lower())
    if not near:
        return word[:position] + word[position + 1 :]
    replacement = rng.choice(near)
    if word[position].isupper():
        replacement = replacement.upper()
    return word[:position] + replacement + word[position + 1 :]


# Expected answers, tools and difficulty


def required_tools(query: str, args: dict[str, Any]) -> list[str]:
    drug = str(args.get("drug", ""))
    by_id = drug.upper().startswith("RXCUI")
    tools: list[str] = []
    if {args.get("from"), args.get("to")} & {"latest", "previous"} or query == "month_not_loaded":
        tools.append("list_months")
    if (
        query
        in ("coverage", "drug_change", "cost_change", "class_changes", "clarify", "unknown_drug")
        and not by_id
    ):
        tools.append("find_drug")
    if args.get("plan_query") or (query == "drug_change" and args.get("plan_id")):
        tools.append("find_plan")
    tools += {
        "coverage": ["get_coverage"],
        "drug_change": ["get_changes"],
        "filtered_list": ["get_changes"],
        "count_drugs": ["count_changes"],
        "cost_change": ["estimate_cost_change"],
        "class_changes": ["get_changes"] + (["get_coverage"] if args.get("with_tiers") else []),
        "changes_where_coverage": ["get_changes", "get_coverage"],
        "changes_intersection": ["get_changes"],
    }.get(query, [])
    return list(dict.fromkeys(tools))


def expected_size(answer: StructuredAnswer) -> int:
    if isinstance(answer, DrugListAnswer):
        return len(answer.rxcuis)
    if isinstance(answer, ChangeAnswer):
        return len(answer.items)
    if isinstance(answer, ClarifyAnswer):
        return len(answer.rxcuis())
    return 1


def difficulty(
    config: Config, tools: list[str], answer: StructuredAnswer, typo: bool
) -> Difficulty:
    rules = config.evalset.difficulty
    size = expected_size(answer)
    level = 0
    if len(tools) >= rules.medium_min_tools or size >= rules.medium_min_list:
        level = 1
    if len(tools) >= rules.hard_min_tools or size >= rules.hard_min_list:
        level = 2
    if typo and rules.typo_lifts:
        level = min(level + 1, 2)
    levels: tuple[Difficulty, Difficulty, Difficulty] = ("easy", "medium", "hard")
    return levels[level]


def _wanted(category: Category, answer: Any, tags: list[str], max_list: int) -> bool:
    """Whether a generated candidate's gold answer is the kind its category needs."""
    if category == "ambiguous":
        clarify, alternative = answer
        signatures = {
            frozenset(
                (i.formulary_id, i.change_type, i.before, i.after)
                for i in alternative.items
                if i.rxcui == r
            )
            for r in clarify.rxcuis()
        }
        return bool(alternative.items) and len(signatures) > 1
    if category == "no_answer":
        return isinstance(answer, NoDataAnswer) and answer.reason.value in tags
    if category == "out_of_scope":
        return bool(answer.kind == "refusal")
    if category in ("filtered_list", "multi_step"):
        return isinstance(answer, DrugListAnswer) and len(answer.rxcuis) <= max_list
    return not isinstance(answer, NoDataAnswer)


def _quotas(config: Config, n: int) -> dict[Category, int]:
    """Largest remainder rounding, so the quotas sum to exactly n."""
    raw = {c: config.evalset.mix[c] * n for c in CATEGORIES}
    quotas = {c: int(v) for c, v in raw.items()}
    leftover = n - sum(quotas.values())
    for c in sorted(CATEGORIES, key=lambda c: (-(raw[c] - quotas[c]), CATEGORIES.index(c))):
        if leftover <= 0:
            break
        quotas[c] += 1
        leftover -= 1
    return quotas


@dataclass
class BuildResult:
    items: list[EvalItem]
    shortfalls: dict[str, int]
    months: list[str]


def _evaluate(
    conn: sqlite3.Connection, config: Config, query: str, args: dict[str, Any]
) -> Any | None:
    try:
        return gold.evaluate(conn, config, query, args)
    except gold.GoldError:
        return None


def build(
    conn: sqlite3.Connection,
    config: Config,
    *,
    month_from: str,
    month_to: str,
    n: int,
    seed: int,
    handwritten: list[HandwrittenItem],
) -> BuildResult:
    facts = Facts(conn, config, month_from, month_to)
    rng = random.Random(seed)
    gen = Generator(facts, rng)
    quotas = _quotas(config, n)
    accepted: dict[Category, list[tuple[Candidate, Any]]] = {c: [] for c in CATEGORIES}
    seen: set[str] = set()
    shortfalls: dict[str, int] = {}

    asked: set[str] = set()

    def accept(cand: Candidate | None) -> bool:
        if (
            cand is None
            or cand.key in seen
            or cand.question in asked
            or len(accepted[cand.category]) >= quotas[cand.category]
        ):
            return False
        answer = _evaluate(conn, config, cand.query, cand.args)
        if answer is None or not _wanted(
            cand.category, answer, cand.tags, config.evalset.max_list_items
        ):
            return False
        seen.add(cand.key)
        asked.add(cand.question)
        accepted[cand.category].append((cand, answer))
        return True

    spills: list[Candidate] = []
    attempts = config.evalset.attempts_per_item
    for attempt in range(quotas["typo"] * attempts):
        if len(accepted["typo"]) >= quotas["typo"]:
            break
        typo_item, spill = gen.typo(TYPO_BASES[attempt % len(TYPO_BASES)])
        accept(typo_item)
        if spill is not None:
            spills.append(spill)
    # Unresolved misspellings fill only part of the no answer quota.
    for spill in spills[: int(quotas["no_answer"] * config.evalset.typo_spill_share)]:
        accept(spill)

    makers: dict[Category, Callable[[Template], Candidate | None]] = {
        "coverage_lookup": gen.coverage_lookup,
        "drug_change": gen.drug_change,
        "filtered_list": gen.filtered_list,
        "count": gen.count,
        "cost": gen.cost,
        "multi_step": gen.multi_step,
        "no_answer": gen.no_answer,
        "ambiguous": gen.ambiguous,
        "out_of_scope": gen.out_of_scope,
    }
    for category, make in makers.items():
        templates = TEMPLATES[category]
        for attempt in range(quotas[category] * attempts):
            if len(accepted[category]) >= quotas[category]:
                break
            accept(make(templates[attempt % len(templates)]))

    for category in CATEGORIES:
        missing = quotas[category] - len(accepted[category])
        if missing:
            shortfalls[category] = missing

    items: list[EvalItem] = []
    for category in CATEGORIES:
        for cand, answer in accepted[category]:
            items.append(_item(config, f"{SET_PREFIX}-{len(items) + 1:04d}", cand, answer, seed))
    for index, hand in enumerate(handwritten, start=1):
        answer = gold.evaluate(conn, config, hand.gold.query, hand.gold.args)
        cand = Candidate(
            hand.category,
            f"handwritten.{hand.id}",
            "formal",
            hand.question,
            hand.gold.query,
            dict(hand.gold.args),
            hand.context,
            list(hand.tags),
        )
        item = _item(config, f"{SET_PREFIX}-h{index:02d}", cand, answer, seed)
        items.append(item.model_copy(update={"source": "handwritten"}))
    return BuildResult(
        items=_split(config, items, seed), shortfalls=shortfalls, months=facts.months
    )


def _item(config: Config, item_id: str, cand: Candidate, answer: Any, seed: int) -> EvalItem:
    alternative = None
    if isinstance(answer, tuple):
        answer, alternative = answer
    tools = required_tools(cand.query, cand.args)
    return EvalItem(
        id=item_id,
        category=cand.category,
        template_id=cand.template_id,
        question=cand.question,
        context=cand.context,
        expected=answer,
        alternative=alternative,
        required_tools=tools,
        gold_query=gold.describe(cand.query, cand.args),
        gold=GoldSpec(query=cand.query, args=cand.args),
        difficulty=difficulty(config, tools, answer, "typo" in cand.tags),
        split="dev",
        source="generated",
        seed=seed,
        tags=cand.tags,
    )


def _split(config: Config, items: list[EvalItem], seed: int) -> list[EvalItem]:
    """Stratified by category: the dev share of each category, the rest test."""
    out: dict[str, EvalItem] = {}
    for category in CATEGORIES:
        members = sorted((i for i in items if i.category == category), key=lambda i: i.id)
        order = list(range(len(members)))
        random.Random(f"{seed}:{category}").shuffle(order)
        n_dev = round(config.evalset.splits.dev * len(members))
        for rank, index in enumerate(order):
            split = "dev" if rank < n_dev else "test"
            out[members[index].id] = members[index].model_copy(update={"split": split})
    return [out[i.id] for i in items]


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = ("formulary", "plan_info", "beneficiary_cost", "drug_names", "ingest_log")
    return {
        t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # noqa: S608
        for t in tables
    }


def write_set(
    result: BuildResult,
    out: Path,
    *,
    conn: sqlite3.Connection,
    config: Config,
    seed: int,
    n: int,
    month_range: tuple[str, str],
) -> Path:
    write_items(out, result.items)
    counts: dict[str, dict[str, int]] = {}
    for item in result.items:
        entry = counts.setdefault(item.category, {"dev": 0, "test": 0, "total": 0})
        entry[item.split] += 1
        entry["total"] += 1
    manifest = {
        "eval_set": out.stem,
        "file": out.name,
        "file_sha256": sha256_file(out),
        "builder_version": config.evalset.builder_version,
        "seed": seed,
        "n_generated": n,
        "n_handwritten": sum(1 for i in result.items if i.source == "handwritten"),
        "items": len(result.items),
        "months": result.months,
        "month_range": list(month_range),
        "counts": {c: counts.get(c, {"dev": 0, "test": 0, "total": 0}) for c in CATEGORIES},
        "splits": {s: sum(1 for i in result.items if i.split == s) for s in ("dev", "test")},
        "shortfalls": result.shortfalls,
        "db_row_counts": table_counts(conn),
        "config_sha256": config.sha256,
    }
    path = manifest_path(out)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path


def open_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise BuildError(f"Database not found: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_file(
    db_path: Path,
    config: Config,
    *,
    month_from: str,
    month_to: str,
    n: int,
    seed: int,
    out: Path,
    handwritten_path: Path | None,
) -> tuple[BuildResult, Path]:
    conn = open_db(db_path)
    try:
        hand = read_handwritten(handwritten_path) if handwritten_path else []
        result = build(
            conn,
            config,
            month_from=month_from,
            month_to=month_to,
            n=n,
            seed=seed,
            handwritten=hand,
        )
        manifest = write_set(
            result,
            out,
            conn=conn,
            config=config,
            seed=seed,
            n=n,
            month_range=(month_from, month_to),
        )
    finally:
        conn.close()
    return result, manifest
