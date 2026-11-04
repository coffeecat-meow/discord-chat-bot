"""Bounded discovery within the existing Discord disclosure scope."""

from __future__ import annotations

import json
import re
import discord


def _size(value):
    return len(json.dumps(value, ensure_ascii=False))


def _message(message, bot_id):
    text = message.content or ""
    attachments = getattr(message, "attachments", [])
    from sayuki_bot.discord.stickers import records

    row = {
        "stickers": records(message),
        "id": str(message.id),
        "author_id": str(message.author.id),
        "author": message.author.display_name[:100],
        "self": message.author.id == bot_id,
        "text": text[:2000],
        "time": message.created_at.isoformat(),
        "reply_to": (
            f"msg_{message.reference.message_id}"
            if message.reference and message.reference.message_id
            else None
        ),
        "url": message.jump_url,
        "attachments": [
            {
                "name": x.filename[:200],
                "url": str(x.url)[:1000],
                "content_type": (x.content_type or "")[:100],
            }
            for x in attachments[:3]
        ],
        "truncated": len(text) > 2000
        or len(attachments) > 3
        or any(
            len(x.filename) > 200 or len(str(x.url)) > 1000 or len(x.content_type or "") > 100
            for x in attachments[:3]
        ),
    }
    return row


async def read_channel(executor, turn, args):
    channel_id = int(args["channel_id"])

    def check_access():
        if turn.cancelled:
            raise ValueError("Turn was cancelled")
        executor.access.channel(turn, turn.event.channel_id)
        return executor.access.channel(turn, channel_id)

    target = check_access()
    if not hasattr(target, "history"):
        raise ValueError(
            "This channel has no message history; select a readable text channel or thread"
        )
    limit = int(args["limit"])
    cursor = args["before_message_id"]
    kwargs = {"limit": limit + 1, "oldest_first": False}
    if cursor:
        kwargs["before"] = discord.Object(id=int(cursor))
    page = {
        "channel_id": str(channel_id),
        "order": "newest_first",
        "messages": [],
        "has_more": False,
        "next_before_message_id": None,
        "availability": "live_verified",
        "attachments_are_not_image_descriptions": True,
    }
    # Reserve the cursor envelope even before knowing whether a lookahead exists.
    async for message in target.history(**kwargs):
        check_access()
        if len(page["messages"]) == limit:
            page["has_more"] = True
            break
        row = _message(message, executor.bot.user.id)
        trial = {
            **page,
            "messages": [*page["messages"], row],
            "has_more": True,
            "next_before_message_id": str(message.id),
        }
        if _size(trial) > 6000:
            if page["messages"]:
                page["has_more"] = True
                break
            # Even a single attachment-heavy record must fit without breaking JSON.
            row["attachments"] = []
            row["stickers"] = []
            row["truncated"] = True
            while _size(trial) > 6000 and row["text"]:
                row["text"] = row["text"][: len(row["text"]) // 2]
        page["messages"].append(row)
    check_access()
    if page["has_more"]:
        page["next_before_message_id"] = page["messages"][-1]["id"]
    return page


def _loose(text):
    """Ignore separators so "mike watch" finds "mike_watching"."""
    return re.sub(r"[\s_\-]+", "", text)


def _rank(query, row):
    name = row["name"].casefold()
    if not query or query == row.get("id") or query == name or _loose(query) == _loose(name):
        return 0
    if name.startswith(query) or _loose(name).startswith(_loose(query)):
        return 1
    if (
        query in name
        or _loose(query) in _loose(name)
        or query in row.get("topic", "").casefold()
    ):
        return 2
    return 3


def channels(executor, turn, args):
    query = args["query"].strip("# ").casefold()
    types = set(filter(None, args["types"].split(",")))
    rows = []
    for identifier in executor.access.allowed_channels(turn):
        channel = executor.access.channel(turn, identifier)
        kind = str(getattr(channel, "type", "text"))
        if "thread" in kind:
            kind = "thread"
        if kind == "news":
            kind = "text"
        if kind == "stage_voice":
            kind = "stage"
        row = {
            "id": str(channel.id),
            "name": channel.name,
            "type": kind,
            "topic": getattr(channel, "topic", None) or "",
            "category": getattr(getattr(channel, "category", None), "name", None),
        }
        rank = _rank(query, row)
        if rank < 3 and (not types or kind in types):
            row["topic_truncated"] = len(row["topic"]) > 180
            row["topic"] = row["topic"][:180]
            rows.append((rank, getattr(channel, "position", 0), channel.id, row))
    return [row for _, _, _, row in sorted(rows)[: int(args["limit"])]]


def assets(executor, turn, args):
    guild = executor.bot.get_guild(turn.event.guild_id)
    query = args["query"].strip(":/ ").casefold()
    types = set(filter(None, args["types"].split(",")))
    rows = []
    for kind, items in (("emoji", guild.emojis), ("sticker", guild.stickers)):
        for item in items:
            if not getattr(item, "available", True):
                continue
            row = {"type": kind, "id": str(item.id), "name": item.name}
            if kind == "emoji":
                row["text"] = str(item)
            if kind == "sticker":
                row["format"] = getattr(getattr(item, "format", None), "name", "unknown")
            rows.append(row)

    def walk(commands):
        for command in commands:
            yield command
            yield from walk(getattr(command, "commands", []))

    roots = {}
    for scope in ({}, {"guild": guild}):
        for command in executor.bot.tree.get_commands(**scope):
            roots[(command.name, getattr(command, "type", None))] = command
    for command in walk(roots.values()):
        rows.append(
            {
                "type": "command",
                "name": getattr(command, "qualified_name", command.name),
                "description": getattr(command, "description", "")[:200],
            }
        )
    # Rank exact matches first; cycle categories within each rank so large emoji
    # collections cannot hide all command/sticker matches in an unfiltered search.
    selected = []
    for rank in range(3):
        buckets = [
            [row for row in rows if row["type"] == kind and _rank(query, row) == rank]
            for kind in ("emoji", "sticker", "command")
            if not types or kind in types
        ]
        while any(buckets):
            for bucket in buckets:
                if bucket:
                    selected.append(bucket.pop(0))
                    if len(selected) == int(args["limit"]):
                        return selected
    return selected


async def run(executor, turn, name, args):
    if name == "READ_CHANNEL":
        return await read_channel(executor, turn, args)
    if name == "CHANNEL_LOOKUP":
        return channels(executor, turn, args)
    return assets(executor, turn, args)
