"""One validated source for runtime behavior, independent from persona prose."""

from __future__ import annotations

import os
import tomllib
import math
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class SleepPolicy:
    enabled: bool = True
    start: str = "00:30"
    end: str = "08:00"
    timezone: str = "Asia/Taipei"
    status: str = "悄悄去睡覺了 (＿ ＿)zZ"

    def __post_init__(self):
        a, b = time.fromisoformat(self.start), time.fromisoformat(self.end)
        if a.tzinfo or b.tzinfo:
            raise ValueError(
                "sleep times must be local HH:MM values; set sleep.timezone separately"
            )
        if a == b:
            raise ValueError("sleep.start and sleep.end must differ")
        ZoneInfo(self.timezone)

    def sleeping(self, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("sleep clock must be timezone-aware")
        if not self.enabled:
            return False
        current = now.astimezone(ZoneInfo(self.timezone)).time().replace(tzinfo=None)
        start, end = time.fromisoformat(self.start), time.fromisoformat(self.end)
        return start <= current < end if start < end else current >= start or current < end


@dataclass(frozen=True)
class Config:
    root: Path
    sections: dict
    sleep: SleepPolicy

    def get(self, section: str, key: str, default=None):
        return self.sections.get(section, {}).get(key, default)

    def path(self, key: str) -> Path:
        value = Path(self.sections["paths"][key]).expanduser()
        return value if value.is_absolute() else self.root / value

    @property
    def admins(self) -> frozenset[int]:
        return frozenset(int(x) for x in self.get("discord", "admin_ids", []))

    @property
    def guilds(self) -> frozenset[int]:
        return frozenset(int(x) for x in self.get("discord", "trusted_guild_ids", []))


def load_config(path: str | Path | None = None) -> Config:
    source = (
        Path(path or os.getenv("SAYUKI_CONFIG", ROOT / "config/runtime.toml"))
        .expanduser()
        .resolve()
    )
    if not source.is_file():
        raise ValueError(f"Missing {source}; copy config/runtime.example.toml first")
    # Config in config/ resolves relative paths from project root; custom config
    # can explicitly set project_root to avoid CWD-dependent data locations.
    with source.open("rb") as handle:
        sections = tomllib.load(handle)
    root_value = sections.pop("project_root", None)
    default_root = source.parent.parent if source.parent.name == "config" else source.parent
    root = (source.parent / root_value).resolve() if root_value else default_root
    policy = SleepPolicy(**sections.get("sleep", {}))
    endpoint = urlsplit(sections.get("model", {}).get("base_url", "https://openrouter.ai/api/v1"))
    if (
        not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or (
            endpoint.scheme != "https"
            and not (
                endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise ValueError("model.base_url requires HTTPS, except loopback development")
    if type(sections.get("model", {}).get("attention_reasoning_enabled", False)) is not bool:
        raise ValueError("model.attention_reasoning_enabled must be a boolean")
    for group, keys in {
        "model": [
            "max_output_tokens",
            "memory_max_output_tokens",
            "attention_max_output_tokens",
            "task_max_output_tokens",
            "timeout_seconds",
        ],
        "tasks": ["max_tool_rounds", "timeout_seconds"],
        "conversation": [
            "history_limit",
            "continuation_window_seconds",
            "cross_channel_window_seconds",
            "continuation_timeout_seconds",
            "history_chars",
            "reply_source_chars",
            "reply_fetch_timeout_seconds",
            "tool_merge_chars",
            "tool_merge_messages",
            "max_queue",
            "max_tool_rounds",
            "max_tools_per_round",
        ],
        "memory": [
            "batch_messages",
            "min_interval_seconds",
            "summary_chars",
            "profile_chars",
            "self_chars",
            "self_limit",
            "short_ttl_hours",
            "background_daily_calls",
        ],
        "tools": ["max_result_chars"],
        "storage": ["diagnostic_retention_days"],
    }.items():
        for key in keys:
            value = sections.get(group, {}).get(key)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{group}.{key} must be positive")
            if (
                value is not None
                and key
                not in {
                    "timeout_seconds",
                    "continuation_timeout_seconds",
                    "reply_fetch_timeout_seconds",
                    "min_interval_seconds",
                    "short_ttl_hours",
                }
                and not isinstance(value, int)
            ):
                raise ValueError(f"{group}.{key} must be an integer")
    for key, minimum, maximum in (("self_chars", 400, 20000), ("self_limit", 1, 100)):
        value = sections.get("memory", {}).get(key)
        if value is not None and not minimum <= value <= maximum:
            raise ValueError(f"memory.{key} must be {minimum}–{maximum}")
    delivery = sections.get("delivery", {})
    for name, value in delivery.items():
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"delivery.{name} must be non-negative")
    for prefix in ["wait_typing", "wait_pause"]:
        if delivery.get(prefix + "_min", 0) > delivery.get(prefix + "_max", float("inf")):
            raise ValueError(f"delivery.{prefix}_min exceeds max")
    config = Config(root, sections, policy)
    from sayuki_bot.llm.routing import validate_routes

    validate_routes(config)
    config.admins, config.guilds
    config.path("persona"), config.path("database")
    return config
