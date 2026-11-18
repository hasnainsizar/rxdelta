from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from rxdelta.cli import app
from rxdelta.config import Config
from rxdelta.db import connect
from rxdelta.names import fetch, fixture, rrf
from rxdelta.names.match import DrugMatcher
from rxdelta.names.normalize import bracket_brand, normalize_name
from rxdelta.names.store import status
from tests.conftest import ASK, ASK_NAMES, FIXTURES, build_ask_db

runner = CliRunner()


def test_normalize_name_lowercases_and_drops_punctuation() -> None:
    assert normalize_name("  Humalog  Mix 75/25 ") == "humalog mix 75 25"
    assert normalize_name("semaglutide 0.25 MG [Ozempic]") == "semaglutide 0.25 mg ozempic"
    assert normalize_name("insulin lispro protamine, human") == "insulin lispro protamine human"
    assert normalize_name("...") == ""


def test_bracket_brand_reads_the_trailing_brand() -> None:
    assert bracket_brand("apixaban 5 MG Oral Tablet [Eliquis]") == "Eliquis"
    assert bracket_brand("lisinopril 10 MG Oral Tablet") is None


def test_an_old_single_name_table_is_migrated_in_place(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE drug_names (rxcui TEXT NOT NULL PRIMARY KEY, name TEXT NOT NULL, "
        "source TEXT NOT NULL, fetched_at TEXT NOT NULL)"
    )
    raw.executemany(
        "INSERT INTO drug_names VALUES (?, ?, ?, ?)",
        [
            ("1", "apixaban 5 MG Oral Tablet [Eliquis]", "rxnav", "t"),
            ("2", "lisinopril 10 MG Oral Tablet", "rxnav", "t"),
            ("3", "", "rxnav:not-found", "t"),
        ],
    )
    raw.commit()
    raw.close()

    conn = connect(db)
    rows = conn.execute(
        "SELECT rxcui, name_type, name, name_norm FROM drug_names ORDER BY rxcui, name_type"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("1", "brand", "Eliquis", "eliquis"),
        ("1", "full", "apixaban 5 MG Oral Tablet [Eliquis]", "apixaban 5 mg oral tablet eliquis"),
        ("2", "full", "lisinopril 10 MG Oral Tablet", "lisinopril 10 mg oral tablet"),
    ]
    conn.close()
    # Running the migration again on the new shape changes nothing.
    again = connect(db)
    assert again.execute("SELECT COUNT(*) FROM drug_names").fetchone()[0] == 3
    again.close()


def _fixture_rxcuis() -> set[str]:
    found: set[str] = set()
    for path in FIXTURES.rglob("basic drugs formulary file.txt"):
        lines = path.read_text(encoding="utf-8").splitlines()
        header = lines[0].split("|")
        if "RXCUI" not in header:
            continue
        index = header.index("RXCUI")
        found |= {line.split("|")[index] for line in lines[1:] if line.strip()}
    return found


def test_fixture_names_cover_every_rxcui_in_every_test_fixture() -> None:
    named = {row.rxcui for row in fixture.read_rows(ASK_NAMES) if row.name_type == "full"}
    missing = _fixture_rxcuis() - named
    assert not missing, sorted(missing)


def test_fixture_load_fills_drug_names_and_status_reports_full_coverage(
    tmp_path: Path, config: Config
) -> None:
    db = build_ask_db(tmp_path / "a.db", config)
    conn = connect(db)
    report = status(conn)
    assert report.formulary_rxcuis == report.named > 40
    assert report.missing == 0
    assert report.by_source == {"fixture": report.named}
    types = {r[0] for r in conn.execute("SELECT DISTINCT name_type FROM drug_names")}
    assert types == {"full", "ingredient", "brand"}


def test_a_bad_name_type_in_the_fixture_csv_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "n.csv"
    path.write_text("rxcui,name_type,name\n1,generic,thing\n", encoding="utf-8")
    with pytest.raises(Exception, match="name_type must be one of"):
        fixture.read_rows(path)


# RxNav client, always against a mocked transport.

_CONCEPTS: dict[str, dict[str, object]] = {
    "8800141": {
        "full": "apixaban 5 MG Oral Tablet [Eliquis]",
        "groups": [
            {"tty": "IN", "conceptProperties": [{"name": "apixaban"}]},
            {"tty": "BN", "conceptProperties": [{"name": "Eliquis"}]},
        ],
    },
    "8800271": {
        "full": "lisinopril 10 MG Oral Tablet",
        "groups": [{"tty": "IN", "conceptProperties": [{"name": "lisinopril"}]}],
    },
}


def _transport(
    calls: list[str], *, flaky: dict[str, int] | None = None, down: set[str] | None = None
) -> httpx.MockTransport:
    flaky = dict(flaky or {})

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        rxcui = request.url.path.split("/")[-2]
        if down and rxcui in down:
            raise httpx.ConnectError("no route to host", request=request)
        if flaky.get(rxcui, 0) > 0:
            flaky[rxcui] -= 1
            return httpx.Response(503)
        concept = _CONCEPTS.get(rxcui)
        if request.url.path.endswith("properties.json"):
            if concept is None:
                return httpx.Response(200, json={})
            return httpx.Response(
                200, json={"properties": {"rxcui": rxcui, "name": concept["full"]}}
            )
        assert request.url.params["tty"] == "IN MIN BN"
        groups = concept["groups"] if concept else []
        return httpx.Response(200, json={"relatedGroup": {"conceptGroup": groups}})

    return httpx.MockTransport(handler)


def _client(config: Config, transport: httpx.MockTransport) -> fetch.RxNavClient:
    from dataclasses import replace

    names = replace(config.names, requests_per_second=1000.0, max_retries=2)
    return fetch.RxNavClient(names, httpx.Client(transport=transport), sleep=lambda _s: None)


def _db_with_rxcuis(tmp_path: Path, rxcuis: list[str]) -> sqlite3.Connection:
    conn = connect(tmp_path / "f.db")
    with conn:
        conn.executemany(
            "INSERT INTO formulary VALUES ('2026-01', 'F1', ?, ?, ?, 1, 0, 0, 0, NULL, NULL)",
            [(f"{i:011d}", f"{i:011d}", r) for i, r in enumerate(rxcuis)],
        )
    return conn


def test_fetch_stores_full_ingredient_and_brand_names(tmp_path: Path, config: Config) -> None:
    conn = _db_with_rxcuis(tmp_path, ["8800141", "8800271"])
    calls: list[str] = []
    summary = fetch.fetch(conn, _client(config, _transport(calls)))
    assert (summary.fetched, summary.not_found, summary.failed) == (2, 0, 0)
    rows = conn.execute(
        "SELECT name_type, name, source FROM drug_names WHERE rxcui = '8800141' ORDER BY name_type"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("brand", "Eliquis", "rxnav"),
        ("full", "apixaban 5 MG Oral Tablet [Eliquis]", "rxnav"),
        ("ingredient", "apixaban", "rxnav"),
    ]
    assert all("rxnav.nlm.nih.gov/REST/rxcui/" in c for c in calls)


def test_fetch_logs_unknown_rxcuis_instead_of_dropping_them(tmp_path: Path, config: Config) -> None:
    conn = _db_with_rxcuis(tmp_path, ["9999999"])
    summary = fetch.fetch(conn, _client(config, _transport([])))
    assert summary.not_found == 1
    logged = conn.execute("SELECT rxcui, source FROM drug_names_missing").fetchall()
    assert [tuple(r) for r in logged] == [("9999999", "rxnav")]


def test_fetch_retries_server_errors_with_backoff(tmp_path: Path, config: Config) -> None:
    conn = _db_with_rxcuis(tmp_path, ["8800271"])
    calls: list[str] = []
    summary = fetch.fetch(conn, _client(config, _transport(calls, flaky={"8800271": 2})))
    assert summary.fetched == 1
    assert len(calls) == 4  # two 503s, then properties and related


def test_fetch_counts_a_failure_and_a_rerun_retries_only_that(
    tmp_path: Path, config: Config
) -> None:
    conn = _db_with_rxcuis(tmp_path, ["8800141", "8800271"])
    first = fetch.fetch(conn, _client(config, _transport([], down={"8800271"})))
    assert (first.fetched, first.failed, first.failed_rxcuis) == (1, 1, ["8800271"])

    calls: list[str] = []
    second = fetch.fetch(conn, _client(config, _transport(calls)))
    assert (second.already_present, second.fetched) == (1, 1)
    assert all("8800271" in c for c in calls)

    third_calls: list[str] = []
    third = fetch.fetch(conn, _client(config, _transport(third_calls)))
    assert third.already_present == 2 and not third_calls


def test_fetch_reports_progress(tmp_path: Path, config: Config) -> None:
    conn = _db_with_rxcuis(tmp_path, ["8800141", "8800271", "9999999"])
    seen: list[int] = []
    fetch.fetch(
        conn,
        _client(config, _transport([])),
        progress_every=1,
        on_progress=lambda s, _total: seen.append(s.fetched + s.not_found + s.failed),
    )
    assert seen == [1, 2, 3]


def test_rrf_loader_names_formulary_rxcuis_and_logs_the_rest(
    tmp_path: Path, config: Config
) -> None:
    conn = _db_with_rxcuis(tmp_path, ["8800141", "8800271", "5550000"])
    line = "{rxcui}|ENG|P|L1|PF|S1|Y|A1||||{sab}|{tty}|1|{name}||{supp}|4096|\n"
    path = tmp_path / "RXNCONSO.RRF"
    path.write_text(
        line.format(
            rxcui="8800141",
            sab="RXNORM",
            tty="SBD",
            name="apixaban 5 MG Oral Tablet [Eliquis]",
            supp="N",
        )
        + line.format(rxcui="8800141", sab="RXNORM", tty="SY", name="Eliquis 5mg", supp="N")
        + line.format(rxcui="8800271", sab="MTHSPL", tty="DP", name="LISINOPRIL", supp="N")
        + line.format(
            rxcui="8800271", sab="RXNORM", tty="SCD", name="lisinopril 10 MG Oral Tablet", supp="N"
        )
        + line.format(rxcui="5550000", sab="RXNORM", tty="SCD", name="old name", supp="O"),
        encoding="utf-8",
    )
    outcome = rrf.load(conn, path)
    assert (outcome.in_scope, outcome.named, outcome.not_found) == (3, 2, 1)
    names = dict(conn.execute("SELECT rxcui || ':' || name_type, name FROM drug_names").fetchall())
    assert names["8800141:full"] == "apixaban 5 MG Oral Tablet [Eliquis]"
    assert names["8800141:brand"] == "Eliquis"
    assert names["8800271:full"] == "lisinopril 10 MG Oral Tablet"
    assert {r[0] for r in conn.execute("SELECT rxcui FROM drug_names_missing")} == {"5550000"}


# Matcher


@pytest.fixture
def matcher(ask_conn: sqlite3.Connection, config: Config) -> DrugMatcher:
    return DrugMatcher(ask_conn, config.match)


@pytest.mark.parametrize(
    ("query", "match_type", "rxcuis"),
    [
        ("8800151", "id", ["8800151"]),
        ("RXCUI 8800151", "id", ["8800151"]),
        ("88000-0141-01", "id", ["8800141"]),
        ("88000014102", "id", ["8800141"]),
        ("Eliquis", "exact", ["8800141"]),
        ("apixaban", "exact", ["8800141"]),
        ("ozempic pen", "token", ["8800101", "8800102", "8800103"]),
        ("Ozempic 2 mg", "token", ["8800103"]),
        ("ozempik", "fuzzy", ["8800101", "8800102", "8800103"]),
        ("semiglutide", "fuzzy", ["8800101", "8800102", "8800103", "8800111", "8800421"]),
    ],
)
def test_matcher_stages(
    matcher: DrugMatcher, query: str, match_type: str, rxcuis: list[str]
) -> None:
    matches = matcher.match_all(query)
    assert {m.match_type for m in matches} == {match_type}
    assert sorted(m.rxcui for m in matches) == rxcuis
    assert all(0 <= m.score <= 100 for m in matches)


def test_matcher_stops_at_the_first_stage_with_results(matcher: DrugMatcher) -> None:
    # "Humalog" is an exact brand, so the token stage never adds Humalog Mix 75/25.
    assert [m.rxcui for m in matcher.match_all("Humalog")] == ["8800191"]


def test_matcher_groups_one_brand_with_many_rxcuis_as_one_product(matcher: DrugMatcher) -> None:
    products = matcher.group(matcher.match_all("Ozempic"))
    assert [(p.product, p.rxcuis) for p in products] == [
        ("Ozempic", ["8800101", "8800102", "8800103"])
    ]
    by_ingredient = matcher.group(matcher.match_all("semaglutide"))
    assert sorted(p.product for p in by_ingredient) == ["Ozempic", "Rybelsus", "Wegovy"]


def test_matcher_returns_nothing_for_unknown_or_empty_queries(matcher: DrugMatcher) -> None:
    assert matcher.match_all("Zorvalex") == []
    assert matcher.match_all("   ") == []
    assert matcher.match_all("4040404") == []


def test_near_misses_sit_between_the_floor_and_the_threshold(
    matcher: DrugMatcher, config: Config
) -> None:
    assert matcher.match_all("lantsu") == []
    misses = matcher.near_misses("lantsu")
    assert misses and misses[0].matched_on == "Lantus"
    assert all(
        config.match.near_miss_floor <= m.score < config.match.fuzzy_threshold for m in misses
    )


def test_match_drug_applies_the_limit(matcher: DrugMatcher) -> None:
    assert len(matcher.match_drug("insulin", 2)) == 2
    assert len(matcher.match_all("insulin")) == 6


# CLI


def test_names_cli_load_status_and_argument_check(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    for month in ("2026-01", "2026-02", "2026-03"):
        result = runner.invoke(app, ["load", "--month", month, "--dir", str(ASK), "--db", str(db)])
        assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["names", "load", "--fixture", str(ASK_NAMES), "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "Loaded" in result.output

    result = runner.invoke(app, ["names", "status", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert "have names (100.0%)" in result.output
    assert "fixture" in result.output

    result = runner.invoke(app, ["names", "load", "--db", str(db)])
    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_names_status_on_an_empty_database(tmp_path: Path) -> None:
    result = runner.invoke(app, ["names", "status", "--db", str(tmp_path / "e.db")])
    assert result.exit_code == 0
    assert "No RXCUIs found" in result.output


def test_names_fetch_cli_with_nothing_loaded_makes_no_requests(tmp_path: Path) -> None:
    result = runner.invoke(app, ["names", "fetch", "--db", str(tmp_path / "e.db")])
    assert result.exit_code == 0, result.output
    assert "No RXCUIs found" in result.output
