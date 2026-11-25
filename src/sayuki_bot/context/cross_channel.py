"""Small source-backed handoffs, only inside configured shared channels.

No global user-following state, new database schema, network reads or model call.
An invitation is a candidate, never a confirmed conversational relationship.
"""
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sayuki_bot.api import Event
from .records import bounded_record, event_record, encoded_size
from .relevance import topic_terms

_CUE = re.compile(r"剛剛|剛才|接著.{0,12}(?:聊|說|講)|繼續.{0,12}(?:聊|說|講)")
_MOVE = re.compile(
    r"^(?:我們|咱們|大家|走[，, ]*)?\s*(?:去|到|換到|換去|移到)\s*"
    r"<#(\d+)>\s*(?:再|繼續|接著)?\s*(?:聊|說|講)(?:吧|啦|喔|好嗎|一下|天)?[。！!？?～~\s]*$"
)
_CANCEL = re.compile(r"^(?:算了|不去了|先不聊|取消)")


def invitation(text, bot_id=None, aliases=()):
    if bot_id:
        text = re.sub(rf"^<@!?{bot_id}>[\s，,:：]*", "", text.strip())
    for name in aliases:
        if name:
            text = re.sub(rf"^{re.escape(name)}[\s，,:：]+", "", text, flags=re.I)
    match = _MOVE.fullmatch(text.strip())
    return int(match[1]) if match else None


def discover(bot, turn):
    event = turn.event
    if (not bot.config.get("conversation", "continuation_enabled", True)
            or event.reply_to or event.text.lstrip().startswith("!")
            or not bot.db.memory_enabled(event.guild_id, event.author_id)):
        return
    channels = [c for c in bot.access.allowed_channels(turn) if c != event.channel_id]
    if not channels:
        return
    window = bot.config.get("conversation", "cross_channel_window_seconds", 600)
    rows = bot.db.connection.execute(
        f"""SELECT e.* FROM events e LEFT JOIN privacy p
        ON p.guild_id=e.guild_id AND p.user_id=e.author_id
        WHERE e.guild_id=? AND e.channel_id IN ({','.join('?' for _ in channels)})
        AND e.deleted=0 AND COALESCE(p.enabled,1)=1
        AND julianday(e.created_at)>=julianday(?) AND julianday(e.created_at)<=julianday(?)
        ORDER BY julianday(e.created_at) DESC, CAST(e.id AS INTEGER) DESC LIMIT 500""",
        (str(event.guild_id), *map(str, channels),
         (event.created_at-timedelta(seconds=window)).isoformat(), event.created_at.isoformat()),
    ).fetchall()
    cached = {e.id: e for c in channels for e in bot.history[c]}
    events = [Event(int(r["id"]), int(r["guild_id"]), int(r["channel_id"]),
                    int(r["author_id"]), r["author_name"], r["text"],
                    datetime.fromisoformat(r["created_at"]), reply_to=int(r["reply_to"]) if r["reply_to"] else None,
                    attachments=tuple(json.loads(r["attachments"]))) for r in reversed(rows)
              if int(r["id"]) in cached and cached[int(r["id"])].text == r["text"]
              and (datetime.fromisoformat(r["created_at"]), int(r["id"])) < (event.created_at, event.id)]
    authored = [e for e in events if e.author_id == event.author_id]
    def destination(e):
        return invitation(e.text, turn.bot_id, bot.config.get("discord", "aliases", []))

    moves = [e for e in authored if destination(e) is not None]
    move = moves[-1] if moves else None
    if move and (destination(move) != event.channel_id or any(
        e.channel_id == move.channel_id and (e.created_at, e.id) > (move.created_at, move.id)
        and _CANCEL.match(e.text.strip()) for e in authored
    )):
        move = None
    cue = bool(_CUE.search(event.text))
    if move is None and not cue:
        return
    choices = []
    for channel_id in channels:
        if move is not None and channel_id != move.channel_id:
            continue
        local = [e for e in events if e.channel_id == channel_id]
        if move is not None:
            local = [e for e in local if (e.created_at, e.id) <= (move.created_at, move.id)]
        local = local[-12:]
        own = [e for e in local if e.author_id == turn.bot_id]
        users = [e for e in local if e.author_id == event.author_id]
        if not own or not users:
            continue
        # A mention elsewhere is not enough: the source must have recent bot
        # participation adjacent to this user's part of that conversation.
        if abs((own[-1].created_at-users[-1].created_at).total_seconds()) > 120:
            continue
        selected = local[-6:]
        required = {own[-1].id, users[-1].id}
        if not required <= {e.id for e in selected}:
            continue
        score = len(topic_terms(event.text) & topic_terms(" ".join(e.text for e in selected)))
        choices.append((score, selected))
    if not choices:
        return
    choices.sort(key=lambda pair: pair[0], reverse=True)
    if len(choices) > 1 and choices[0][0] == choices[1][0]:
        return  # Multiple equally plausible source conversations: do not guess.
    turn.cross_sources = choices[0][1]
    turn.cross_context = {
        "kind": "invited_transfer" if move else "explicit_cue",
        "invitation_id": move.id if move else None,
        "requester_id": str(event.author_id), "destination_channel_id": str(event.channel_id),
        "relation_confirmed": False, "older_context_omitted": True,
    }
    if not envelope(turn, bot.config.sleep.timezone,
                    min(4000, bot.config.get("conversation", "history_chars", 14000) // 2)):
        turn.cross_sources = []
        turn.cross_context = None
        return
    turn.supplemental.extend(turn.cross_sources)


def validate(bot, turn, guild, member, shared):
    """Called by Access.channel, including before/after models and each send."""
    from sayuki_bot.discord.access import can_read

    for event in turn.cross_sources:
        source = guild.get_channel_or_thread(event.channel_id)
        if (event.guild_id != turn.event.guild_id
                or not {event.channel_id, turn.event.channel_id} <= shared
                or not can_read(source, member) or not can_read(source, guild.me)
                or not bot.db.memory_enabled(event.guild_id, event.author_id)
                or not bot.db.memory_enabled(turn.event.guild_id, turn.event.author_id)):
            raise PermissionError("Cross-channel source is no longer available")
        row = bot.db.connection.execute("SELECT * FROM events WHERE id=?", (str(event.id),)).fetchone()
        cached = next((e for e in bot.history[event.channel_id] if e.id == event.id), None)
        if (cached is None or cached.text != event.text
                or not row or row["deleted"] or row["text"] != event.text
                or row["guild_id"] != str(event.guild_id) or row["channel_id"] != str(event.channel_id)
                or row["author_id"] != str(event.author_id)
                or json.loads(row["attachments"]) != list(event.attachments)):
            raise PermissionError("Cross-channel source changed")


def envelope(turn, timezone, budget=4000):
    if not turn.cross_context:
        return None
    data = {**turn.cross_context, "records": []}
    zone = ZoneInfo(timezone)
    record_budget = min(600, (budget - encoded_size(data) - 16) // len(turn.cross_sources))
    for event in turn.cross_sources:
        item = bounded_record({**event_record(event, turn.bot_id, zone),
                               "guild_id": str(event.guild_id), "channel_id": str(event.channel_id)}, record_budget)
        if item is None:
            return None
        data["records"].append(item)
    assert encoded_size(data) <= budget
    return data
