"""Parse balanced control tags, including nested SPLIT in action bodies."""

from dataclasses import dataclass
import re

from .registry import REGISTRY


@dataclass(frozen=True)
class Part:
    kind: str
    text: str
    name: str = ""


def parse(text: str) -> list[Part]:
    text = re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.S | re.I)
    # Provider-native tool markup is not conversational text or our tool protocol.
    if re.search(r"<\s*/?\s*[|｜]*\s*DSML\b", text, flags=re.I):
        return [
            Part(
                "error",
                "DSML is unsupported; use the documented [[TOOL: arguments]] format, not XML/DSML",
            )
        ]
    parts, cursor = [], 0
    while cursor < len(text):
        start = text.find("[[", cursor)
        if start == -1:
            parts.append(Part("text", text[cursor:]))
            break
        if start > cursor:
            parts.append(Part("text", text[cursor:start]))
        pos, depth = start + 2, 1
        while pos < len(text) and depth:
            if text.startswith("[[", pos):
                depth += 1
                pos += 2
            elif text.startswith("]]", pos):
                depth -= 1
                pos += 2
            else:
                pos += 1
        if depth:
            parts.append(Part("error", "unterminated control tag"))
            break
        body = text[start + 2 : pos - 2]
        name, _, arguments = body.partition(":")
        name = name.strip()
        if name in {"SPLIT", "SPLIT-WAIT"}:
            parts.append(Part("split", name))
        elif name == "$NO_NEED_TO_ANSWER$":
            parts.append(Part("silence", ""))
        elif name in {"PING", "REPLY_TO"}:
            value = re.sub(r"^(?:#?msg_)", "", arguments.strip())
            if value.isdigit():
                parts.append(Part("mention" if name == "PING" else "reply", value, name))
            else:
                parts.append(Part("error", f"Invalid {name}"))
        elif name in REGISTRY:
            parts.append(Part("tool", arguments.strip(), name))
        else:
            parts.append(Part("error", f"Unknown control: {name[:80]}"))
        cursor = pos
    return parts


def visible_segments(parts):
    buffer, reply, delay = "", None, None
    for part in parts:
        if part.kind == "text":
            buffer += part.text
        elif part.kind == "mention":
            buffer += f"<@{part.text}>"
        elif part.kind == "reply":
            reply = int(part.text)
        elif part.kind == "split":
            if buffer.strip():
                yield buffer.strip(), reply, delay
            buffer, reply, delay = "", None, part.text
    if buffer.strip():
        yield buffer.strip(), reply, delay


def auxiliary_parts(text):
    """The same content contract for preflight and actual auxiliary delivery."""
    parts = parse(text)
    if any(p.kind in {"tool", "reply", "error"} for p in parts):
        raise ValueError("Auxiliary message supports only text, split and mentions")
    if not any(value.strip() for value, _, _ in visible_segments(parts)):
        raise ValueError("Auxiliary message must contain visible text")
    return parts
