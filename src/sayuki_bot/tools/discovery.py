from __future__ import annotations


from .registry import tool_help


async def run(executor, turn, name, a):
    if name == "TOOL_HELP":
        selected = tool_help(a["工具名稱或分類"])
        turn.loaded_tools.update(t.name for t in selected)
        return [t.help() for t in selected] or "找不到工具"
    raise ValueError(f"Unsupported tool: {name}")
