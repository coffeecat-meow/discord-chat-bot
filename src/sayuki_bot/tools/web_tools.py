from __future__ import annotations


async def run(executor, turn, name, a):
    if name in {"SEARCH", "READ_WEB"}:
        from .search import search_web, read_web_page, OBSCURA_BINARY
        from dataclasses import asdict
        from pathlib import Path

        raw = executor.config.get("tools", "obscura_bin", "obscura")
        binary = Path(raw).expanduser()
        if not binary.is_absolute() and (len(binary.parts) > 1 or raw.startswith(".")):
            binary = executor.config.root / binary
        token = OBSCURA_BINARY.set(str(binary))
        try:
            result = await (search_web(a["query"]) if name == "SEARCH" else read_web_page(a["url"]))
            return asdict(result)
        finally:
            OBSCURA_BINARY.reset(token)
    raise ValueError(f"Unsupported tool: {name}")
