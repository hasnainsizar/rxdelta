"""Typed configuration loader.

Everything tunable lives in config/rxdelta.toml. This module reads it once,
validates it, and hands back frozen dataclasses so the rest of the codebase
never touches a raw dict or a magic number.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from rxdelta.types import ConfigError

_ENV_VAR = "RXDELTA_CONFIG"
_NDC_POLICIES = frozenset({"reject", "assume_5_4_1", "assume_5_3_2", "assume_4_4_2"})
_COST_KINDS = frozenset({"copay", "coinsurance", "not_offered"})
_SORT_FIELDS = ("severity", "plan_count", "range_width", "abs_midpoint", "ndc")
# The four pharmacy channels the beneficiary cost file publishes, in file order.
COST_CHANNELS = ("retail_preferred", "retail_non_preferred", "mail_preferred", "mail_non_preferred")


@dataclass(frozen=True)
class SourceConfig:
    delimiter: str
    encoding: str
    patterns: dict[str, tuple[str, ...]]
    exclude_dir_patterns: tuple[str, ...]


@dataclass(frozen=True)
class NdcConfig:
    unhyphenated_10_digit: str


@dataclass(frozen=True)
class IngestConfig:
    max_rejected_pct: float
    insert_chunk_rows: int
    ndc: NdcConfig


@dataclass(frozen=True)
class DaysSupplyCode:
    label: str
    # None when CMS publishes no length for the code, as for "other". A row
    # using it cannot be normalized and is left unpriced.
    days: int | None


@dataclass(frozen=True)
class CostTypeCode:
    label: str
    kind: str  # "copay" or "coinsurance"


@dataclass(frozen=True)
class CodeMaps:
    coverage_level: dict[str, str]
    cost_type: dict[str, CostTypeCode]
    days_supply: dict[str, DaysSupplyCode]

    def cost_type_code(self, code: str, file_name: str) -> CostTypeCode:
        if code not in self.cost_type:
            raise ConfigError(
                f"Unknown COST_TYPE code {code!r} in {file_name}. "
                f"Known codes: {', '.join(sorted(self.cost_type))}. "
                "Add it to [codes.cost_type] in config/rxdelta.toml."
            )
        return self.cost_type[code]

    def coverage_level_name(self, code: str, file_name: str) -> str:
        return _require_code(self.coverage_level, code, "COVERAGE_LEVEL", file_name)

    def days_for(self, code: str, file_name: str) -> int | None:
        if code not in self.days_supply:
            raise ConfigError(
                f"Unknown DAYS_SUPPLY code {code!r} in {file_name}. "
                f"Known codes: {', '.join(sorted(self.days_supply))}. "
                "Add it to [codes.days_supply] in config/rxdelta.toml."
            )
        return self.days_supply[code].days


@dataclass(frozen=True)
class ModalCase:
    """The single most common fill: one pharmacy channel, one supply length."""

    channel: str
    days_supply: str


@dataclass(frozen=True)
class ImpactConfig:
    coverage_level: str
    normalize_days: int
    modal: ModalCase


@dataclass(frozen=True)
class SeverityConfig:
    weight_cost: float
    weight_direction: float
    weight_plans: float
    weight_restriction: float
    cost_reference: float
    plan_reference: int
    open_ended_factor: float

    @property
    def weight_total(self) -> float:
        return (
            self.weight_cost + self.weight_direction + self.weight_plans + self.weight_restriction
        )


@dataclass(frozen=True)
class SeverityBand:
    """A named range of the severity score, so rank never rests on order alone."""

    label: str
    min_score: float


@dataclass(frozen=True)
class SortKey:
    field: str
    descending: bool


@dataclass(frozen=True)
class ReportConfig:
    low_result_floor: int
    top_changes: int
    max_plans_listed: int
    severity_bands: tuple[SeverityBand, ...]
    sort_order: tuple[SortKey, ...]

    def band_for(self, score: float) -> SeverityBand:
        for band in self.severity_bands:
            if score >= band.min_score:
                return band
        return self.severity_bands[-1]


@dataclass(frozen=True)
class NamesConfig:
    cache_path: str
    api_base: str
    requests_per_second: float
    request_timeout_seconds: float
    max_retries: int
    progress_every: int


@dataclass(frozen=True)
class Config:
    path: Path
    source: SourceConfig
    ingest: IngestConfig
    tiers: dict[int, str]
    codes: CodeMaps
    impact: ImpactConfig
    severity: SeverityConfig
    report: ReportConfig
    names: NamesConfig

    def resolve(self, relative: str) -> Path:
        """Resolve a config-relative path against the repo, not the caller cwd."""
        candidate = Path(relative).expanduser()
        if candidate.is_absolute():
            return candidate
        return (self.path.resolve().parent.parent / candidate).resolve()

    def tier_label(self, tier: int | None) -> str:
        if tier is None:
            return "not covered"
        return self.tiers.get(tier, f"Tier {tier}")


def _require_code(mapping: dict[str, str], code: str, field: str, file_name: str) -> str:
    if code not in mapping:
        raise ConfigError(
            f"Unknown {field} code {code!r} in {file_name}. "
            f"Known codes: {', '.join(sorted(mapping))}. "
            f"Add it to [codes.{field.lower()}] in config/rxdelta.toml."
        )
    return mapping[code]


def _section(data: dict[str, Any], *path: str) -> dict[str, Any]:
    node: Any = data
    for part in path:
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"Missing config section [{'.'.join(path)}]")
        node = node[part]
    if not isinstance(node, dict):
        raise ConfigError(f"Config section [{'.'.join(path)}] must be a table")
    return node


def _get(section: dict[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in section:
        raise ConfigError(f"Missing config key {key!r} in [{where}]")
    value = section[key]
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, kind) or isinstance(value, bool) is not (kind is bool):
        raise ConfigError(
            f"Config key {key!r} in [{where}] must be {kind.__name__}, got {type(value).__name__}"
        )
    return value


def _parse_source(data: dict[str, Any]) -> SourceConfig:
    section = _section(data, "source")
    files = _section(section, "files")
    patterns: dict[str, tuple[str, ...]] = {}
    for file_type, spec in files.items():
        if not isinstance(spec, dict) or "patterns" not in spec:
            raise ConfigError(f"[source.files.{file_type}] must define 'patterns'")
        raw = spec["patterns"]
        if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw) or not raw:
            raise ConfigError(
                f"[source.files.{file_type}].patterns must be a non-empty string list"
            )
        patterns[file_type] = tuple(str(p) for p in raw)
    excludes = section.get("exclude_dir_patterns", [])
    if not isinstance(excludes, list) or not all(isinstance(e, str) for e in excludes):
        raise ConfigError("[source].exclude_dir_patterns must be a list of strings")
    return SourceConfig(
        delimiter=_get(section, "delimiter", str, "source"),
        encoding=_get(section, "encoding", str, "source"),
        patterns=patterns,
        exclude_dir_patterns=tuple(str(e) for e in excludes),
    )


def _parse_ingest(data: dict[str, Any]) -> IngestConfig:
    section = _section(data, "ingest")
    ndc_section = _section(section, "ndc")
    policy = _get(ndc_section, "unhyphenated_10_digit", str, "ingest.ndc")
    if policy not in _NDC_POLICIES:
        raise ConfigError(
            f"[ingest.ndc].unhyphenated_10_digit must be one of "
            f"{', '.join(sorted(_NDC_POLICIES))}, got {policy!r}"
        )
    pct = _get(section, "max_rejected_pct", float, "ingest")
    if not 0.0 <= pct <= 100.0:
        raise ConfigError("[ingest].max_rejected_pct must be between 0 and 100")
    chunk = _get(section, "insert_chunk_rows", int, "ingest")
    if chunk < 1:
        raise ConfigError("[ingest].insert_chunk_rows must be at least 1")
    return IngestConfig(
        max_rejected_pct=pct,
        insert_chunk_rows=chunk,
        ndc=NdcConfig(unhyphenated_10_digit=policy),
    )


def _parse_tiers(data: dict[str, Any]) -> dict[int, str]:
    section = _section(data, "tiers")
    tiers: dict[int, str] = {}
    for key, label in section.items():
        try:
            tiers[int(key)] = str(label)
        except ValueError as exc:
            raise ConfigError(f"[tiers] key {key!r} must be an integer tier level") from exc
    if not tiers:
        raise ConfigError("[tiers] must define at least one tier label")
    return tiers


def _parse_codes(data: dict[str, Any]) -> CodeMaps:
    codes = _section(data, "codes")
    coverage = {str(k): str(v) for k, v in _section(codes, "coverage_level").items()}
    cost_type: dict[str, CostTypeCode] = {}
    for key, spec in _section(codes, "cost_type").items():
        where = f"codes.cost_type.{key}"
        if not isinstance(spec, dict):
            raise ConfigError(f"[{where}] must be a table with label and kind")
        kind = _get(spec, "kind", str, where)
        if kind not in _COST_KINDS:
            raise ConfigError(
                f"[{where}].kind must be one of {', '.join(sorted(_COST_KINDS))}, got {kind!r}"
            )
        cost_type[str(key)] = CostTypeCode(label=_get(spec, "label", str, where), kind=kind)
    days_supply: dict[str, DaysSupplyCode] = {}
    for key, spec in _section(codes, "days_supply").items():
        if not isinstance(spec, dict):
            raise ConfigError(f"[codes.days_supply.{key}] must be a table with label and days")
        days: int | None = None
        if "days" in spec:
            days = _get(spec, "days", int, f"codes.days_supply.{key}")
            if days <= 0:
                raise ConfigError(f"[codes.days_supply.{key}].days must be positive")
        days_supply[str(key)] = DaysSupplyCode(
            label=_get(spec, "label", str, f"codes.days_supply.{key}"), days=days
        )
    if not coverage:
        raise ConfigError("[codes.coverage_level] must define at least one code")
    if not cost_type:
        raise ConfigError("[codes.cost_type] must define at least one code")
    if not days_supply:
        raise ConfigError("[codes.days_supply] must define at least one code")
    return CodeMaps(coverage_level=coverage, cost_type=cost_type, days_supply=days_supply)


def _parse_impact(data: dict[str, Any], codes: CodeMaps) -> ImpactConfig:
    section = _section(data, "impact")
    level = _get(section, "coverage_level", str, "impact")
    if level not in codes.coverage_level:
        raise ConfigError(
            f"[impact].coverage_level {level!r} is not defined in [codes.coverage_level]"
        )
    days = _get(section, "normalize_days", int, "impact")
    if days <= 0:
        raise ConfigError("[impact].normalize_days must be positive")
    modal_section = _section(section, "modal")
    channel = _get(modal_section, "channel", str, "impact.modal")
    if channel not in COST_CHANNELS:
        raise ConfigError(
            f"[impact.modal].channel must be one of {', '.join(COST_CHANNELS)}, got {channel!r}"
        )
    supply = _get(modal_section, "days_supply", str, "impact.modal")
    if supply not in codes.days_supply:
        raise ConfigError(
            f"[impact.modal].days_supply {supply!r} is not defined in [codes.days_supply]"
        )
    if codes.days_supply[supply].days is None:
        raise ConfigError(
            f"[impact.modal].days_supply {supply!r} has no published length, so it cannot "
            "be the modal case"
        )
    return ImpactConfig(
        coverage_level=level,
        normalize_days=days,
        modal=ModalCase(channel=channel, days_supply=supply),
    )


def _parse_severity(data: dict[str, Any]) -> SeverityConfig:
    section = _section(data, "severity")
    severity = SeverityConfig(
        weight_cost=_get(section, "weight_cost", float, "severity"),
        weight_direction=_get(section, "weight_direction", float, "severity"),
        weight_plans=_get(section, "weight_plans", float, "severity"),
        weight_restriction=_get(section, "weight_restriction", float, "severity"),
        cost_reference=_get(section, "cost_reference", float, "severity"),
        plan_reference=_get(section, "plan_reference", int, "severity"),
        open_ended_factor=_get(section, "open_ended_factor", float, "severity"),
    )
    if severity.weight_total <= 0:
        raise ConfigError("[severity] weights must sum to more than zero")
    if severity.cost_reference <= 0:
        raise ConfigError("[severity].cost_reference must be positive")
    if severity.plan_reference < 1:
        raise ConfigError("[severity].plan_reference must be at least 1")
    return severity


def _parse_report(data: dict[str, Any]) -> ReportConfig:
    section = _section(data, "report")
    floor = _get(section, "low_result_floor", int, "report")
    top = _get(section, "top_changes", int, "report")
    if floor < 0:
        raise ConfigError("[report].low_result_floor must not be negative")
    if top < 1:
        raise ConfigError("[report].top_changes must be at least 1")
    listed = _get(section, "max_plans_listed", int, "report")
    if listed < 1:
        raise ConfigError("[report].max_plans_listed must be at least 1")
    return ReportConfig(
        low_result_floor=floor,
        top_changes=top,
        max_plans_listed=listed,
        severity_bands=_parse_severity_bands(section),
        sort_order=_parse_sort_order(section),
    )


def _parse_sort_order(section: dict[str, Any]) -> tuple[SortKey, ...]:
    raw = section.get("sort_order")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("[report].sort_order must list at least one sort key")
    keys: list[SortKey] = []
    for index, entry in enumerate(raw):
        where = f"report.sort_order[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"[{where}] must be a table with field and direction")
        field = _get(entry, "field", str, where)
        if field not in _SORT_FIELDS:
            raise ConfigError(
                f"[{where}].field must be one of {', '.join(_SORT_FIELDS)}, got {field!r}"
            )
        direction = _get(entry, "direction", str, where)
        if direction not in ("asc", "desc"):
            raise ConfigError(f"[{where}].direction must be asc or desc, got {direction!r}")
        keys.append(SortKey(field=field, descending=direction == "desc"))
    if keys[-1].field != "ndc":
        raise ConfigError(
            "[report].sort_order must end with the ndc field so the ranking is deterministic"
        )
    return tuple(keys)


def _parse_names(data: dict[str, Any]) -> NamesConfig:
    section = _section(data, "names")
    rate = _get(section, "requests_per_second", float, "names")
    if rate <= 0:
        raise ConfigError("[names].requests_per_second must be positive")
    retries = _get(section, "max_retries", int, "names")
    if retries < 0:
        raise ConfigError("[names].max_retries must not be negative")
    return NamesConfig(
        cache_path=_get(section, "cache_path", str, "names"),
        api_base=_get(section, "api_base", str, "names").rstrip("/"),
        requests_per_second=rate,
        request_timeout_seconds=_get(section, "request_timeout_seconds", float, "names"),
        max_retries=retries,
        progress_every=_get(section, "progress_every", int, "names"),
    )


def _parse_severity_bands(section: dict[str, Any]) -> tuple[SeverityBand, ...]:
    raw = section.get("severity_bands")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("[[report.severity_bands]] must define at least one band")
    bands: list[SeverityBand] = []
    for index, entry in enumerate(raw):
        where = f"report.severity_bands[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"[[{where}]] must be a table with label and min_score")
        bands.append(
            SeverityBand(
                label=_get(entry, "label", str, where),
                min_score=_get(entry, "min_score", float, where),
            )
        )
    ordered = sorted(bands, key=lambda b: b.min_score, reverse=True)
    if [b.label for b in ordered] != [b.label for b in bands]:
        raise ConfigError("[[report.severity_bands]] must be listed from highest to lowest")
    if ordered[-1].min_score != 0.0:
        raise ConfigError("The lowest [[report.severity_bands]] entry must have min_score = 0")
    return tuple(ordered)


def default_config_path() -> Path:
    """Repo checkout first, then the copy bundled into the installed wheel."""
    import os

    override = os.environ.get(_ENV_VAR)
    if override:
        return Path(override)
    repo_copy = Path(__file__).resolve().parent.parent / "config" / "rxdelta.toml"
    if repo_copy.is_file():
        return repo_copy
    return Path(__file__).resolve().parent / "_bundled" / "rxdelta.toml"


def load_config(path: Path | None = None) -> Config:
    resolved = (path or default_config_path()).expanduser()
    if not resolved.is_file():
        raise ConfigError(f"Config file not found: {resolved}")
    try:
        data = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config file {resolved} is not valid TOML: {exc}") from exc
    codes = _parse_codes(data)
    return Config(
        path=resolved,
        source=_parse_source(data),
        ingest=_parse_ingest(data),
        tiers=_parse_tiers(data),
        codes=codes,
        impact=_parse_impact(data, codes),
        severity=_parse_severity(data),
        report=_parse_report(data),
        names=_parse_names(data),
    )


@lru_cache(maxsize=8)
def _cached(path_str: str) -> Config:
    return load_config(Path(path_str))


def get_config(path: Path | None = None) -> Config:
    """Cached accessor for the common case of one config per process."""
    return _cached(str(path or default_config_path()))
