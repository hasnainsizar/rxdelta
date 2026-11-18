from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from rxdelta.config import Config, load_config
from rxdelta.db import connect
from rxdelta.ingest.loader import load_month

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = FIXTURES / "snapshots"
BAD_SCHEMA = FIXTURES / "bad-schema"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "rxdelta.toml"


@pytest.fixture
def config() -> Config:
    return load_config(CONFIG_PATH)


@pytest.fixture
def lenient_config(config: Config) -> Config:
    """Same config with a higher rejected row ceiling, for tests that plant a
    bad row into a fixture small enough that one row is over the limit."""
    return replace(config, ingest=replace(config.ingest, max_rejected_pct=25.0))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def loaded(conn: sqlite3.Connection, config: Config) -> sqlite3.Connection:
    load_month(conn, config, "2024-01", SNAPSHOTS)
    load_month(conn, config, "2024-02", SNAPSHOTS)
    return conn


ASK = FIXTURES / "ask"
ASK_MONTHS = ("2026-01", "2026-02", "2026-03")
ASK_NAMES = ASK / "drug_names.csv"


def build_ask_db(path: Path, config: Config) -> Path:
    """Three fixture months plus fixture names, the database the eval set uses."""
    from rxdelta.names import fixture as names_fixture

    connection = connect(path)
    for month in ASK_MONTHS:
        load_month(connection, config, month, ASK)
    names_fixture.load(connection, ASK_NAMES)
    connection.close()
    return path


@pytest.fixture(scope="session")
def ask_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Built once per session. Tests must treat it as read only."""
    return build_ask_db(tmp_path_factory.mktemp("ask") / "ask.db", load_config(CONFIG_PATH))


@pytest.fixture
def ask_conn(ask_db: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"file:{ask_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


REPO = Path(__file__).resolve().parents[1]
EVAL_SET = REPO / "evals" / "rxdelta_v1.jsonl"
HANDWRITTEN = REPO / "evals" / "handwritten.jsonl"


def build_cli_eval_db(path: Path) -> Path:
    """The database `make evaldb` builds, made through the CLI the same way."""
    from typer.testing import CliRunner

    from rxdelta.cli import app

    runner = CliRunner()
    for month in ASK_MONTHS:
        result = runner.invoke(
            app, ["load", "--month", month, "--dir", str(ASK), "--db", str(path)]
        )
        assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["names", "load", "--fixture", str(ASK_NAMES), "--db", str(path)])
    assert result.exit_code == 0, result.output
    return path


@pytest.fixture(scope="session")
def eval_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Same contents as `make evaldb`, which the committed eval set was built from."""
    return build_cli_eval_db(tmp_path_factory.mktemp("evaldb") / "evals.db")
