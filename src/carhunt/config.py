"""Settings (config.yaml) and .env loading."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


@dataclass(frozen=True)
class SearchSettings:
    source_city: str = "pittsburgh"
    max_price: int = 7000
    min_price: int = 2000
    price_bands: int = 1  # 1 = one search over the whole range
    region: str = "Pittsburgh, PA"

    def __post_init__(self) -> None:
        if not isinstance(self.price_bands, int) or self.price_bands < 1:
            raise ValueError(f"invalid search.price_bands: {self.price_bands!r} (must be >= 1)")


@dataclass(frozen=True)
class ReportSettings:
    timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError, OSError) as exc:
            raise ValueError(f"invalid report.timezone: {self.timezone!r}") from exc

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class PollSettings:
    interval_min: int = 15
    max_details_per_run: int = 30
    detail_delay_s: tuple[float, float] = (2, 5)


@dataclass(frozen=True)
class NotifySettings:
    price_drop_min_usd: int = 200
    price_drop_min_pct: float = 5
    min_reliability: int = 70
    min_value: int = 70
    min_sum: int = 0  # reliability + value; 0 = off


@dataclass(frozen=True)
class HealthSettings:
    fail_runs_before_alert: int = 2


@dataclass(frozen=True)
class AiSettings:
    triage_model: str = "haiku"
    judge_model: str = "opus"
    max_photos: int = 8
    timeout_s: float = 240


@dataclass(frozen=True)
class Settings:
    search: SearchSettings = field(default_factory=SearchSettings)
    ai: AiSettings = field(default_factory=AiSettings)
    poll: PollSettings = field(default_factory=PollSettings)
    notify: NotifySettings = field(default_factory=NotifySettings)
    health: HealthSettings = field(default_factory=HealthSettings)
    report: ReportSettings = field(default_factory=ReportSettings)


_SECTIONS = {f.name: f.default_factory for f in fields(Settings)}


def _build(cls: type, name: str, data: Any):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TypeError(f"section {name!r} must be a mapping")
    known = {f.name for f in fields(cls)}
    for key in data:
        if key not in known:
            raise ValueError(f"unknown config key: {name}.{key}")
    kwargs = dict(data)
    if "detail_delay_s" in kwargs:
        kwargs["detail_delay_s"] = tuple(kwargs["detail_delay_s"])
    return cls(**kwargs)


_ENV_PREFIX = "CARHUNT__"


def _apply_env(raw: dict, env: Mapping[str, str]) -> None:
    """Merge CARHUNT__<SECTION>__<KEY>=<yaml value> overrides into the raw dict."""
    for name, value in env.items():
        if not name.upper().startswith(_ENV_PREFIX):
            continue
        parts = name.lower().split("__")
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise ValueError(f"unknown config key: {name}")
        _, section, key = parts
        current = raw.get(section)
        if current is None:
            current = raw[section] = {}
        if not isinstance(current, dict):
            raise TypeError(f"section {section!r} must be a mapping")
        current[key] = yaml.safe_load(value)


def load_settings(
    path: str | Path = "config.yaml", env: Mapping[str, str] | None = None
) -> Settings:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError("config root must be a mapping")
    _apply_env(raw, os.environ if env is None else env)
    for key in raw:
        if key not in _SECTIONS:
            raise ValueError(f"unknown config key: {key}")
    sections = {
        f.name: _build(f.default_factory, f.name, raw.get(f.name))
        for f in fields(Settings)
    }
    return Settings(**sections)


def load_env(path: str | Path = ".env") -> dict[str, str]:
    """Parse KEY=VALUE lines; set os.environ only for keys not already set."""
    result: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return result
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
        os.environ.setdefault(key, value)
    return result
