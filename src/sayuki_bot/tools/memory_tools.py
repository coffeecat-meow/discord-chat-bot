from __future__ import annotations

import hashlib
import re


def public_memories(rows):
    """Retrieval hints are internal ranking data, never evidence for the model."""
    return [{k: v for k, v in row.items() if not k.startswith("_")} for row in rows]


def confirmation_sources(turn):
    """A reply confirmation depends on both the answer and the quoted message."""
    if not turn.event.reply_to:
        return []
    source = next(
        (
            e
            for e in [*turn.history, *turn.supplemental, turn.reply_source]
            if e is not None and e.id == turn.event.reply_to
        ),
        None,
    )
    if source is None:
        raise ValueError("Read the reply source before storing a profile based on this reply")
    return [source]


async def specified_source(executor, turn, name, arguments):
    """Read the selected statement and, for a profile reply, its own parent."""
    from sayuki_bot.discord.events import event_from_message

    def recheck(sources):
        executor.access.authorize(turn, name, arguments)
        executor.access.member(turn, arguments["user_id"])
        for source in sources:
            if source.guild_id != turn.event.guild_id:
                raise PermissionError("Memory source is outside this guild")
            executor.access.channel(turn, source.channel_id)
        if turn.cancelled:
            raise ValueError("Turn was cancelled")

    message = await executor.message(turn, arguments["source_id"])
    source = event_from_message(message, executor.bot.user.id)
    recheck([source])
    # Register this snapshot before another await. A gateway edit/delete during
    # the parent read then remains visible to the shared writer's stale check.
    executor.db.record_event(source)
    evidence = []
    if arguments["kind"] == "profile" and source.reply_to:
        parent_channel_id = getattr(message.reference, "channel_id", None) or source.channel_id
        channel = executor.access.channel(turn, parent_channel_id)
        parent = event_from_message(
            await channel.fetch_message(source.reply_to), executor.bot.user.id
        )
        recheck([source, parent])
        executor.db.record_event(parent)
        evidence.append(parent)
    return source, evidence


