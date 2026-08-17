from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from rxdelta.config import Config
from rxdelta.db import queries
from rxdelta.diff.engine import diff_snapshots
from rxdelta.diff.impact import build_groups
from rxdelta.names.cache import DrugName, load_cache, merge_into_db, read_csv, write_csv
from rxdelta.report import format as fmt
from rxdelta.report import html

CSV_HEADER = "rxcui,name,source,fetched_at\n"


def test_missing_cache_file_is_an_empty_cache_not_an_error(tmp_path: Path) -> None:
    assert read_csv(tmp_path / "nope.csv") == {}


def test_cache_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "names.csv"
    names = {
        "100002": DrugName("100002", "Betaxolol 10 MG", "rxnav", "2025-01-01T00:00:00+00:00"),
        "100001": DrugName("100001", "Amlodipine 5 MG", "rxnav", "2025-01-01T00:00:00+00:00"),
    }
    assert write_csv(path, names) == 2
    assert read_csv(path) == names


def test_cache_is_written_in_rxcui_order_so_it_does_not_churn(tmp_path: Path) -> None:
    path = tmp_path / "names.csv"
    write_csv(
        path,
        {
            "300": DrugName("300", "C", "rxnav", "t"),
            "20": DrugName("20", "B", "rxnav", "t"),
            "100": DrugName("100", "A", "rxnav", "t"),
        },
    )
    rows = path.read_text(encoding="utf-8").splitlines()[1:]
    assert [r.split(",")[0] for r in rows] == ["20", "100", "300"]


def test_unresolved_rxcuis_are_kept_in_the_cache_but_not_in_the_database(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A name RxNav could not resolve is remembered so a later refresh does not
    ask again, but it must not reach the table as an empty name."""
    path = tmp_path / "names.csv"
    path.write_text(
        CSV_HEADER + "111,Real Drug 5 MG,rxnav,t\n222,,rxnav:not-found,t\n", encoding="utf-8"
    )
    cache = read_csv(path)
    assert set(cache) == {"111", "222"}
    assert merge_into_db(conn, cache) == 1
    assert queries.drug_names(conn) == {"111": "Real Drug 5 MG"}


def test_load_cache_is_idempotent(conn: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "names.csv"
    path.write_text(CSV_HEADER + "111,Aspirin 81 MG,rxnav,t\n", encoding="utf-8")
    load_cache(conn, path)
    load_cache(conn, path)
    assert queries.name_cache_size(conn) == 1


def test_drug_label_falls_back_to_the_ndc_when_no_name_is_cached() -> None:
    assert fmt.drug_label("Amlodipine 5 MG", "11111111101") == "Amlodipine 5 MG"
    assert fmt.drug_label(None, "11111111101") == "11111-1111-01"
    assert fmt.drug_label("", "11111111101") == "11111-1111-01"


def test_report_renders_the_ndc_when_the_name_is_missing(
    loaded: sqlite3.Connection, config: Config
) -> None:
    """The fallback path: no names cached at all, so every row shows its NDC and
    the cell is neither blank nor an error."""
    result = diff_snapshots(loaded, "2024-01", "2024-02")
    groups = build_groups(loaded, config, result)
    assert all(g.drug_name is None for g in groups)

    page = html.render(config, result, groups, generated_at="frozen")
    rows = html.build_rows(config, groups)
    assert rows
    for row in rows:
        assert not row.has_name
        assert row.drug_label == row.ndc
        assert row.drug_label.strip()
    assert "11111-1111-01" in page
    assert "no name cached" in page


def test_report_shows_the_name_when_one_is_cached(
    loaded: sqlite3.Connection, config: Config, tmp_path: Path
) -> None:
    path = tmp_path / "names.csv"
    path.write_text(CSV_HEADER + "100001,Amlodipine 5 MG oral tablet,rxnav,t\n", encoding="utf-8")
    load_cache(loaded, path)

    result = diff_snapshots(loaded, "2024-01", "2024-02")
    groups = build_groups(loaded, config, result)
    named = [g for g in groups if g.rxcui == "100001"]
    assert named and named[0].drug_name == "Amlodipine 5 MG oral tablet"

    page = html.render(config, result, groups, generated_at="frozen")
    assert "Amlodipine 5 MG oral tablet" in page
    # The NDC stays visible, demoted to the secondary line.
    assert "11111-1111-01" in page


def test_config_resolves_the_cache_path_against_the_repo(config: Config) -> None:
    resolved = config.resolve(config.names.cache_path)
    assert resolved.is_absolute()
    assert resolved.name == "drug_names.csv"


def test_committed_cache_exists_and_covers_the_sample_rxcuis() -> None:
    """`make demo` has to render names with no network, which only works if the
    cache is committed and non-empty."""
    from rxdelta.config import load_config
    from tests.conftest import CONFIG_PATH

    config = load_config(CONFIG_PATH)
    cache = read_csv(config.resolve(config.names.cache_path))
    assert len(cache) > 100
    resolved = [n for n in cache.values() if n.name]
    assert resolved
    assert all(n.source for n in cache.values())


def test_lenient_config_still_resolves_names(config: Config) -> None:
    tweaked = replace(config, names=replace(config.names, cache_path="data/reference/other.csv"))
    assert tweaked.resolve(tweaked.names.cache_path).name == "other.csv"
