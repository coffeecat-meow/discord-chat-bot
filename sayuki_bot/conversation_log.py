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
