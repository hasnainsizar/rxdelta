"""Find drugs by RXCUI, NDC, name, partial name or misspelled name.

Stages run in order and the first stage that finds anything wins: an exact
identifier, an exact normalized name, every query token present in a name, then
a fuzzy score above the configured threshold.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel
from rapidfuzz import fuzz

from rxdelta.config import MatchConfig
from rxdelta.ingest.normalize import normalize_ndc
from rxdelta.names.normalize import normalize_name

MatchType = Literal["id", "exact", "token", "fuzzy"]

_ID_PREFIX = re.compile(r"^\s*(rxcui|ndc)\s*[:#]?\s*", re.IGNORECASE)
_RXCUI = re.compile(r"^\d{1,8}$")
_NDC = re.compile(r"^(\d{4,5}-\d{3,4}-\d{1,2}|\d{10,11})$")


class DrugMatch(BaseModel):
    rxcui: str
    display_name: str
    matched_on: str
    match_type: MatchType
    score: float


class DrugProduct(BaseModel):
    """One product, such as a brand, and every RXCUI (strength or form) under it."""

    product: str
    rxcuis: list[str]
    match_type: MatchType
    score: float
    matches: list[DrugMatch]


@dataclass(frozen=True)
class _Name:
    rxcui: str
    name: str
    name_type: str
    norm: str
    tokens: frozenset[str]


class DrugMatcher:
    """Holds every name in memory; build one per database connection."""

    def __init__(self, conn: sqlite3.Connection, config: MatchConfig) -> None:
        self._conn = conn
        self._config = config
        rows = conn.execute(
            "SELECT rxcui, name, name_type, name_norm FROM drug_names "
            "ORDER BY rxcui, name_type, name_norm"
        ).fetchall()
        self._names = [
            _Name(str(r[0]), str(r[1]), str(r[2]), str(r[3]), frozenset(str(r[3]).split()))
            for r in rows
        ]
        self._by_rxcui: dict[str, list[_Name]] = defaultdict(list)
        self._by_norm: dict[str, list[_Name]] = defaultdict(list)
        self._by_token: dict[str, set[int]] = defaultdict(set)
        for index, entry in enumerate(self._names):
            self._by_rxcui[entry.rxcui].append(entry)
            self._by_norm[entry.norm].append(entry)
            for token in entry.tokens:
                self._by_token[token].add(index)

    def display_name(self, rxcui: str) -> str:
        for entry in self._by_rxcui.get(rxcui, []):
            if entry.name_type == "full":
                return entry.name
        return f"RXCUI {rxcui}"

    def product_label(self, rxcui: str) -> str:
        names = self._by_rxcui.get(rxcui, [])
        for kind in ("brand", "ingredient"):
            found = sorted({n.name for n in names if n.name_type == kind})
            if found:
                return " / ".join(found)
        return self.display_name(rxcui)

    def match_all(self, query: str, *, fuzzy: bool = True) -> list[DrugMatch]:
        """Every match from the first stage that finds any, best first.

        `fuzzy=False` stops after the token stage, for callers that need a name
        to resolve exactly rather than by resemblance.
        """
        stages = [self._by_id, self._exact, self._token] + ([self._fuzzy] if fuzzy else [])
        for stage in stages:
            found = stage(query)
            if found:
                return _sorted(found)
        return []

    def match_drug(self, query: str, limit: int) -> list[DrugMatch]:
        return self.match_all(query)[: max(limit, 0)]

    def near_misses(self, query: str, limit: int | None = None) -> list[DrugMatch]:
        """Fuzzy candidates scoring between the near miss floor and the threshold."""
        norm = normalize_name(query)
        if not norm:
            return []
        best: dict[str, DrugMatch] = {}
        for entry in self._names:
            score = _fuzzy_score(norm, entry)
            if self._config.near_miss_floor <= score < self._config.fuzzy_threshold:
                _keep_best(best, self._match(entry, "fuzzy", score))
        cap = self._config.near_miss_limit if limit is None else limit
        return _sorted(list(best.values()))[:cap]

    def group(self, matches: list[DrugMatch]) -> list[DrugProduct]:
        buckets: dict[str, list[DrugMatch]] = defaultdict(list)
        for match in matches:
            buckets[self.product_label(match.rxcui)].append(match)
        products = [
            DrugProduct(
                product=label,
                rxcuis=sorted({m.rxcui for m in members}),
                match_type=members[0].match_type,
                score=max(m.score for m in members),
                matches=members,
            )
            for label, members in buckets.items()
        ]
        return sorted(products, key=lambda p: (-p.score, p.product.lower()))

    def _match(self, entry: _Name, match_type: MatchType, score: float) -> DrugMatch:
        return DrugMatch(
            rxcui=entry.rxcui,
            display_name=self.display_name(entry.rxcui),
            matched_on=entry.name,
            match_type=match_type,
            score=round(score, 1),
        )

    def _by_id(self, query: str) -> list[DrugMatch]:
        text = _ID_PREFIX.sub("", query.strip())
        found: dict[str, DrugMatch] = {}
        if _NDC.match(text):
            ndc = normalize_ndc(text)
            if ndc.ndc_11:
                for row in self._conn.execute(
                    "SELECT DISTINCT rxcui FROM formulary WHERE ndc_11 = ? ORDER BY rxcui",
                    (ndc.ndc_11,),
                ).fetchall():
                    rxcui = str(row[0])
                    found[rxcui] = self._id_match(rxcui, ndc.ndc_11)
        if not found and _RXCUI.match(text) and self._rxcui_known(text):
            found[text] = self._id_match(text, text)
        return list(found.values())

    def _id_match(self, rxcui: str, matched_on: str) -> DrugMatch:
        return DrugMatch(
            rxcui=rxcui,
            display_name=self.display_name(rxcui),
            matched_on=matched_on,
            match_type="id",
            score=100.0,
        )

    def _rxcui_known(self, rxcui: str) -> bool:
        if rxcui in self._by_rxcui:
            return True
        row = self._conn.execute(
            "SELECT 1 FROM formulary WHERE rxcui = ? LIMIT 1", (rxcui,)
        ).fetchone()
        return row is not None

    def _exact(self, query: str) -> list[DrugMatch]:
        best: dict[str, DrugMatch] = {}
        for entry in self._by_norm.get(normalize_name(query), []):
            _keep_best(best, self._match(entry, "exact", 100.0))
        return list(best.values())

    def _token(self, query: str) -> list[DrugMatch]:
        norm = normalize_name(query)
        wanted = frozenset(norm.split())
        best: dict[str, DrugMatch] = {}
        if not wanted:
            return []
        hits = set.intersection(*(self._by_token.get(t, set()) for t in wanted))
        for index in sorted(hits):
            entry = self._names[index]
            if wanted <= entry.tokens:
                # Shorter names that still contain every token rank higher.
                score = 100.0 * len(norm) / max(len(entry.norm), len(norm))
                _keep_best(best, self._match(entry, "token", score))
        return list(best.values())

    def _fuzzy(self, query: str) -> list[DrugMatch]:
        norm = normalize_name(query)
        best: dict[str, DrugMatch] = {}
        if not norm:
            return []
        for entry in self._names:
            score = _fuzzy_score(norm, entry)
            if score >= self._config.fuzzy_threshold:
                _keep_best(best, self._match(entry, "fuzzy", score))
        return list(best.values())


def _fuzzy_score(norm: str, entry: _Name) -> float:
    """Whole string similarity, or the weakest per token similarity when every
    query token can be paired with some token of the name."""
    whole = float(fuzz.ratio(norm, entry.norm))
    per_token = min(
        max(float(fuzz.ratio(token, candidate)) for candidate in entry.tokens)
        for token in norm.split()
    )
    return max(whole, per_token)


def _keep_best(best: dict[str, DrugMatch], match: DrugMatch) -> None:
    current = best.get(match.rxcui)
    if current is None or _rank(match) < _rank(current):
        best[match.rxcui] = match


def _rank(match: DrugMatch) -> tuple[float, int, str]:
    return (-match.score, len(match.matched_on), match.matched_on)


def _sorted(matches: list[DrugMatch]) -> list[DrugMatch]:
    return sorted(matches, key=lambda m: (-m.score, m.display_name.lower(), m.rxcui))
