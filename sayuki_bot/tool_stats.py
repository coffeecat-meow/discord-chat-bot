from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
TW_TZ = timezone(timedelta(hours=8))


def _empty_bucket() -> dict[str, Any]:
    return {"calls": 0, "failures": 0, "duration_ms_total": 0.0}


def _add_bucket(bucket: dict[str, Any], duration_ms: float, success: bool) -> None:
    bucket["calls"] = int(bucket.get("calls", 0)) + 1
    bucket["failures"] = int(bucket.get("failures", 0)) + (0 if success else 1)
    bucket["duration_ms_total"] = float(bucket.get("duration_ms_total", 0.0)) + max(duration_ms, 0.0)


def _format_bucket(bucket: dict[str, Any]) -> str:
    calls = int(bucket.get("calls", 0))
    failures = int(bucket.get("failures", 0))
    total_ms = float(bucket.get("duration_ms_total", 0.0))
    avg_ms = int(total_ms / calls) if calls else 0
    return f"{calls}次/{failures}失敗/{avg_ms}ms"


class ToolStatsManager:
    def __init__(self, db_file: Path):
        self.db_file = db_file
        self.data: dict[str, Any] = {
            "bot_messages": {"daily": {}, "monthly": {}, "total": 0},
            "tools": {},
        }
        self.lock = asyncio.Lock()

    async def init_db(self) -> None:
        if not self.db_file.exists():
            return

        try:
            content = await asyncio.to_thread(self.db_file.read_text, encoding="utf-8")
            loaded = json.loads(content) if content else {}
            if isinstance(loaded, dict):
                self.data.update(loaded)
                self.data.setdefault("bot_messages", {"daily": {}, "monthly": {}, "total": 0})
                self.data.setdefault("tools", {})
        except Exception as exc:
            logger.error("讀取工具統計失敗: %s", exc)

    async def _save_locked(self) -> None:
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(self.data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(self.db_file.write_text, content, encoding="utf-8")

    def _period_keys(self) -> tuple[str, str]:
        now = datetime.now(TW_TZ)
        return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")

    def _ensure_tool(self, tool_name: str) -> dict[str, Any]:
        tools = self.data.setdefault("tools", {})
        entry = tools.setdefault(
            tool_name,
            {"daily": {}, "monthly": {}, "total": _empty_bucket()},
        )
        entry.setdefault("daily", {})
        entry.setdefault("monthly", {})
        entry.setdefault("total", _empty_bucket())
        return entry

    async def record_bot_message(self) -> None:
        try:
            async with self.lock:
                today, month = self._period_keys()
                bot_messages = self.data.setdefault("bot_messages", {"daily": {}, "monthly": {}, "total": 0})
                bot_messages.setdefault("daily", {})
                bot_messages.setdefault("monthly", {})
                bot_messages["daily"][today] = int(bot_messages["daily"].get(today, 0)) + 1
                bot_messages["monthly"][month] = int(bot_messages["monthly"].get(month, 0)) + 1
                bot_messages["total"] = int(bot_messages.get("total", 0)) + 1
                await self._save_locked()
        except Exception as exc:
            logger.error("寫入bot回覆統計失敗: %s", exc)

    async def record_tool(self, tool_name: str, duration_ms: float = 0.0, success: bool = True) -> None:
        try:
            async with self.lock:
                today, month = self._period_keys()
                entry = self._ensure_tool(tool_name)
                daily = entry["daily"].setdefault(today, _empty_bucket())
                monthly = entry["monthly"].setdefault(month, _empty_bucket())
                _add_bucket(daily, duration_ms, success)
                _add_bucket(monthly, duration_ms, success)
                _add_bucket(entry["total"], duration_ms, success)
                await self._save_locked()
        except Exception as exc:
            logger.error("寫入工具統計失敗: %s", exc)

    def get_bot_message_counts(self) -> dict[str, int]:
        today, month = self._period_keys()
        bot_messages = self.data.get("bot_messages", {})
        return {
            "today": int(bot_messages.get("daily", {}).get(today, 0)),
            "month": int(bot_messages.get("monthly", {}).get(month, 0)),
            "total": int(bot_messages.get("total", 0)),
        }

    def format_tool_stats(self, limit: int = 16) -> str:
        today, month = self._period_keys()
        tools = self.data.get("tools", {})
        if not tools:
            return "目前沒有工具統計。"

        def sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, int, str]:
            name, entry = item
            priority = {"SEARCH": 0, "VIEW_IMAGE": 1, "VIEW_IMAGE_VL": 2}.get(name, 10)
            calls = int(entry.get("total", {}).get("calls", 0))
            return priority, -calls, name

        lines = [
            "格式：工具 今日(calls/失敗/平均) | 本月 | 永久",
        ]
        for name, entry in sorted(tools.items(), key=sort_key)[:limit]:
            daily_bucket = entry.get("daily", {}).get(today, _empty_bucket())
            monthly_bucket = entry.get("monthly", {}).get(month, _empty_bucket())
            total_bucket = entry.get("total", _empty_bucket())
            lines.append(
                f"{name}: 今日 {_format_bucket(daily_bucket)} | "
                f"本月 {_format_bucket(monthly_bucket)} | 永久 {_format_bucket(total_bucket)}"
            )

        if len(tools) > limit:
            lines.append(f"...另有 {len(tools) - limit} 種工具未顯示")

        return "\n".join(lines)
