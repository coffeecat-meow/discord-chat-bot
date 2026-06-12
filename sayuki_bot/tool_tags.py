from __future__ import annotations

from dataclasses import dataclass


MEMORY_TOOL_NAMES = (
    "MEM_SET",
    "MEM_HOBBY",
    "MEM_GOSSIP",
    "MEM_EVENT",
    "MEM_EVENT_FOR",
    "MEMORY",
    "EDIT_MEMORY",
    "DELETE_MEMORY",
    "PERMANENT_MEMORY",
    "EDIT_PERMANENT_MEMORY",
    "DELETE_PERMANENT_MEMORY",
    "SERVER_MEMORY",
    "DELETE_SERVER_MEMORY",
)


@dataclass(frozen=True)
class ToolTag:
    name: str
    body: str
    start: int
    end: int
    raw: str


def _find_tag_end(text: str, body_start: int) -> int | None:
    index = body_start
    while index < len(text) - 1:
        if text.startswith("[[", index):
            nested_end = text.find("]]", index + 2)
            if nested_end != -1:
                index = nested_end + 2
                continue

        if text.startswith("]]", index):
            return index + 2

        index += 1

    return None


def find_balanced_tool_tags(text: str, tag_names: tuple[str, ...] = MEMORY_TOOL_NAMES) -> list[ToolTag]:
    tags: list[ToolTag] = []
    index = 0
    prefixes = [(name, f"[[{name}:") for name in tag_names]

    while index < len(text):
        next_match: tuple[int, str, str] | None = None
        for name, prefix in prefixes:
            start = text.find(prefix, index)
            if start == -1:
                continue
            if next_match is None or start < next_match[0]:
                next_match = (start, name, prefix)

        if next_match is None:
            break

        start, name, prefix = next_match
        body_start = start + len(prefix)
        end = _find_tag_end(text, body_start)
        if end is None:
            index = body_start
            continue

        body = text[body_start:end - 2].strip()
        tags.append(ToolTag(name=name, body=body, start=start, end=end, raw=text[start:end]))
        index = end

    return tags


def strip_balanced_tool_tags(text: str, tag_names: tuple[str, ...] = MEMORY_TOOL_NAMES) -> str:
    tags = find_balanced_tool_tags(text, tag_names)
    for tag in reversed(tags):
        text = text[:tag.start] + text[tag.end:]
    return text
