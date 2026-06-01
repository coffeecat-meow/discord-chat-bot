from __future__ import annotations

from pathlib import Path


def load_system_prompt(path: Path) -> str:
    content = path.read_text(encoding="utf-8")

    if content.startswith("\ufeff"):
        content = content[1:]

    if not content.startswith("\n"):
        content = "\n" + content
    if not content.endswith("\n"):
        content += "\n"

    return content
