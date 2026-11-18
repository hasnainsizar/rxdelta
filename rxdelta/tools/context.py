"""What a tool needs to run: the database path and the config."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from rxdelta.config import Config
from rxdelta.types import RxdeltaError


@dataclass(frozen=True)
class ToolContext:
    db_path: Path
    config: Config

    def connect(self) -> sqlite3.Connection:
        """A read-only connection. Any write through it fails."""
        return open_readonly(self.db_path)


def open_readonly(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise RxdeltaError(f"Database not found: {path}. Load a month first.")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn
