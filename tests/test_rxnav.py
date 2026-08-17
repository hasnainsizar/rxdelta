from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest

from rxdelta.config import Config, NamesConfig
from rxdelta.names import rxnav
from rxdelta.names.cache import DrugName


@pytest.fixture
def names_config(config: Config) -> NamesConfig:
    # Fast enough that the rate limiter does not slow the suite down.
    return replace(config.names, requests_per_second=1000.0, max_retries=0)


def stub_fetcher(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, str | None], *, fail: set[str] | None = None
) -> list[str]:
    """Replace the network call and record which RXCUIs were actually asked for."""
    asked: list[str] = []

    def fake(_config: Any, _limiter: Any, rxcui: str) -> tuple[str, str | None, bool]:
        asked.append(rxcui)
        if fail and rxcui in fail:
            return rxcui, None, True
        return rxcui, answers.get(rxcui), False

    monkeypatch.setattr(rxnav, "_fetch_one", fake)
    return asked


def test_repeat_runs_only_fetch_uncached_rxcuis(
    names_config: NamesConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = stub_fetcher(monkeypatch, {"1": "One", "2": "Two", "3": "Three"})
    cache, first = rxnav.refresh(names_config, ["1", "2", "3"], {})
    assert sorted(asked) == ["1", "2", "3"]
    assert first.resolved == 3
    assert first.already_cached == 0

    asked.clear()
    cache, second = rxnav.refresh(names_config, ["1", "2", "3"], cache)
    assert asked == []
    assert second.already_cached == 3
    assert second.fetched == 0


def test_a_new_rxcui_is_the_only_thing_fetched_on_a_later_run(
    names_config: NamesConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = {"1": DrugName("1", "One", "rxnav", "t")}
    asked = stub_fetcher(monkeypatch, {"2": "Two"})
    cache, outcome = rxnav.refresh(names_config, ["1", "2"], cache)
    assert asked == ["2"]
    assert outcome.resolved == 1
    assert outcome.already_cached == 1
    assert cache["1"].name == "One"


def test_force_refetches_everything(
    names_config: NamesConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = {"1": DrugName("1", "Stale", "rxnav", "t")}
    asked = stub_fetcher(monkeypatch, {"1": "Fresh", "2": "Two"})
    cache, outcome = rxnav.refresh(names_config, ["1", "2"], cache, force=True)
    assert sorted(asked) == ["1", "2"]
    assert cache["1"].name == "Fresh"
    assert outcome.already_cached == 0


def test_an_rxcui_rxnav_cannot_resolve_is_cached_so_it_is_not_asked_again(
    names_config: NamesConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_fetcher(monkeypatch, {"1": None})
    cache, outcome = rxnav.refresh(names_config, ["1"], {})
    assert outcome.not_found == 1
    assert cache["1"].name == ""
    assert cache["1"].source == rxnav.SOURCE_NOT_FOUND

    second_pass = stub_fetcher(monkeypatch, {"1": None})
    _, second = rxnav.refresh(names_config, ["1"], cache)
    assert second_pass == []
    assert second.already_cached == 1


def test_a_hard_failure_is_not_cached_so_a_rerun_retries_it(
    names_config: NamesConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_fetcher(monkeypatch, {"1": "One"}, fail={"1"})
    cache, outcome = rxnav.refresh(names_config, ["1"], {})
    assert outcome.failed == 1
    assert "1" not in cache

    retried = stub_fetcher(monkeypatch, {"1": "One"})
    cache, second = rxnav.refresh(names_config, ["1"], cache)
    assert retried == ["1"]
    assert second.resolved == 1


def test_duplicate_rxcuis_are_requested_once(
    names_config: NamesConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = stub_fetcher(monkeypatch, {"7": "Seven"})
    _, outcome = rxnav.refresh(names_config, ["7", "7", "7"], {})
    assert asked == ["7"]
    assert outcome.requested == 1


def test_nothing_to_do_short_circuits(
    names_config: NamesConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = stub_fetcher(monkeypatch, {})
    _, outcome = rxnav.refresh(names_config, [], {})
    assert asked == []
    assert outcome.requested == 0


def test_progress_is_reported_at_the_configured_interval(
    names_config: NamesConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_fetcher(monkeypatch, {str(i): f"Drug {i}" for i in range(10)})
    seen: list[tuple[int, int]] = []
    rxnav.refresh(
        replace(names_config, progress_every=3),
        [str(i) for i in range(10)],
        {},
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert [d for d, _ in seen] == [3, 6, 9]


def test_rate_limiter_spaces_requests_out() -> None:
    limiter = rxnav.RateLimiter(per_second=50.0)
    start = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    # Four intervals of 20ms between five acquisitions, with slack for the clock.
    assert time.monotonic() - start >= 0.06
