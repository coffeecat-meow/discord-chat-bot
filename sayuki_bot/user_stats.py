from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from typing import Any

import aiofiles
import discord

from .config import TW_TZ


def _now_text() -> str:
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


class UserStatsManager:
    def __init__(self, db_file: str = "user_stats.json"):
        self.db_file = db_file
        self.data: dict[str, dict[str, Any]] = {}
        self.lock = asyncio.Lock()

    async def init_db(self) -> None:
        if os.path.exists(self.db_file):
            async with aiofiles.open(self.db_file, "r", encoding="utf-8") as file:
                content = await file.read()
                loaded = json.loads(content) if content else {}
                self.data = loaded if isinstance(loaded, dict) else {}

    async def _save_db(self) -> None:
        async with self.lock:
            async with aiofiles.open(self.db_file, "w", encoding="utf-8") as file:
                await file.write(json.dumps(self.data, ensure_ascii=False, indent=2))

    def _ensure_user(self, user_id: int | str, name: str | None = None) -> dict[str, Any]:
        uid = str(user_id)
        if uid not in self.data or not isinstance(self.data[uid], dict):
            self.data[uid] = {
                "user_id": uid,
                "name": name or "unknown",
                "messages_seen": 0,
                "bot_triggered": 0,
                "bot_replied": 0,
                "bot_no_response": 0,
                "replies_to_user_messages": 0,
                "last_seen_at": "",
                "last_triggered_at": "",
                "last_replied_at": "",
                "last_channel_id": "",
                "last_channel_name": "",
                "channels": {},
                "trigger_reasons": {},
            }

        entry = self.data[uid]
        entry.setdefault("channels", {})
        entry.setdefault("trigger_reasons", {})
        if name:
            entry["name"] = name
        return entry

    def _channel_info(self, message_or_interaction) -> tuple[str, str]:
        channel = getattr(message_or_interaction, "channel", None)
        if not channel:
            return "", ""
        return str(getattr(channel, "id", "")), getattr(channel, "name", str(getattr(channel, "id", "")))

    async def record_seen_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        entry = self._ensure_user(message.author.id, message.author.display_name)
        channel_id, channel_name = self._channel_info(message)
        entry["messages_seen"] += 1
        entry["last_seen_at"] = _now_text()
        entry["last_channel_id"] = channel_id
        entry["last_channel_name"] = channel_name

        if channel_id:
            channel_entry = entry["channels"].setdefault(
                channel_id,
                {"name": channel_name, "messages_seen": 0, "bot_triggered": 0, "bot_replied": 0},
            )
            channel_entry["name"] = channel_name
            channel_entry["messages_seen"] += 1

        await self._save_db()

    async def record_trigger(self, user_id: int | str, name: str, channel_id: int | str, channel_name: str, reason: str) -> None:
        entry = self._ensure_user(user_id, name)
        entry["bot_triggered"] += 1
        entry["last_triggered_at"] = _now_text()
        entry["last_channel_id"] = str(channel_id)
        entry["last_channel_name"] = channel_name
        entry["trigger_reasons"][reason] = int(entry["trigger_reasons"].get(reason, 0)) + 1

        channel_entry = entry["channels"].setdefault(
            str(channel_id),
            {"name": channel_name, "messages_seen": 0, "bot_triggered": 0, "bot_replied": 0},
        )
        channel_entry["name"] = channel_name
        channel_entry["bot_triggered"] += 1
        await self._save_db()

    async def record_bot_response(self, user_id: int | str, name: str, channel_id: int | str, channel_name: str, replied_to_user: bool) -> None:
        entry = self._ensure_user(user_id, name)
        entry["bot_replied"] += 1
        entry["last_replied_at"] = _now_text()
        if replied_to_user:
            entry["replies_to_user_messages"] += 1

        channel_entry = entry["channels"].setdefault(
            str(channel_id),
            {"name": channel_name, "messages_seen": 0, "bot_triggered": 0, "bot_replied": 0},
        )
        channel_entry["name"] = channel_name
        channel_entry["bot_replied"] += 1
        await self._save_db()

    async def record_no_response(self, user_id: int | str, name: str) -> None:
        entry = self._ensure_user(user_id, name)
        entry["bot_no_response"] += 1
        await self._save_db()

    def get_user_stats(self, user_id: int | str) -> dict[str, Any] | None:
        return self.data.get(str(user_id))

    def format_user_stats(self, user_id: int | str) -> str:
        entry = self.get_user_stats(user_id)
        if not entry:
            return f"沒有找到 user_id={user_id} 的統計資料。"

        channels = sorted(
            entry.get("channels", {}).values(),
            key=lambda item: int(item.get("bot_replied", 0)) + int(item.get("messages_seen", 0)),
            reverse=True,
        )[:5]
        channel_text = "\n".join(
            f"- {item.get('name')}: 訊息 {item.get('messages_seen', 0)}，觸發 {item.get('bot_triggered', 0)}，回覆 {item.get('bot_replied', 0)}"
            for item in channels
        ) or "無"

        reasons = entry.get("trigger_reasons", {})
        reason_text = "\n".join(f"- {key}: {value}" for key, value in sorted(reasons.items())) or "無"

        return (
            f"使用者：{entry.get('name')} (ID:{entry.get('user_id')})\n"
            f"看過訊息數：{entry.get('messages_seen', 0)}\n"
            f"觸發 bot 次數：{entry.get('bot_triggered', 0)}\n"
            f"bot 實際回覆次數：{entry.get('bot_replied', 0)}\n"
            f"bot 選擇不回覆次數：{entry.get('bot_no_response', 0)}\n"
            f"reply 該使用者訊息次數：{entry.get('replies_to_user_messages', 0)}\n"
            f"最後看見：{entry.get('last_seen_at') or '無'}\n"
            f"最後觸發：{entry.get('last_triggered_at') or '無'}\n"
            f"最後回覆：{entry.get('last_replied_at') or '無'}\n"
            f"最後頻道：{entry.get('last_channel_name') or '無'}\n\n"
            f"觸發原因統計：\n{reason_text}\n\n"
            f"常見頻道：\n{channel_text}"
        )
