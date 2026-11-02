"""Bounded, source-scoped reply evidence for local memory search.

Reply edges carry quotations, not a decision that a fact was corrected.
"""

import copy
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from sayuki_bot.context.records import encoded_size, event_record
from sayuki_bot.conversation.models import Event


def enrich_search_result(
    db, guild_id, channels, result, *, now, bot_id, exclude_ids=(), result_budget=10000
):
    if result.get("status") != "ok" or not result.get("messages"):
        return result
    channels = {str(c) for c in channels}
    guild = str(guild_id)
    excluded = {str(identifier) for identifier in exclude_ids}
    output = copy.deepcopy(result)
    roots = {(str(r["id"]), str(r["channel_id"])): r for r in output["messages"][:8]}
    anchors = result["messages"][:8]
    existing = {str(r["id"]) for r in result["messages"]} | excluded
    queue = [
        (str(r["id"]), str(r["channel_id"]), 0, None)
        for r in anchors
        if str(r["channel_id"]) in channels
    ]
    visited, candidates, omitted = set(), [], 0
    while queue and len(visited) < 24:
        identifier, channel, depth, child_time = queue.pop(0)
        if identifier in visited or identifier in excluded:
            continue
        visited.add(identifier)
        row = db.connection.execute(
            "SELECT * FROM events WHERE id=? AND guild_id=? AND channel_id=? AND deleted=0",
            (identifier, guild, channel),
        ).fetchone()
        if not row or not db.memory_enabled(guild, row["author_id"]):
            continue
        when = datetime.fromisoformat(row["created_at"])
        if when > now or (child_time is not None and when > child_time):
            continue
        if depth == 0:
            hit = roots[(identifier, channel)]
            if str(hit["author_id"]) != row["author_id"] or not row["text"].startswith(hit["text"]):
                continue
            hit.update(
                author=row["author_name"][:80],
                self=row["author_id"] == str(bot_id),
                message_id=f"msg_{identifier}",
                reply_to=f"msg_{row['reply_to']}" if row["reply_to"] else None,
                text_truncated=len(hit["text"]) < len(row["text"]),
            )
        if identifier not in existing:
            event = Event(
                int(row["id"]),
                int(guild),
                int(channel),
                int(row["author_id"]),
                row["author_name"],
                row["text"],
                when,
                reply_to=int(row["reply_to"]) if row["reply_to"] else None,
                attachments=tuple(json.loads(row["attachments"])),
            )
            record = event_record(event, bot_id, ZoneInfo("Asia/Taipei"))
            record["url"] = f"https://discord.com/channels/{guild}/{channel}/{identifier}"
            record["availability"] = "stored_index_not_live_verified"
            candidates.append(record)
        if depth < 2:
            if row["reply_to"]:
                queue.append((row["reply_to"], channel, depth + 1, when))
            children = db.connection.execute(
                """SELECT e.id FROM events e LEFT JOIN privacy p ON p.guild_id=e.guild_id AND p.user_id=e.author_id
                   WHERE e.reply_to=? AND e.guild_id=? AND e.channel_id=? AND e.deleted=0
                   AND COALESCE(p.enabled,1)=1 AND julianday(e.created_at)>=julianday(?)
                   AND julianday(e.created_at)<=julianday(?)
                   ORDER BY julianday(e.created_at),CAST(e.id AS INTEGER) LIMIT 9""",
                (identifier, guild, channel, row["created_at"], now.isoformat()),
            ).fetchall()
            omitted += int(len(children) > 8)
            queue.extend((r["id"], channel, depth + 1, None) for r in children[:8])
    omitted += int(bool(queue))
    context = output["reply_context"] = {
        "note": "本地命中訊息的reply關係原文；reply不等於同意、更正或同一對象。依作者與內容判斷，未即時查證Discord。",
        "max_depth": 2,
        "max_records": 8,
        "max_chars": 4000,
        "omitted_at_least": omitted + len(candidates),
        "records": [],
    }
    budget = result_budget

    def fits():
        return encoded_size([{"status": "ok", "tool": "MEMORY_SEARCH", "result": output}]) <= budget

    if not fits():
        return result
    used = 0
    for record in sorted(candidates, key=lambda r: (r["time"], int(r["message_id"][4:]))):
        size = encoded_size(record)
        if len(context["records"]) >= 8 or size > 1800 or used + size > 4000:
            continue
        context["records"].append(record)
        if not fits():
            context["records"].pop()
        else:
            used += size
            context["omitted_at_least"] -= 1
    return output
