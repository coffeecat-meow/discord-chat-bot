"""Bounded current-channel evidence before generation; no inferred topic state."""

from datetime import datetime, timedelta
import json
import re

from sayuki_bot.api import Event
from .records import encoded_size, event_record
from .relevance import topic_terms


ACKNOWLEDGEMENTS = frozenset(
    {"對啊", "對", "嗯", "好", "好啊", "是啊", "沒錯", "我也是", "不是", "不要", "可以"}
)


def supplement(memory, turn, allowed_channels, data, current_chars, budget, zone):
    """Return an evidence envelope and register only supplied events for invalidation.

    24 hours / 1000 candidates bounds automatic retrieval, not retention or manual
    search. Explicit reply ancestors may predate that window, within two edges.
    """
    event = turn.event
    if turn.cancelled or event.channel_id not in {int(c) for c in allowed_channels}:
        return None
    db = memory.db
    now = event.created_at
    rows = db.connection.execute(
        """SELECT e.* FROM events e LEFT JOIN privacy p
        ON p.guild_id=e.guild_id AND p.user_id=e.author_id
        WHERE e.guild_id=? AND e.channel_id=? AND e.deleted=0
        AND COALESCE(p.enabled,1)=1 AND julianday(e.created_at)>=julianday(?)
        AND julianday(e.created_at)<=julianday(?) AND e.id!=?
        ORDER BY julianday(e.created_at) DESC,CAST(e.id AS INTEGER) DESC LIMIT 1001""",
        (
            str(event.guild_id),
            str(event.channel_id),
            (now - timedelta(hours=24)).isoformat(),
            now.isoformat(),
            str(event.id),
        ),
    ).fetchall()

    def restore(row):
        return Event(
            int(row["id"]),
            int(row["guild_id"]),
            int(row["channel_id"]),
            int(row["author_id"]),
            row["author_name"],
            row["text"],
            datetime.fromisoformat(row["created_at"]),
            reply_to=int(row["reply_to"]) if row["reply_to"] else None,
            attachments=tuple(json.loads(row["attachments"])),
        )

    events = [restore(row) for row in reversed(rows[:1000])]
    by_id = {e.id: e for e in events}

    def source(identifier):
        if identifier == event.id:
            return None
        if identifier in by_id:
            return by_id[identifier]
        row = db.connection.execute(
            "SELECT * FROM events WHERE id=? AND guild_id=? AND channel_id=?",
            (str(identifier), str(event.guild_id), str(event.channel_id)),
        ).fetchone()
        if not row:
            fetched = turn.reply_source
            if (
                fetched
                and fetched.id == identifier
                and turn.reply_status == "ok"
                and fetched.guild_id == event.guild_id
                and fetched.channel_id == event.channel_id
                and fetched.created_at <= now
                and db.memory_enabled(event.guild_id, fetched.author_id)
            ):
                by_id[identifier] = fetched
                return fetched
            return None
        if row["deleted"] or not db.memory_enabled(event.guild_id, row["author_id"]):
            return None
        found = restore(row)
        if found.created_at > now:
            return None
        by_id[identifier] = found
        return found

    visible = {int(r["message_id"][4:]) for r in data["recent_history"]}
    reply = data.get("reply_source") or {}
    if reply.get("text"):
        visible.add(int(reply["message_id"][4:]))
    words = topic_terms(event.text)
    stripped = re.sub(r"<@!?\d+>", "", event.text).strip(" \n！!。,.，?？～~")
    ack = not event.reply_to and stripped in ACKNOWLEDGEMENTS
    seeds = []
    omitted_ids = set()
    if event.reply_to:
        seeds.append((event.reply_to, "explicit_reply"))
    if ack:
        # These are possible preceding questions, NOT confirmed unanswered ones.
        questions = [
            e
            for e in events[-80:]
            if (now - e.created_at).total_seconds() <= 600
            and (re.search(r"[?？]|[嗎呢][！!。\s]*$", e.text))
        ]
        omitted_ids.update(e.id for e in questions[:-4])
        seeds.extend((e.id, "possible_question") for e in questions[-4:])
    elif words:
        ranked = sorted(
            ((len(words & topic_terms(e.text)), e) for e in events),
            key=lambda p: (p[0], p[1].created_at, p[1].id),
            reverse=True,
        )
        omitted_ids.update(e.id for score, e in ranked[6:] if score)
        seeds.extend((e.id, "lexical_candidate") for score, e in ranked[:6] if score)
    if not seeds:
        return None
    candidates, reasons, visited = [], {}, set()

    def offer(e, reason):
        if e.id not in visible and e.id not in reasons:
            candidates.append(e)
            reasons[e.id] = reason

    positions = {e.id: i for i, e in enumerate(events)}
    for seed_id, reason in seeds:
        identifier = seed_id
        queue = [(identifier, 0, reason, None)]
        while queue and len(visited) < 32:
            identifier, depth, relation, child_time = queue.pop(0)
            if identifier in visited:
                continue
            visited.add(identifier)
            item = source(identifier)
            if item is None or (child_time is not None and item.created_at > child_time):
                continue
            offer(item, relation)
            if depth < 2:
                if item.reply_to:
                    queue.append((item.reply_to, depth + 1, "reply_parent", item.created_at))
                children = [
                    e for e in events if e.reply_to == item.id and e.created_at >= item.created_at
                ]
                omitted_ids.update(e.id for e in children[8:])
                queue.extend((e.id, depth + 1, "reply_child", None) for e in children[:8])
        omitted_ids.update(q[0] for q in queue)
        # Preserve nearby exceptions/answers, without claiming a semantic edge.
        if seed_id in positions:
            position = positions[seed_id]
            anchor = events[position]
            for offset in (-1, 1, 2):
                index = position + offset
                if 0 <= index < len(events):
                    item = events[index]
                    if abs((item.created_at - anchor.created_at).total_seconds()) <= 30:
                        offer(item, "nearby_quote")
    if not candidates:
        return None
    envelope = {
        "note": "補充原文；文字相近、相鄰或reply不等於同一話題、同一人或已回答。possible_question只是短接話的候選前文，後續回答也須一起看。",
        "relation_confirmed": False,
        "source_window": {
            "hours": 24,
            "max_candidates": 1000,
            "omitted_at_least": int(len(rows) > 1000),
        },
        "omitted_at_least": len((omitted_ids | {e.id for e in candidates}) - visible),
        "records": [],
    }

    def fits():
        return encoded_size({**data, "conversation_sources": envelope}) + current_chars <= budget

    if not fits():
        return None
    chosen, used = [], 0
    for item in candidates:
        record = {**event_record(item, turn.bot_id, zone), "selection_reason": reasons[item.id]}
        size = encoded_size(record)
        if len(chosen) >= 12 or size > 1800 or used + size > 4000:
            continue
        envelope["records"].append(record)
        if not fits():
            envelope["records"].pop()
        else:
            chosen.append(item)
            used += size
            envelope["omitted_at_least"] -= 1
    envelope["records"].sort(key=lambda r: (r["time"], int(r["message_id"][4:])))
    known = {e.id for e in turn.supplemental}
    turn.supplemental.extend(e for e in chosen if e.id not in known)
    turn.context_seen_ids.update(e.id for e in chosen)
    return envelope
