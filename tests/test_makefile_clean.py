"""`make clean` must never be able to delete data it did not generate.

An early version was `rm -rf $(DATA)`, which deleted a downloaded CMS release.
The version after that interpolated `$(FROM)` and `$(TO)` into the path, so
`make clean FROM=2026-05` or `make clean TO=reference` would have deleted the
real months or the committed drug name cache. These tests drive `make -n` with
hostile overrides and assert the recipe cannot be steered.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# Anything under data/ that clean must never touch: the real CMS releases and
# the committed reference cache.
PROTECTED = ("data/2026", "data/reference", "docs/")

HOSTILE_OVERRIDES = [
    [],
    ["FROM=2026-05", "TO=2026-06"],
    ["DATA=data", "TO=reference"],
    ["DATA=data", "FROM=reference"],
    ["DB=data/reference"],
    ["REPORT=data/reference/drug_names.csv"],
    ["DATA=/", "FROM=.", "TO=."],
    ["DATA=data/reference"],
]


def clean_recipe(*overrides: str) -> str:
    make = shutil.which("make")
    if make is None:  # pragma: no cover - make is present on any dev machine
        pytest.skip("make is not installed")
    result = subprocess.run(
        [make, "-n", "clean", *overrides],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize("overrides", HOSTILE_OVERRIDES, ids=lambda o: " ".join(o) or "no-args")
def test_clean_never_targets_protected_paths(overrides: list[str]) -> None:
    recipe = clean_recipe(*overrides)
    for path in PROTECTED:
        assert path not in recipe, f"`make clean {' '.join(overrides)}` would touch {path}"


@pytest.mark.parametrize("overrides", HOSTILE_OVERRIDES, ids=lambda o: " ".join(o) or "no-args")
def test_clean_removes_only_the_two_synthetic_months(overrides: list[str]) -> None:
    """Whatever is passed in, the only directories removed are the generated ones."""
    recipe = clean_recipe(*overrides)
    removed_dirs = [
        token
        for line in recipe.splitlines()
        if line.startswith("rm -rf ")
        for token in line.removeprefix("rm -rf ").split()
    ]
    data_dirs = sorted(d for d in removed_dirs if d.startswith("data"))
    assert data_dirs == ["data/2025-01", "data/2025-02"]


def test_clean_recipe_interpolates_no_variables_into_rm() -> None:
    """A recipe that deletes directories should not be steerable at all."""
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    body = makefile.split("\nclean:\n", 1)[1]
    recipe = [line for line in body.splitlines() if line.startswith("\t")]
    assert recipe, "clean target has no recipe"
    for line in recipe:
        if "rm -rf" in line or "rm -f" in line:
            assert "$(" not in line, f"clean interpolates a variable into a removal: {line.strip()}"


def test_the_committed_reference_cache_is_present() -> None:
    """The thing clean must protect, and the thing `make demo` needs offline."""
    cache = REPO / "data" / "reference" / "drug_names.csv"
    assert cache.is_file()
    assert cache.stat().st_size > 0
