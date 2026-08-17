"""Measure load and diff time on the generated sample data.

The README quotes whatever this prints. Run it after `make sample`, or with
--generate to build the sample data first.

Usage: python scripts/benchmark.py [--repeat 3]
"""

from __future__ import annotations

import argparse
import platform
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from rxdelta.config import load_config
from rxdelta.db import connect
from rxdelta.diff.engine import diff_snapshots
from rxdelta.diff.impact import build_groups
from rxdelta.ingest.loader import load_month
from rxdelta.names.cache import load_cache
from rxdelta.report import html

DEFAULT_MONTHS = ("2025-01", "2025-02")


@dataclass(frozen=True)
class Timing:
    label: str
    seconds: float
    detail: str


def _best(samples: list[float]) -> float:
    return min(samples)


def run(data_dir: Path, repeat: int, months: tuple[str, str]) -> list[Timing]:
    config = load_config()
    load_samples: dict[str, list[float]] = {month: [] for month in months}
    diff_samples: list[float] = []
    report_samples: list[float] = []
    rows = 0
    changes = 0
    groups = 0

    for _ in range(repeat):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "bench.db")
            for month in months:
                start = time.perf_counter()
                summary = load_month(conn, config, month, data_dir)
                load_samples[month].append(time.perf_counter() - start)
                rows = summary.total_rows

            start = time.perf_counter()
            load_cache(conn, config.resolve(config.names.cache_path))
            result = diff_snapshots(conn, *months)
            change_groups = build_groups(conn, config, result)
            diff_samples.append(time.perf_counter() - start)
            changes = len(result.changes)
            groups = len(change_groups)

            start = time.perf_counter()
            html.render(config, result, change_groups, generated_at="benchmark")
            report_samples.append(time.perf_counter() - start)
            conn.close()

    timings = [
        Timing(
            label=f"load {month}",
            seconds=_best(load_samples[month]),
            detail=f"{rows:,} rows written",
        )
        for month in months
    ]
    timings.append(
        Timing(
            label="diff and score",
            seconds=_best(diff_samples),
            detail=f"{changes:,} changes, {groups:,} groups",
        )
    )
    timings.append(
        Timing(label="render report", seconds=_best(report_samples), detail=f"{groups:,} groups")
    )
    return timings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--months",
        nargs=2,
        metavar=("FROM", "TO"),
        default=list(DEFAULT_MONTHS),
        help="Snapshot months to measure.",
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--generate", action="store_true", help="Generate the sample data before measuring."
    )
    args = parser.parse_args()

    if args.generate:
        from scripts.generate_sample_data import generate

        generate(args.dir)

    timings = run(args.dir, args.repeat, (args.months[0], args.months[1]))
    total = sum(t.seconds for t in timings)
    print(f"Python {platform.python_version()} on {platform.system()} {platform.machine()}")
    print(f"Best of {args.repeat} runs.\n")
    for timing in timings:
        print(f"{timing.seconds:7.2f}s  {timing.label:<18} {timing.detail}")
    print(f"{total:7.2f}s  {'end to end':<18}")


if __name__ == "__main__":
    main()
