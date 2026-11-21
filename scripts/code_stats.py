"""Lines, modules, functions and classes per package, from tracked Python files.

A line counts when it is not blank and not only a comment; docstrings count.
Prints a markdown table.
"""

from __future__ import annotations

import ast
import subprocess
from collections import defaultdict
from pathlib import Path

PACKAGES = ("agent", "ask", "db", "diff", "evals", "ingest", "names", "report", "tools")


def group(path: str) -> str:
    parts = Path(path).parts
    if parts[0] == "rxdelta":
        return (
            f"rxdelta/{parts[1]}"
            if len(parts) > 2 and parts[1] in PACKAGES
            else "rxdelta (top level)"
        )
    return parts[0]


def code_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


def main() -> None:
    files = subprocess.run(
        ["git", "ls-files", "*.py"], capture_output=True, text=True, check=True
    ).stdout.split()
    stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in files:
        text = Path(path).read_text(encoding="utf-8")
        tree = ast.parse(text)
        row = stats[group(path)]
        row["lines"] += code_lines(text)
        row["modules"] += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                row["functions"] += 1
                row["tests"] += node.name.startswith("test_")
            elif isinstance(node, ast.ClassDef):
                row["classes"] += 1
    print("| package | lines | modules | functions | classes | test functions |")
    print("|---|---|---|---|---|---|")
    totals: dict[str, int] = defaultdict(int)
    for name in sorted(stats):
        row = stats[name]
        print(
            f"| {name} | {row['lines']} | {row['modules']} | {row['functions']} | "
            f"{row['classes']} | {row['tests']} |"
        )
        for key, value in row.items():
            totals[key] += value
    code = sum(v["lines"] for k, v in stats.items() if k.startswith("rxdelta"))
    print(
        f"| **all** | {totals['lines']} | {totals['modules']} | {totals['functions']} | "
        f"{totals['classes']} | {totals['tests']} |"
    )
    print(f"\nNon-test code (rxdelta/): {code} lines.")


if __name__ == "__main__":
    main()
