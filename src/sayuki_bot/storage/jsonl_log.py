"""Append-only JSONL logs matching 4.0's conversation/invocation records.

Disabled until configure() is called from the real entry point, so tests,
simulators and evaluation runs never write into the deployment directory.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_directory: Path | None = None
_zone = ZoneInfo("Asia/Taipei")
_max_bytes = 50 * 1024 * 1024


def configure(directory, *, tz="Asia/Taipei", max_mb=50):
    global _directory, _zone, _max_bytes
    _directory = Path(directory)
    _directory.mkdir(parents=True, exist_ok=True)
    _zone = ZoneInfo(tz)
    _max_bytes = int(max_mb * 1024 * 1024)


def now():
    return datetime.now(timezone.utc).astimezone(_zone).isoformat(timespec="seconds")


def local(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(_zone).isoformat(timespec="seconds")


def write(name, record):
    """name is the file stem: conversation, invocation or memory."""
    if _directory is None:
        return
    try:
        path = _directory / f"{name}.jsonl"
        # Keep one previous generation; 4.0 never rotated and grew unbounded.
        if path.exists() and path.stat().st_size > _max_bytes:
            path.replace(path.with_suffix(".jsonl.1"))
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
        with path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except Exception as exc:
        logger.error("寫入%s紀錄失敗: %s", name, exc)


def content_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if not isinstance(part, dict):
                pieces.append(str(part))
            elif part.get("type") == "text":
                pieces.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                pieces.append("[圖片]")
            else:
                pieces.append(f"[{part.get('type', '?')}]")
        return "\n".join(pieces)
    return "" if content is None else str(content)


def messages_for_log(messages):
    """The leading system prompt (persona, or memory's fixed instructions) is
    omitted as in 4.0; later per-turn system instructions are kept for debugging."""
    sanitized, input_chars, system_chars = [], 0, 0
    for index, message in enumerate(messages):
        role = message.get("role", "")
        text = content_text(message.get("content", ""))
        if role == "system" and index == 0:
            system_chars += len(text)
            sanitized.append({"role": role, "system_prompt_omitted": True, "chars": len(text)})
        else:
            input_chars += len(text)
            sanitized.append({"role": role, "content": text, "chars": len(text)})
    return sanitized, input_chars, system_chars