async def run(executor, turn, name, a):
    event = turn.event
    if name == "USER_STATS":
        member = executor.access.member(turn, a["user_id"])
        return dict(
            executor.db.connection.execute(
                "SELECT COUNT(*) AS observed_messages,MIN(created_at) AS first_seen,MAX(created_at) AS last_seen FROM events WHERE guild_id=? AND author_id=? AND deleted=0",
                (str(event.guild_id), str(member.id)),
            ).fetchone()
        )
    if name == "MEMORY_SEARCH":
        if turn.cancelled:
            raise ValueError("Turn was cancelled before memory search")
        channels = executor.access.allowed_channels(turn)
        facts = public_memories(executor.memory.recall(event.guild_id, channels, query=a["query"]))
        if any(p["kind"] == "profile" for p in facts):
            turn.loaded_tools.add("MEM_REVISE")
        messages = executor.memory.search_events(
            event.guild_id, channels, a["query"], exclude_ids=[event.id]
        )
        confirmed_channels = executor.access.allowed_channels(turn)
        if set(confirmed_channels) != set(channels):
            facts = public_memories(executor.memory.recall(event.guild_id, confirmed_channels, query=a["query"]))
            messages = executor.memory.search_events(event.guild_id, confirmed_channels, a["query"], exclude_ids=[event.id])
        own = executor.memory.self_context(
            event.guild_id, confirmed_channels, executor.bot.user.id, query=a["query"],
            budget=executor.config.get("memory", "self_chars", 2000),
            limit=executor.config.get("memory", "self_limit", 12),
        )
        if messages:
            # Load the registered verification schema only after there is a source to verify.
            turn.loaded_tools.add("READ_MESSAGE")
        result = {
            "status": "ok" if facts or messages or own['entries'] else "no_match",
            "memories": facts,
            "self_memory": own,
            "messages": messages,
            "coverage": "Only stored events; not an exhaustive Discord history search",
            "interpretation": (
                "找到相關記憶或訊息索引。這些是已儲存的線索，並非本次已打開原訊息或看過原圖；"
                "不能把尚未查證說成沒找到。要確認連結或附件目前是否存在，可用 READ_MESSAGE 讀取下列 url；"
                "需要辨認圖片內容時再用 VIEW_IMAGE。"
                if facts or messages or own['entries']
                else "本次只在可讀範圍的本地索引中沒有匹配；不代表對方從未貼過、記錯群組或原訊息已刪除。"
            ),
        }
        return executor.memory.search_context(
            event.guild_id,
            confirmed_channels,
            result,
            bot_id=executor.bot.user.id,
            exclude_ids=[event.id],
            result_budget=executor.config.get("tools", "max_result_chars", 10000),
        )
    if name == "LOOKUP_MEMORY":
        executor.access.member(turn, a["user_id"])
        turn.loaded_tools.add("MEM_REVISE")
        return public_memories(
            executor.memory.recall(
                event.guild_id, executor.access.allowed_channels(turn), subjects=[a["user_id"]]
            )
        )
    if name == "MEM_REVISE":
        target = executor.db.connection.execute("SELECT subject_id FROM memories WHERE id=?", (a["memory_id"],)).fetchone()
        if target and target['subject_id'] == str(executor.bot.user.id) and a['relation'] != 'delete':
            raise ValueError("紗月自身資料使用分類來源入口PERMANENT_MEMORY，不使用人物畫像")
        return executor.memory.revise_profile(
            event,
            int(a["memory_id"]),
            a["relation"],
            a["context"],
            a["value"],
            allowed_channels=executor.access.allowed_channels(turn),
            evidence=confirmation_sources(turn),
        )
    if name == "MEM_OBSERVE":
        executor.access.member(turn, a["user_id"])
        if int(a["user_id"]) == executor.bot.user.id:
            raise ValueError("紗月自身資料使用分類來源入口PERMANENT_MEMORY，不使用人物畫像")
        sources = []
        from sayuki_bot.discord.events import event_from_message

        for identifier in dict.fromkeys(
            x.strip().removeprefix("msg_") for x in a["source_ids"].split(",")
        ):
            message = await executor.message(turn, identifier)
            source = event_from_message(message, executor.bot.user.id)
            executor.db.record_event(source)
            sources.append(source)
        # Every source was fetched through Access; recheck all channels after
        # awaits, before persisting the observation through the shared writer.
        executor.access.authorize(turn, name, a)
        executor.access.member(turn, a["user_id"])
        for source in sources:
            executor.access.channel(turn, source.channel_id)
        if turn.cancelled:
            raise ValueError("Turn was cancelled")
        anchor = max(sources, key=lambda e: (e.created_at, e.id))
        return executor.memory.update(
            anchor,
            a["user_id"],
            "profile",
            a["key"],
            a["value"],
            basis=a["basis"],
            evidence=sources,
            allowed_channels=executor.access.allowed_channels(turn),
        )
    if name in {"MEM_UPDATE", "MEM_DELETE"}:
        member = executor.access.member(turn, a["user_id"])
        if name == "MEM_UPDATE" and member.id == executor.bot.user.id:
            raise ValueError("紗月自身資料使用分類來源入口PERMANENT_MEMORY，不使用人物畫像")
        if a["kind"] not in {"profile", "event"}:
            raise ValueError("kind must be profile or event")
        evidence = None
        if name == "MEM_UPDATE" and a.get("source_id"):
            event, evidence = await specified_source(executor, turn, name, a)
        elif a["kind"] == "profile":
            evidence = confirmation_sources(turn)
        return executor.memory.update(
            event,
            member.id,
            a["kind"],
            a["key"],
            a.get("value", ""),
            operation="delete" if name == "MEM_DELETE" else "set",
            ttl_hours=executor.config.get("memory", "short_ttl_hours", 24)
            if a["kind"] == "event"
            else None,
            allowed_channels=executor.access.allowed_channels(turn),
            evidence=evidence,
        )
    if name in {"PERMANENT_MEMORY", "EDIT_PERMANENT_MEMORY", "DELETE_PERMANENT_MEMORY"}:
        channels = executor.access.allowed_channels(turn)
        if name == "DELETE_PERMANENT_MEMORY":
            return executor.memory.own.delete(event, executor.bot.user.id, a['entry_id'], channels)
        identifiers = list(dict.fromkeys(x.strip() for x in a['source_ids'].split(',')))
        if not 1 <= len(identifiers) <= 4 or any(not re.fullmatch(r'msg_[1-9][0-9]*', x) for x in identifiers):
            raise ValueError("自身來源須為1–4個msg_ID")
        from sayuki_bot.discord.events import event_from_message

        sources = []
        for identifier in identifiers:
            message = await executor.message(turn, identifier)
            source = event_from_message(message, executor.bot.user.id)
            executor.db.record_event(source)
            sources.append(source)
        executor.access.authorize(turn, name, a)
        channels = executor.access.allowed_channels(turn)
        for source in sources:
            executor.access.channel(turn, source.channel_id)
        if turn.cancelled:
            raise ValueError("Turn was cancelled before self memory write")
        return executor.memory.own.write(
            event, executor.bot.user.id, a['category'], a['content'], sources, channels,
            entry_id=a.get('entry_id'),
        )
    if name in {
        "SERVER_MEMORY",
        "DELETE_SERVER_MEMORY",
    }:
        kind = "culture"
        key = (
            a.get("entry_id")
            or a.get("key")
            or (
                a.get("kind", "")
                + ":"
                + hashlib.sha256(a.get("content", "").encode()).hexdigest()[:16]
            )
        )
        return executor.memory.update(
            event,
            executor.bot.user.id,
            kind,
            key,
            a.get("content", ""),
            operation="delete" if name.startswith("DELETE") else "set",
        )
    raise ValueError(f"Unsupported tool: {name}")
