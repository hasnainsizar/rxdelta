"""NLM RxNav client. The only code in the project that touches the network.

RxNav publishes no bulk RXCUI to name endpoint, so a refresh issues one request
per unresolved RXCUI. Requests go out in bounded windows under a token bucket,
which is what "batching" can mean against a per-concept API.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rxdelta.config import NamesConfig
from rxdelta.names.cache import DrugName

SOURCE = "rxnav"
SOURCE_NOT_FOUND = "rxnav:not-found"
_USER_AGENT = "rxdelta/0.1 (+https://rxnav.nlm.nih.gov)"


class RateLimiter:
    """Token bucket. Threads block here rather than sleeping between batches,
    so the configured rate holds regardless of how many workers are running."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if wait:
            time.sleep(wait)


@dataclass(frozen=True)
class RefreshResult:
    requested: int
    resolved: int
    not_found: int
    failed: int
    already_cached: int

    @property
    def fetched(self) -> int:
        return self.resolved + self.not_found + self.failed


def _fetch_one(
    config: NamesConfig, limiter: RateLimiter, rxcui: str
) -> tuple[str, str | None, bool]:
    """Return (rxcui, name or None, hard_failure). A 404 is not a failure: it
    means RxNav has no concept for that id, which is worth caching."""
    url = f"{config.api_base}/rxcui/{rxcui}/properties.json"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    delay = 1.0
    for attempt in range(config.max_retries + 1):
        limiter.acquire()
        try:
            with urllib.request.urlopen(  # noqa: S310  (fixed https base from config)
                request, timeout=config.request_timeout_seconds
            ) as response:
                payload: Any = json.loads(response.read().decode("utf-8"))
            properties = (payload or {}).get("properties") or {}
            name = str(properties.get("name") or "").strip()
            return rxcui, (name or None), False
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return rxcui, None, False
            if exc.code in (429, 500, 502, 503, 504) and attempt < config.max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            return rxcui, None, True
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            if attempt < config.max_retries:
                time.sleep(delay)
                delay *= 2
                continue
            return rxcui, None, True
    return rxcui, None, True


def refresh(
    config: NamesConfig,
    rxcuis: list[str],
    cached: dict[str, DrugName],
    *,
    force: bool = False,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, DrugName], RefreshResult]:
    """Resolve every RXCUI not already cached and fold the results in.

    `cached` is mutated into the returned mapping, so callers write the whole
    thing back and keep prior work even if this run resolves nothing.
    """
    wanted = sorted(set(rxcuis), key=lambda r: (0, int(r)) if r.isdigit() else (1, r))
    todo = wanted if force else [r for r in wanted if r not in cached]
    already = len(wanted) - len(todo)
    if not todo:
        return cached, RefreshResult(len(wanted), 0, 0, 0, already)

    limiter = RateLimiter(config.requests_per_second)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    resolved = not_found = failed = 0
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rxcui, name, hard_failure in pool.map(lambda r: _fetch_one(config, limiter, r), todo):
            if hard_failure:
                failed += 1
            elif name:
                cached[rxcui] = DrugName(rxcui, name, SOURCE, stamp)
                resolved += 1
            else:
                cached[rxcui] = DrugName(rxcui, "", SOURCE_NOT_FOUND, stamp)
                not_found += 1
            done += 1
            if on_progress and config.progress_every and done % config.progress_every == 0:
                on_progress(done, len(todo))

    return cached, RefreshResult(len(wanted), resolved, not_found, failed, already)
