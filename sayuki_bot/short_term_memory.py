from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
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


def _message_attachment_summary(message: discord.Message) -> str:
    parts = []
    images = sum(1 for item in message.attachments if item.content_type and item.content_type.startswith("image/"))
    videos = sum(1 for item in message.attachments if item.content_type and item.content_type.startswith("video/"))
    audios = sum(1 for item in message.attachments if item.content_type and item.content_type.startswith("audio/"))
    others = max(len(message.attachments) - images - videos - audios, 0)
    if images:
        parts.append(f"{images}張圖片")
    if videos:
        parts.append(f"{videos}個影片")
    if audios:
        parts.append(f"{audios}個音訊")
    if others:
        parts.append(f"{others}個其他附件")
    return "、".join(parts)


def _record_key(record: dict[str, Any]) -> str:
    record_id = record.get("record_id")
    if record_id:
        return str(record_id)
    return "|".join(
        str(record.get(key, ""))
        for key in ("kind", "time", "channel_id", "message_id", "user_id", "content", "user_message", "bot_response")
    )


class ShortTermMemoryManager:
    def __init__(
        self,
        db_file: str = "short_term_memory.json",
        pending_file: str | Path = "logs/short_memory_pending.jsonl",
        ttl_seconds: int = 21600,
        trigger_messages: int = 40,
        min_interval_seconds: int = 600,
        max_context_chars: int = 5000,
        event_logger=None,
    ):
        self.db_file = db_file
        self.pending_file = Path(pending_file)
        self.ttl_seconds = ttl_seconds
        self.trigger_messages = trigger_messages
        self.min_interval_seconds = min_interval_seconds
        self.max_context_chars = max_context_chars
        self.data: dict[str, Any] = {"channels": {}, "users": {}}
        self.lock = asyncio.Lock()
        self.event_logger = event_logger

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

        self._prune_expired_summaries()
        await self._save_db()
        await self.prune_pending()

    async def _save_db(self) -> None:
        async with self.lock:
            async with aiofiles.open(self.db_file, "w", encoding="utf-8") as file:
                await file.write(json.dumps(self.data, ensure_ascii=False, indent=2))

    def _is_expired(self, updated_at: str | None) -> bool:
        parsed = _parse_time(updated_at)
        if not parsed:
            return True
        return (_now() - parsed).total_seconds() > self.ttl_seconds

    def _prune_expired_summaries(self) -> None:
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

    async def _load_pending_records(self) -> tuple[list[dict[str, Any]], int]:
        if not self.pending_file.exists():
            return [], 0

        records = []
        expired = 0
        async with self.lock:
            async with aiofiles.open(self.pending_file, "r", encoding="utf-8") as file:
                async for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        expired += 1
                        continue

                    if self._is_expired(record.get("time")):
                        expired += 1
                        continue
                    records.append(record)

        return records, expired

    async def _cleanup_pending_records(self, processed_keys: set[str]) -> tuple[int, int, int]:
        if not self.pending_file.exists():
            return 0, 0, 0

        expired = 0
        removed = 0
        kept: list[dict[str, Any]] = []
        async with self.lock:
            async with aiofiles.open(self.pending_file, "r", encoding="utf-8") as file:
                async for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        expired += 1
                        continue
                    if self._is_expired(record.get("time")):
                        expired += 1
                        continue
                    if _record_key(record) in processed_keys:
                        removed += 1
                        continue
                    kept.append(record)

            async with aiofiles.open(self.pending_file, "w", encoding="utf-8") as file:
                for record in kept:
                    await file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

        return expired, removed, len(kept)

    async def prune_pending(self) -> int:
        expired, _, _ = await self._cleanup_pending_records(set())
        return expired

    async def _append_pending_record(self, record: dict[str, Any]) -> None:
        async with self.lock:
            self.pending_file.parent.mkdir(parents=True, exist_ok=True)
            async with aiofiles.open(self.pending_file, "a", encoding="utf-8") as file:
                await file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    async def record_channel_message(self, message: discord.Message, bot_user_id: int) -> None:
        content = message.clean_content.strip()
        attachments = _message_attachment_summary(message)
        if not content and not attachments:
            return

        record = {
            "record_id": str(uuid.uuid4()),
            "log_type": "short_memory_pending",
            "kind": "channel_message",
            "time": message.created_at.astimezone(TW_TZ).isoformat(timespec="seconds"),
            "recorded_at": _now_iso(),
            "guild_id": str(getattr(getattr(message, "guild", None), "id", "")),
            "channel_id": str(message.channel.id),
            "channel_name": getattr(message.channel, "name", str(message.channel.id)),
            "message_id": str(message.id),
            "user_id": str(message.author.id),
            "user_name": message.author.display_name,
            "is_bot_message": message.author.id == bot_user_id,
            "content": content,
            "attachments": attachments,
        }
        await self._append_pending_record(record)

    async def record_user_interaction(
        self,
        user_id: int | str,
        user_name: str,
        channel_id: int | str,
        channel_name: str,
        user_message: str,
        bot_response: str,
    ) -> None:
        if not user_message.strip() and not bot_response.strip():
            return

        record = {
            "record_id": str(uuid.uuid4()),
            "log_type": "short_memory_pending",
            "kind": "user_interaction",
            "time": _now_iso(),
            "user_id": str(user_id),
            "user_name": user_name,
            "channel_id": str(channel_id),
            "channel_name": channel_name,
            "user_message": user_message.strip(),
            "bot_response": bot_response.strip(),
        }
        await self._append_pending_record(record)

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

    def _can_update_summary(self, entry: dict[str, Any] | None) -> bool:
        if not entry:
            return True
        updated_at = _parse_time(entry.get("updated_at"))
        if not updated_at:
            return True
        return (_now() - updated_at).total_seconds() >= self.min_interval_seconds

    def _format_channel_records(self, records: list[dict[str, Any]]) -> str:
        lines = []
        for record in records:
            content = record.get("content", "").replace("\n", " ").strip()
            attachments = record.get("attachments", "")
            if attachments:
                content = f"{content} [附件：{attachments}]".strip()
            if not content:
                continue
            lines.append(
                f"[{record.get('time', '')}] "
                f"{record.get('user_name', '未知')} (ID:{record.get('user_id', '')}): "
                f"{content}"
            )
        return _trim("\n".join(lines), self.max_context_chars * 2)

    def _format_user_records(self, records: list[dict[str, Any]]) -> str:
        lines = []
        for record in records:
            lines.append(
                f"[{record.get('time', '')}] 在 #{record.get('channel_name', '')}\n"
                f"使用者：{record.get('user_message', '')}\n"
                f"bot：{record.get('bot_response', '')}"
            )
        return _trim("\n\n".join(lines), self.max_context_chars * 2)

    async def digest_pending(
        self,
        llm,
        channel_id: int | str,
        channel_name: str,
        user_id: int | str,
        user_name: str,
    ) -> dict[str, int]:
        records, expired = await self._load_pending_records()
        if not records and not expired:
            return {"expired": 0, "channel_summarized": 0, "user_summarized": 0, "kept": 0}

        channel_key = str(channel_id)
        user_key = str(user_id)
        channel_indices = [
            index
            for index, record in enumerate(records)
            if record.get("kind") == "channel_message" and str(record.get("channel_id")) == channel_key
        ]
        user_indices = [
            index
            for index, record in enumerate(records)
            if record.get("kind") == "user_interaction" and str(record.get("user_id")) == user_key
        ]

        processed_indices: set[int] = set()
        channel_summarized = 0
        user_summarized = 0

        channel_entry = self.data.get("channels", {}).get(channel_key)
        if (
            len(channel_indices) >= self.trigger_messages
            and self._can_update_summary(channel_entry)
        ):
            channel_records = [records[index] for index in channel_indices]
            previous = channel_entry.get("summary", "") if channel_entry else ""
            prompt = (
                f"頻道名稱：{channel_name}\n"
                f"舊摘要：{previous or '無'}\n\n"
                f"未消化訊息：\n{self._format_channel_records(channel_records)}\n\n"
                "請判斷這些未消化訊息是否仍有助於理解目前頻道脈絡。"
                "若大多只是寒暄、雜訊、已過時或對後續對話沒有幫助，請只輸出「無」。"
                "若有幫助，請壓縮成短期頻道摘要，保留目前話題、參與者、未解問題、情緒氛圍。"
                "不要加入未出現在訊息中的推測。請用繁體中文，最多6點，250字內。"
            )
            summary = await llm.summarize_async(
                prompt,
                max_tokens=360,
                call_type="short_memory_channel_digest",
            )
            if not summary.startswith("短期記憶壓縮失敗"):
                if summary.strip() and summary.strip() != "無":
                    self.data.setdefault("channels", {})[channel_key] = {
                        "summary": _trim(summary, self.max_context_chars),
                        "updated_at": _now_iso(),
                    }
                    channel_summarized = 1
                processed_indices.update(channel_indices)

        user_entry = self.data.get("users", {}).get(user_key)
        if user_indices and self._can_update_summary(user_entry):
            user_records = [records[index] for index in user_indices]
            previous = user_entry.get("summary", "") if user_entry else ""
            prompt = (
                f"使用者：{user_name} (ID:{user_key})\n"
                f"舊互動摘要：{previous or '無'}\n\n"
                f"未消化互動：\n{self._format_user_records(user_records)}\n\n"
                "請判斷這些互動是否仍有助於延續目前對話。"
                "若只是已結束的寒暄、短答、無後續價值，請只輸出「無」。"
                "若有幫助，請更新此使用者與bot的短期互動摘要，保留最近正在延續的話題、bot問過的問題、"
                "使用者可能期待bot接續的內容。不要寫永久個資。請用繁體中文，最多4點，180字內。"
            )
            summary = await llm.summarize_async(
                prompt,
                max_tokens=240,
                call_type="short_memory_user_digest",
            )
            if not summary.startswith("短期記憶壓縮失敗"):
                if summary.strip() and summary.strip() != "無":
                    self.data.setdefault("users", {})[user_key] = {
                        "summary": _trim(summary, self.max_context_chars),
                        "updated_at": _now_iso(),
                    }
                    user_summarized = 1
                processed_indices.update(user_indices)

        processed_keys = {_record_key(records[index]) for index in processed_indices}
        kept_count = len(records) - len(processed_indices)
        if expired or processed_indices:
            expired, _, kept_count = await self._cleanup_pending_records(processed_keys)
        if channel_summarized or user_summarized:
            await self._save_db()

        stats = {
            "expired": expired,
            "channel_summarized": channel_summarized,
            "user_summarized": user_summarized,
            "processed": len(processed_indices),
            "kept": kept_count,
        }
        if self.event_logger and (expired or processed_indices):
            await self.event_logger.write(
                {
                    "log_type": "short_memory",
                    "time": _now_iso(),
                    "channel_id": channel_key,
                    "channel_name": channel_name,
                    "user_id": user_key,
                    "user_name": user_name,
                    **stats,
                }
            )
        return stats
