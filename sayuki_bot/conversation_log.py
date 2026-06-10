from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class ConversationLogger:
    def __init__(self, path: Path):
        self.path = path

    async def write(self, event: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._write_sync, event)
        except Exception as exc:
            logger.error("寫入對話紀錄失敗: %s", exc)

    def _write_sync(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    async def read_recent(
        self,
        limit: int = 20,
        day: str | None = None,
        log_type: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return await asyncio.to_thread(self._read_recent_sync, limit, day, log_type)
        except Exception as exc:
            logger.error("讀取紀錄失敗: %s", exc)
            return []

    def _read_recent_sync(self, limit: int, day: str | None, log_type: str | None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []

        entries = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if day and not self._event_time(event).startswith(day):
                    continue
                if log_type and event.get("log_type") != log_type:
                    continue
                entries.append(event)

        return entries[-max(1, min(limit, 200)):]

    @staticmethod
    def _event_time(event: dict[str, Any]) -> str:
        for key in ("time", "started_at", "recorded_at", "finished_at"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
