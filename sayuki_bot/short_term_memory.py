from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Any

import aiofiles
import discord

from .config import TW_TZ


def _now() -> datetime:
    return datetime.now(TW_TZ)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TW_TZ)


def _trim(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...（內容過長，已截斷）"


class ShortTermMemoryManager:
    def __init__(
        self,
        db_file: str = "short_term_memory.json",
        ttl_seconds: int = 21600,
        trigger_messages: int = 40,
        min_interval_seconds: int = 600,
        max_context_chars: int = 5000,
    ):
        self.db_file = db_file
        self.ttl_seconds = ttl_seconds
        self.trigger_messages = trigger_messages
        self.min_interval_seconds = min_interval_seconds
        self.max_context_chars = max_context_chars
        self.data: dict[str, Any] = {"channels": {}, "users": {}}
        self.lock = asyncio.Lock()
        self.channel_tasks: dict[str, asyncio.Task] = {}
        self.user_tasks: dict[str, asyncio.Task] = {}

    async def init_db(self) -> None:
        if os.path.exists(self.db_file):
            async with aiofiles.open(self.db_file, "r", encoding="utf-8") as file:
                content = await file.read()
                loaded = json.loads(content) if content else {}
                if isinstance(loaded, dict):
                    self.data = {
                        "channels": loaded.get("channels", {}),
                        "users": loaded.get("users", {}),
                    }

        self._prune_expired()
        await self._save_db()

    async def _save_db(self) -> None:
        async with self.lock:
            async with aiofiles.open(self.db_file, "w", encoding="utf-8") as file:
                await file.write(json.dumps(self.data, ensure_ascii=False, indent=2))

    def _is_expired(self, updated_at: str | None) -> bool:
        parsed = _parse_time(updated_at)
        if not parsed:
            return True
        return (_now() - parsed).total_seconds() > self.ttl_seconds

    def _prune_expired(self) -> None:
        self.data["channels"] = {
            key: value
            for key, value in self.data.get("channels", {}).items()
            if not self._is_expired(value.get("updated_at"))
        }
        self.data["users"] = {
            key: value
            for key, value in self.data.get("users", {}).items()
            if not self._is_expired(value.get("updated_at"))
        }

    def get_channel_context(self, channel_id: int | str) -> str:
        entry = self.data.get("channels", {}).get(str(channel_id))
        if not entry or self._is_expired(entry.get("updated_at")):
            return "無"

        return _trim(entry.get("summary", "無"), self.max_context_chars)

    def get_user_context(self, user_id: int | str) -> str:
        entry = self.data.get("users", {}).get(str(user_id))
        if not entry or self._is_expired(entry.get("updated_at")):
            return "無"

        return _trim(entry.get("summary", "無"), self.max_context_chars)

    def build_context(self, channel_id: int | str, user_id: int | str) -> str:
        channel_context = self.get_channel_context(channel_id)
        user_context = self.get_user_context(user_id)
        return (
            "[目前頻道短期摘要]\n"
            f"{channel_context}\n\n"
            "[與目前對話者近期互動摘要]\n"
            f"{user_context}\n\n"
            "提醒：短期摘要只用來理解背景。跨頻道互動摘要不要主動提起，除非使用者明顯延續相關話題。"
        )

    def record_channel_message(self, channel_id: int | str) -> int:
        key = str(channel_id)
        entry = self.data.setdefault("channels", {}).setdefault(
            key,
            {"summary": "", "updated_at": "", "message_count_since": 0},
        )
        entry["message_count_since"] = int(entry.get("message_count_since", 0)) + 1
        return entry["message_count_since"]

    def should_summarize_channel(self, channel_id: int | str) -> bool:
        entry = self.data.get("channels", {}).get(str(channel_id), {})
        if int(entry.get("message_count_since", 0)) < self.trigger_messages:
            return False

        updated_at = _parse_time(entry.get("updated_at"))
        if not updated_at:
            return True

        return (_now() - updated_at).total_seconds() >= self.min_interval_seconds

    def _format_messages(self, messages: list[discord.Message], bot_user_id: int) -> str:
        lines = []
        for msg in messages:
            sender = "你" if msg.author.id == bot_user_id else f"{msg.author.display_name} (ID:{msg.author.id})"
            content = msg.clean_content.replace("\n", " ").strip()
            if not content:
                continue

            tw_time = msg.created_at.astimezone(TW_TZ)
            lines.append(f"[{tw_time.strftime('%H:%M')}] {sender}: {content[:300]}")

        return _trim("\n".join(lines), self.max_context_chars * 2)

    def schedule_channel_summary(
        self,
        llm,
        channel_id: int | str,
        channel_name: str,
        messages: list[discord.Message],
        bot_user_id: int,
    ) -> None:
        key = str(channel_id)
        if key in self.channel_tasks and not self.channel_tasks[key].done():
            return

        self.channel_tasks[key] = asyncio.create_task(
            self._summarize_channel(llm, key, channel_name, messages, bot_user_id)
        )

    async def _summarize_channel(
        self,
        llm,
        channel_id: str,
        channel_name: str,
        messages: list[discord.Message],
        bot_user_id: int,
    ) -> None:
        previous = self.data.get("channels", {}).get(channel_id, {}).get("summary", "")
        transcript = self._format_messages(messages, bot_user_id)
        prompt = (
            f"頻道名稱：{channel_name}\n"
            f"舊摘要：{previous or '無'}\n\n"
            f"近期訊息：\n{transcript}\n\n"
            "請壓縮成短期頻道摘要，保留目前話題、參與者、未解問題、情緒氛圍。"
            "不要加入未出現在訊息中的推測。請用繁體中文，500字內。"
        )
        summary = await llm.summarize_async(prompt, max_tokens=900)
        self.data.setdefault("channels", {})[channel_id] = {
            "summary": _trim(summary, self.max_context_chars),
            "updated_at": _now_iso(),
            "message_count_since": 0,
        }
        await self._save_db()

    def schedule_user_interaction_summary(
        self,
        llm,
        user_id: int | str,
        user_name: str,
        interaction_context: str,
        bot_response: str,
    ) -> None:
        key = str(user_id)
        if key in self.user_tasks and not self.user_tasks[key].done():
            return

        self.user_tasks[key] = asyncio.create_task(
            self._summarize_user_interaction(llm, key, user_name, interaction_context, bot_response)
        )

    async def _summarize_user_interaction(
        self,
        llm,
        user_id: str,
        user_name: str,
        interaction_context: str,
        bot_response: str,
    ) -> None:
        previous = self.data.get("users", {}).get(user_id, {}).get("summary", "")
        prompt = (
            f"使用者：{user_name} (ID:{user_id})\n"
            f"舊互動摘要：{previous or '無'}\n\n"
            f"本次互動前後文：\n{_trim(interaction_context, self.max_context_chars)}\n\n"
            f"bot回覆：\n{_trim(bot_response, self.max_context_chars)}\n\n"
            "請更新此使用者與bot的短期互動摘要。保留最近正在延續的話題、bot問過的問題、"
            "使用者可能期待bot接續的內容。不要寫永久個資。請用繁體中文，400字內。"
        )
        summary = await llm.summarize_async(prompt, max_tokens=700)
        self.data.setdefault("users", {})[user_id] = {
            "summary": _trim(summary, self.max_context_chars),
            "updated_at": _now_iso(),
        }
        await self._save_db()
