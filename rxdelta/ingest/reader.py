"""File discovery and delimited text parsing.

The delimiter, encoding and file name patterns come from config so a different
delimited dataset can be pointed at without code changes.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from rxdelta.config import Config
from rxdelta.types import LoadError

_SNAPSHOT_SUFFIXES = (".txt", ".csv", ".psv", ".dat")


@dataclass(frozen=True)
class SourceFile:
    file_type: str
    path: Path

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class ParsedRow:
    line_number: int
    values: dict[str, str]


def month_dir(root: Path, month: str) -> Path:
    path = root / month
    if not path.is_dir():
        raise LoadError(
            f"No snapshot directory for {month} at {path}. "
            "Expected one directory per month, for example data/2025-01/."
        )
    return path


def _candidates(config: Config, directory: Path) -> list[Path]:
    """Every data file under the month, at any depth.

    A CMS release nests each table in its own directory, one level below the
    month, and the directory names contain double spaces. Walking the tree reads
    both that layout and the flat one the synthetic fixtures use, without
    renaming or copying anything on disk.
    """
    found: list[Path] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _SNAPSHOT_SUFFIXES:
            continue
        relative = path.relative_to(directory).parts[:-1]
        if any(
            fnmatch.fnmatch(part.lower(), pattern.lower())
            for part in relative
            for pattern in config.source.exclude_dir_patterns
        ):
            continue
        found.append(path)
    return found


def discover(config: Config, directory: Path) -> dict[str, SourceFile]:
    """Match one file per declared file type. Ambiguity is an error, not a coin flip."""
    candidates = _candidates(config, directory)
    found: dict[str, SourceFile] = {}
    missing: list[str] = []
    for file_type, patterns in sorted(config.source.patterns.items()):
        matches = [
            p
            for p in candidates
            if any(fnmatch.fnmatch(p.name.lower(), pattern.lower()) for pattern in patterns)
        ]
        if not matches:
            missing.append(f"{file_type} (patterns: {', '.join(patterns)})")
            continue
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches)
            raise LoadError(
                f"Several files in {directory} match the {file_type} patterns: {names}. "
                "Narrow the patterns in [source.files] so exactly one file matches."
            )
        found[file_type] = SourceFile(file_type=file_type, path=matches[0])
    if missing:
        listing = (
            ", ".join(str(p.relative_to(directory)) for p in candidates)
            or "no files with a recognized suffix"
        )
        raise LoadError(
            f"Missing source files in {directory}: {'; '.join(missing)}. "
            f"Directory contains: {listing}."
        )
    return found


def read_header(config: Config, path: Path) -> list[str]:
    with path.open("r", encoding=config.source.encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=config.source.delimiter)
        for row in reader:
            return [cell.strip() for cell in row]
    raise LoadError(f"{path.name} is empty, expected a header row")


def read_rows(config: Config, path: Path) -> Iterator[ParsedRow]:
    """Yield data rows with their 1-based file line number.

    Rows whose field count does not match the header are yielded with the
    mismatch recorded under a reserved key so the loader can reject them.
    """
    with path.open("r", encoding=config.source.encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=config.source.delimiter)
        header: list[str] | None = None
        for line_number, row in enumerate(reader, start=1):
            if header is None:
                header = [cell.strip() for cell in row]
                continue
            if not row or (len(row) == 1 and not row[0].strip()):
                continue
            if len(row) != len(header):
                yield ParsedRow(
                    line_number=line_number,
                    values={_FIELD_COUNT_ERROR: f"expected {len(header)} fields, got {len(row)}"},
                )
                continue
            yield ParsedRow(line_number=line_number, values=dict(zip(header, row, strict=True)))


_FIELD_COUNT_ERROR = "__field_count_error__"


def field_count_error(values: dict[str, str]) -> str | None:
    return values.get(_FIELD_COUNT_ERROR)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
