from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import discord

from .config import TW_TZ


def _clean_inline(value: Any, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _status_text(status) -> str:
    name = getattr(status, "name", str(status or "unknown"))
    return {
        "online": "在線",
        "idle": "閒置",
        "dnd": "請勿打擾",
        "do_not_disturb": "請勿打擾",
        "offline": "離線",
        "invisible": "隱身/離線",
    }.get(name, name)


def _format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return ""

    total_seconds = max(int(seconds), 0)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _timestamp_start(activity) -> datetime | None:
    timestamps = getattr(activity, "timestamps", None)
    if isinstance(timestamps, dict):
        started_at = timestamps.get("start")
        if isinstance(started_at, datetime):
            return started_at

    started_at = getattr(activity, "start", None) or getattr(activity, "created_at", None)
    return started_at if isinstance(started_at, datetime) else None


def _elapsed_text(activity, now: datetime) -> str:
    started_at = _timestamp_start(activity)
    if not started_at:
        return ""

    elapsed = (now - started_at.astimezone(TW_TZ)).total_seconds()
    duration = _format_duration(elapsed)
    return f" {duration}" if duration else ""


def _activity_type_name(activity) -> str:
    activity_type = getattr(activity, "type", None)
    name = getattr(activity_type, "name", "")
    return str(name or "").lower()


def _format_activity(activity, now: datetime) -> str:
    name = _clean_inline(getattr(activity, "name", "") or "", 80)
    details = _clean_inline(getattr(activity, "details", "") or "", 120)
    state = _clean_inline(getattr(activity, "state", "") or "", 120)
    emoji = getattr(activity, "emoji", None)
    activity_kind = _activity_type_name(activity)
    elapsed = _elapsed_text(activity, now)

    if activity_kind == "custom":
        text = " ".join(part for part in [str(emoji or "").strip(), state or name] if part)
        return f"自訂狀態：{_clean_inline(text, 140)}" if text else ""

    if activity_kind == "listening":
        extra = " - ".join(part for part in [details, state] if part)
        if extra and extra != name:
            return f"正在聆聽 {name}（{extra}）{elapsed}".strip()
        return f"正在聆聽 {name}{elapsed}".strip()

    if activity_kind == "playing":
        extra = f"（{details}）" if details else ""
        return f"正在遊玩 {name}{extra}{elapsed}".strip()

    if activity_kind == "streaming":
        return f"正在直播 {name}{elapsed}".strip()

    if activity_kind == "watching":
        return f"正在觀看 {name}{elapsed}".strip()

    if activity_kind == "competing":
        return f"正在競賽 {name}{elapsed}".strip()

    extra = " / ".join(part for part in [details, state] if part)
    if extra:
        return f"{name}（{extra}）{elapsed}".strip()
    return f"{name}{elapsed}".strip()


@dataclass
class PresenceRecord:
    guild_id: int
    user_id: int
    display_name: str
    status: str
    activities: list[str] = field(default_factory=list)
    updated_at: datetime = field(default_factory=lambda: datetime.now(TW_TZ))

    def line(self, now: datetime, ttl_seconds: int) -> str:
        stale = (now - self.updated_at).total_seconds() > ttl_seconds
        stale_text = "（可能過期）" if stale else ""
        activities = "，".join(self.activities) if self.activities else "無活動"
        return (
            f"- {self.display_name} (ID:{self.user_id})："
            f"{self.status}{stale_text}，{activities}"
        )


class PresenceManager:
    def __init__(
        self,
        ttl_seconds: int = 21600,
        max_context_users: int = 8,
        max_age_seconds: int = 86400,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_context_users = max_context_users
        self.max_age_seconds = max_age_seconds
        self.records: dict[tuple[int, int], PresenceRecord] = {}

    def cleanup_expired(self, now: datetime | None = None) -> int:
        if self.max_age_seconds <= 0:
            return 0

        now = now or datetime.now(TW_TZ)
        expired_keys = [
            key
            for key, record in self.records.items()
            if (now - record.updated_at).total_seconds() > self.max_age_seconds
        ]
        for key in expired_keys:
            self.records.pop(key, None)
        return len(expired_keys)

    def update_member(self, member) -> None:
        guild = getattr(member, "guild", None)
        guild_id = getattr(guild, "id", None)
        user_id = getattr(member, "id", None)
        if not guild_id or not user_id:
            return

        now = datetime.now(TW_TZ)
        activities = []
        for activity in getattr(member, "activities", []) or []:
            text = _format_activity(activity, now)
            if text:
                activities.append(text)

        record = PresenceRecord(
            guild_id=int(guild_id),
            user_id=int(user_id),
            display_name=getattr(member, "display_name", str(user_id)),
            status=_status_text(getattr(member, "status", None)),
            activities=activities[:5],
            updated_at=now,
        )
        self.records[(record.guild_id, record.user_id)] = record

    def prime_from_guilds(self, guilds: list[discord.Guild]) -> None:
        for guild in guilds:
            for member in getattr(guild, "members", []) or []:
                if getattr(member, "bot", False):
                    continue
                self.update_member(member)

    def get_record(self, guild_id: int | str | None, user_id: int | str | None) -> PresenceRecord | None:
        if guild_id is None or user_id is None:
            return None

        try:
            return self.records.get((int(guild_id), int(user_id)))
        except (TypeError, ValueError):
            return None

    def format_user(self, guild_id: int | str | None, user_id: int | str | None) -> str:
        record = self.get_record(guild_id, user_id)
        if not record:
            return "快取中沒有這位使用者的Discord狀態。可能是對方離線、Presence Intent未啟用、或bot尚未收到狀態更新。"

        return record.line(datetime.now(TW_TZ), self.ttl_seconds)

    def build_context(
        self,
        guild_id: int | str | None,
        user_ids: set[str] | set[int],
    ) -> str:
        if guild_id is None:
            return "無"

        now = datetime.now(TW_TZ)
        self.cleanup_expired(now)
        lines = []
        for raw_user_id in list(user_ids)[: self.max_context_users]:
            record = self.get_record(guild_id, raw_user_id)
            if record:
                lines.append(record.line(now, self.ttl_seconds))

        if not lines:
            return "無"
        return "\n".join(lines)
