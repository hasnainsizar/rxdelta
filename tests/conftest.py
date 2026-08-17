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
