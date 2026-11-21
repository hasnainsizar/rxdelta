# Simplification baseline

Measured on `chore/simplify` at `5f62ccc` (the Phase 2 merge into `main`),
before any change, with `uv run python scripts/code_stats.py`. A line counts
when it is not blank and not only a comment; docstrings count. The two new
scripts of this pass are not yet tracked, so they are not in these numbers.

| package | lines | modules | functions | classes | test functions |
|---|---|---|---|---|---|
| rxdelta (top level) | 2058 | 6 | 80 | 37 | 0 |
| rxdelta/agent | 1040 | 10 | 68 | 18 | 0 |
| rxdelta/ask | 164 | 3 | 7 | 18 | 0 |
| rxdelta/db | 294 | 4 | 19 | 1 | 0 |
| rxdelta/diff | 656 | 4 | 35 | 6 | 0 |
| rxdelta/evals | 3802 | 12 | 187 | 25 | 0 |
| rxdelta/ingest | 804 | 6 | 44 | 10 | 0 |
| rxdelta/names | 770 | 9 | 50 | 14 | 0 |
| rxdelta/report | 408 | 4 | 24 | 6 | 0 |
| rxdelta/tools | 1573 | 13 | 56 | 35 | 0 |
| scripts | 1306 | 4 | 36 | 6 | 0 |
| tests | 5016 | 33 | 450 | 5 | 351 |
| **all** | 17891 | 108 | 1056 | 181 | 351 |

Non-test code (rxdelta/): 11569 lines.

| measure | value |
|---|---|
| tests collected by pytest (parametrized cases counted) | 498 |
| line coverage of `rxdelta/` (`pytest --cov=rxdelta`) | 94.34% |
| `make check` (ruff, ruff format, mypy, pytest), wall clock | 16.5 s |

The safety net, `scripts/simplify_check.sh --baseline`, saved the reference
command output, tool schemas, prompts, answer schema, one call per tool and a
rescore of every saved run under `runs/simplify-baseline/` (git ignored). Two
`--full` runs against it both passed byte for byte before any change.
