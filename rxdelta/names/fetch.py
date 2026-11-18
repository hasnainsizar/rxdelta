"""`rxdelta names fetch`: full, ingredient and brand names from the RxNav API.

Endpoints, checked against the RxNav documentation:

- GET {api_base}/rxcui/{rxcui}/properties.json returns
  {"properties": {"rxcui", "name", "tty", ...}}. An RXCUI outside the active
  data set returns no "properties" element.
- GET {api_base}/rxcui/{rxcui}/related.json?tty=IN+MIN returns
  {"relatedGroup": {"conceptGroup": [{"tty", "conceptProperties": [{"name", ...}]}]}}.

Brands are not taken from the related concepts. For a generic drug RxNav
relates every brand of the same ingredient (generic carvedilol relates to
Coreg), which would make "Coreg" find the generic. RxNorm names a branded
concept with its brand in trailing brackets, so that is the brand used.

NLM asks for no more than 20 requests per second per IP. The rate here comes
from [names].requests_per_second.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from rxdelta.config import NamesConfig
from rxdelta.names.normalize import bracket_brand
from rxdelta.names.rxnav import RateLimiter
from rxdelta.names.store import (
    NameRow,
    formulary_rxcuis,
    log_missing,
    missing_rxcuis,
    really_named_rxcuis,
    upsert,
)

SOURCE = "rxnav"
_USER_AGENT = "rxdelta/0.1 (+https://rxnav.nlm.nih.gov)"
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_INGREDIENT_TTYS = ("IN", "MIN")


class FetchError(Exception):
    """A request that still failed after every retry."""


@dataclass(frozen=True)
class ConceptNames:
    rxcui: str
    full: str
    ingredients: tuple[str, ...]
    brands: tuple[str, ...]


@dataclass
class FetchSummary:
    in_scope: int = 0
    already_present: int = 0
    fetched: int = 0
    not_found: int = 0
    failed: int = 0
    failed_rxcuis: list[str] = field(default_factory=list)


class RxNavClient:
    """Thin JSON client. Pass an httpx.Client to control the transport in tests."""

    def __init__(
        self,
        config: NamesConfig,
        http: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._http = http or httpx.Client(
            timeout=config.request_timeout_seconds, headers={"User-Agent": _USER_AGENT}
        )
        self._limiter = RateLimiter(config.requests_per_second)
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any] | None:
        """JSON body, or None on 404. Raises FetchError once retries run out."""
        url = f"{self._config.api_base}{path}"
        delay = 1.0
        last_error = ""
        for attempt in range(self._config.max_retries + 1):
            self._limiter.acquire()
            try:
                response = self._http.get(url, params=params)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 404:
                    return None
                if response.status_code not in _RETRY_STATUS:
                    if response.status_code >= 400:
                        raise FetchError(f"HTTP {response.status_code} for {url}")
                    try:
                        body: Any = response.json()
                    except ValueError as exc:
                        raise FetchError(f"invalid JSON from {url}") from exc
                    return body if isinstance(body, dict) else {}
                last_error = f"HTTP {response.status_code}"
            if attempt < self._config.max_retries:
                self._sleep(delay)
                delay *= 2
        raise FetchError(f"{last_error} for {url} after {self._config.max_retries + 1} tries")

    def concept(self, rxcui: str) -> ConceptNames | None:
        """Names for one RXCUI, or None when RxNav does not know it."""
        props_body = self._get(f"/rxcui/{rxcui}/properties.json")
        properties = (props_body or {}).get("properties") or {}
        full = str(properties.get("name") or "").strip()
        if not full:
            return None
        related = self._get(f"/rxcui/{rxcui}/related.json", {"tty": " ".join(_INGREDIENT_TTYS)})
        groups = ((related or {}).get("relatedGroup") or {}).get("conceptGroup") or []
        ingredients = {
            str(concept.get("name") or "").strip()
            for group in groups
            if str(group.get("tty") or "") in _INGREDIENT_TTYS
            for concept in group.get("conceptProperties") or []
        } - {""}
        brand = bracket_brand(full)
        return ConceptNames(rxcui, full, tuple(sorted(ingredients)), (brand,) if brand else ())


def concept_rows(names: ConceptNames, fetched_at: str) -> list[NameRow]:
    rows = [NameRow(names.rxcui, names.full, "full", SOURCE, fetched_at)]
    rows += [NameRow(names.rxcui, n, "ingredient", SOURCE, fetched_at) for n in names.ingredients]
    rows += [NameRow(names.rxcui, n, "brand", SOURCE, fetched_at) for n in names.brands]
    return rows


def fetch(
    conn: sqlite3.Connection,
    client: RxNavClient,
    *,
    progress_every: int = 0,
    on_progress: Callable[[FetchSummary, int], None] | None = None,
) -> FetchSummary:
    """Fetch every formulary RXCUI with no real name and no logged miss.

    A placeholder name, such as one from the synthetic demo cache, does not
    count as named: on real data its RXCUI can belong to a different drug.

    Each RXCUI is written as soon as it resolves, so stopping part way loses
    nothing and a rerun picks up where the last one stopped.
    """
    wanted = formulary_rxcuis(conn)
    done = really_named_rxcuis(conn) | missing_rxcuis(conn)
    todo = [r for r in wanted if r not in done]
    summary = FetchSummary(in_scope=len(wanted), already_present=len(wanted) - len(todo))
    for index, rxcui in enumerate(todo, start=1):
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            names = client.concept(rxcui)
        except FetchError:
            summary.failed += 1
            summary.failed_rxcuis.append(rxcui)
        else:
            if names is None:
                log_missing(
                    conn, rxcui, SOURCE, "RxNav has no active concept for this RXCUI", stamp
                )
                summary.not_found += 1
            else:
                upsert(conn, concept_rows(names, stamp))
                summary.fetched += 1
        if on_progress and progress_every and index % progress_every == 0:
            on_progress(summary, len(todo))
    return summary
