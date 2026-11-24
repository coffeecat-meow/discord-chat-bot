"""One validated source for runtime behavior, independent from persona prose."""

from __future__ import annotations

import os
import tomllib
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


NAP_STATUS = "小睡中 (＿ ＿)zZ"


@dataclass
class SleepState:
    """Runtime sleep chosen by Sayuki herself; persisted by the bot in the DB."""

    nap_until: datetime | None = None
    nap_status: str = ""
    # One night only: (local date the daily sleep ends on, adjusted wake time).
    wake_override: tuple[date, time] | None = None

    def to_json(self):
        return {
            "nap_until": self.nap_until.isoformat() if self.nap_until else None,
            "nap_status": self.nap_status,
            "wake_override": [
                self.wake_override[0].isoformat(),
                self.wake_override[1].strftime("%H:%M"),
            ]
            if self.wake_override
            else None,
        }

    def load(self, data):
        data = data or {}
        self.nap_until = (
            datetime.fromisoformat(data["nap_until"]) if data.get("nap_until") else None
        )
        self.nap_status = data.get("nap_status") or ""
        override = data.get("wake_override")
        self.wake_override = (
            (date.fromisoformat(override[0]), time.fromisoformat(override[1]))
            if override
            else None
        )


@dataclass(frozen=True)
class SleepPolicy:
    enabled: bool = True
    start: str = "00:30"
    end: str = "08:00"
    timezone: str = "Asia/Taipei"
    status: str = "悄悄去睡覺了 (＿ ＿)zZ"
    state: SleepState = field(default_factory=SleepState, compare=False, repr=False)

    def __post_init__(self):
        a, b = time.fromisoformat(self.start), time.fromisoformat(self.end)
        if a.tzinfo or b.tzinfo:
            raise ValueError(
                "sleep times must be local HH:MM values; set sleep.timezone separately"
            )
        if a == b:
            raise ValueError("sleep.start and sleep.end must differ")
        ZoneInfo(self.timezone)

    def napping(self, now: datetime) -> bool:
        return self.state.nap_until is not None and now < self.state.nap_until

    def window(self, end_date: date):
        """Local [start, end) of the daily sleep that ends on end_date."""
        start, end = time.fromisoformat(self.start), time.fromisoformat(self.end)
        override = self.state.wake_override
        if override and override[0] == end_date:
            end = override[1]
        start_date = end_date if start < time.fromisoformat(self.end) else end_date - timedelta(days=1)
        return datetime.combine(start_date, start), datetime.combine(end_date, end)

    def next_window(self, now: datetime):
        """(end_date, start, end) of the current or next daily sleep, local naive."""
        local = now.astimezone(ZoneInfo(self.timezone)).replace(tzinfo=None)
        for offset in range(3):
            end_date = local.date() + timedelta(days=offset)
            start, end = self.window(end_date)
            if end > local:
                return end_date, start, end
        raise AssertionError("unreachable")

    def daily_sleeping(self, now: datetime) -> bool:
        if not self.enabled:
            return False
        local = now.astimezone(ZoneInfo(self.timezone)).replace(tzinfo=None)
        _, start, end = self.next_window(now)
        return start <= local < end

    def sleeping(self, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("sleep clock must be timezone-aware")
        return self.napping(now) or self.daily_sleeping(now)

    def status_text(self, now: datetime) -> str:
        if self.napping(now) and not self.daily_sleeping(now):
            return self.state.nap_status or NAP_STATUS
        return self.status

    def describe(self, now: datetime) -> str:
        """Prompt note while asleep, so a mention knows how she was woken."""
        zone = ZoneInfo(self.timezone)
        if self.daily_sleeping(now):
            _, _, end = self.next_window(now)
            return f"你現在在睡覺（日常睡眠，預計{end:%H:%M}起床），是被叫醒來回話的。"
        if self.napping(now):
            until = self.state.nap_until.astimezone(zone)
            return f"你現在在小睡（預計{until:%H:%M}醒來），是被叫醒來回話的。"
        return ""


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
            "timeout_seconds",
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
    voice = sections.get("voice", {})
    for name in ("enabled", "recordings_enabled"):
        if type(voice.get(name, False)) is not bool:
            raise ValueError(f"voice.{name} must be a boolean")
    if voice.get("enabled", False):
        provider = voice.get("asr_provider", "openai-compatible")
        if provider not in {"openai-compatible", "nvidia", "elevenlabs"}:
            raise ValueError("voice.asr_provider must be openai-compatible, nvidia or elevenlabs")
        if not voice.get("asr_model") or not voice.get("asr_api_key_env"):
            raise ValueError("Enabled voice requires voice.asr_model and voice.asr_api_key_env")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", voice["asr_api_key_env"]):
            raise ValueError("voice.asr_api_key_env must be an environment variable name")
        provider_url = {
            "nvidia": "https://integrate.api.nvidia.com/v1",
            "elevenlabs": "https://api.elevenlabs.io",
            "openai-compatible": "https://api.openai.com/v1",
        }[provider]
        endpoint = urlsplit(voice.get("asr_base_url", provider_url))
        if not endpoint.hostname or endpoint.username or endpoint.password or not (
            endpoint.scheme == "https"
            or endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("voice.asr_base_url requires HTTPS, except loopback development")
        for name in ("bridge_port", "pcm_port"):
            value = voice.get(name, 8787 if name == "bridge_port" else 8788)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise ValueError(f"voice.{name} must be a TCP port")
        if voice.get("bridge_port", 8787) == voice.get("pcm_port", 8788):
            raise ValueError("voice.bridge_port and voice.pcm_port must differ")
        silence = voice.get("silence_seconds", 1.5)
        if isinstance(silence, bool) or not isinstance(silence, (int, float)) or not 0.25 <= silence <= 10:
            raise ValueError("voice.silence_seconds must be between 0.25 and 10")
    config = Config(root, sections, policy)
    from sayuki_bot.llm.routing import validate_routes

    validate_routes(config)
    config.admins, config.guilds
    config.path("persona"), config.path("database")
    return config
