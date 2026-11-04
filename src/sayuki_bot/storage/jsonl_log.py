"""Append-only JSONL logs matching 4.0's conversation/invocation records.

Disabled until configure() is called from the real entry point, so tests,
simulators and evaluation runs never write into the deployment directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_directory: Path | None = None
_zone = ZoneInfo("Asia/Taipei")
_max_bytes = 50 * 1024 * 1024
# Hashes already stored in system_prompts.jsonl; each distinct prompt is kept once.
_known_prompts: set[str] = set()


def configure(directory, *, tz="Asia/Taipei", max_mb=50):
    global _directory, _zone, _max_bytes
    _directory = Path(directory)
    _directory.mkdir(parents=True, exist_ok=True)
    _zone = ZoneInfo(tz)
    _max_bytes = int(max_mb * 1024 * 1024)
    _known_prompts.clear()
    for path in (_directory / "system_prompts.jsonl.1", _directory / "system_prompts.jsonl"):
        try:
            with path.open(encoding="utf-8") as file:
                for line in file:
                    try:
                        _known_prompts.add(json.loads(line)["sha256"])
                    except (ValueError, KeyError, TypeError):
                        continue
        except FileNotFoundError:
            continue


def _remember_prompt(text):
    """Store a leading system prompt once; invocation records cite its hash."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if _directory is not None and digest not in _known_prompts:
        write(
            "system_prompts",
            {"time": now(), "sha256": digest, "chars": len(text), "content": text},
        )
        _known_prompts.add(digest)
    return digest


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
    """The leading system prompt (persona + tool help, or memory's fixed
    instructions) is replaced by its sha256; the full text is stored once in
    system_prompts.jsonl. Later per-turn system instructions are kept inline."""
    sanitized, input_chars, system_chars = [], 0, 0
    for index, message in enumerate(messages):
        role = message.get("role", "")
        text = content_text(message.get("content", ""))
        if role == "system" and index == 0:
            system_chars += len(text)
            sanitized.append(
                {
                    "role": role,
                    "system_prompt_omitted": True,
                    "chars": len(text),
                    "sha256": _remember_prompt(text),
                }
            )
        else:
            input_chars += len(text)
            sanitized.append({"role": role, "content": text, "chars": len(text)})
    return sanitized, input_chars, system_chars
