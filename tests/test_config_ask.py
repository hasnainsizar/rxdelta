from __future__ import annotations

from pathlib import Path

import pytest

from rxdelta.config import CATEGORIES, load_config
from rxdelta.types import ConfigError
from tests.conftest import CONFIG_PATH


def write_variant(tmp_path: Path, old: str, new: str) -> Path:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "rxdelta.toml"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


def test_the_new_sections_load() -> None:
    config = load_config(CONFIG_PATH)
    assert config.match.fuzzy_threshold == 85.0
    assert config.tools.cap("get_changes") == 100
    assert set(config.evalset.mix) == set(CATEGORIES)
    assert abs(sum(config.evalset.mix.values()) - 1.0) < 1e-9
    assert config.evalset.splits.dev == 0.7
    assert config.eval.cost_tolerance == 0.01
    assert len(config.sha256) == 64


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("coverage_lookup = 0.10", "coverage_lookup = 0.20", "must sum to 1"),
        ("dev = 0.7", "dev = 0.6", "must sum to 1"),
        ("no_matching_changes = 0.40", "no_matching_changes = 0.50", "must sum to 1"),
        ("fuzzy_threshold = 85.0", "fuzzy_threshold = 50.0", "near_miss_floor"),
        ("get_changes = 100", "get_changes = 0", "at least 1"),
        ("typo = 0.05", "typoo = 0.05", "evalset"),
        ("cost_tolerance = 0.01", "cost_tolerance = -1.0", "cost_tolerance"),
        ("workers = 4", "workers = 0", "workers"),
    ],
)
def test_bad_values_are_rejected_with_a_clear_message(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write_variant(tmp_path, old, new))
