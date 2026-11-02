"""Refresh context only at dequeue and existing tool continuation boundaries.

This module never submits/removes turns or decides who should speak. It reads
one channel under live permissions; the current event remains the fixed anchor.
"""

import asyncio
import json
from zoneinfo import ZoneInfo

import discord

from sayuki_bot.discord.events import event_from_message
from .records import bounded_record, encoded_size, event_record


class ConversationContext:
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.zone = ZoneInfo(self.config.sleep.timezone)

    def current(self, turn):
        self.bot.access.channel(turn, turn.event.channel_id)
        return sorted(
            (
                e
                for e in self.bot.history[turn.event.channel_id]
                if e.guild_id == turn.event.guild_id
            ),
            key=lambda e: (e.created_at, e.id),
        )

    async def prepare(self, turn):
        turn.bot_id = self.bot.user.id
        turn.history = self.current(turn)
        turn.context_seen_ids = {turn.event.id, *(e.id for e in turn.history)}
        identifier = turn.event.reply_to
        if not identifier:
            return
        source = next((e for e in turn.history if e.id == identifier), None)
        row = self.bot.db.connection.execute(
            "SELECT deleted FROM events WHERE id=?", (str(identifier),)
        ).fetchone()
        if row and row["deleted"]:
            turn.reply_status = "not_found"
            return
        if source is None:
            channel = self.bot.access.channel(turn, turn.event.channel_id)
            try:
                message = await asyncio.wait_for(
                    channel.fetch_message(identifier),
                    timeout=self.config.get("conversation", "reply_fetch_timeout_seconds", 2.0),
                )
                source = event_from_message(message, turn.bot_id)
            except TimeoutError:
                turn.reply_status = "timeout"
                return
            except discord.Forbidden:
                turn.reply_status = "forbidden"
                return
            except discord.NotFound:
                turn.reply_status = "not_found"
                return
            except discord.HTTPException:
                turn.reply_status = "unavailable"
                return
        # Edits/deletes during the await invalidate this turn before any use.
        self.bot.access.channel(turn, turn.event.channel_id)
        if turn.cancelled:
            return
        if source.channel_id != turn.event.channel_id or source.guild_id != turn.event.guild_id:
            turn.reply_status = "forbidden"
            return
        turn.reply_source = source
        turn.reply_status = "ok"
        turn.context_seen_ids.add(source.id)

    def merge(self, turn):
        latest = self.current(turn)
        candidates = [e for e in latest if e.id not in turn.context_seen_ids]
        if not candidates:
            return None
        turn.context_seen_ids.update(e.id for e in candidates)
        records = []
        count_limit = self.config.get("conversation", "tool_merge_messages", 5)
        char_limit = self.config.get("conversation", "tool_merge_chars", 1500)
        # Reserve the newest arrivals first, then present accepted data in time order.
        for event in reversed(candidates):
            # Own preface is already represented by the assistant/tool-call block.
            if event.author_id == turn.bot_id:
                continue
            remaining = char_limit - turn.merge_chars
            record = bounded_record(event_record(event, turn.bot_id, self.zone), remaining)
            if turn.merge_count >= count_limit or record is None:
                turn.merge_omitted += 1
                continue
            records.append(record)
            turn.supplemental.append(event)
            turn.merge_chars += encoded_size(record)
            turn.merge_count += 1
        return (
            "以下為初始快照後新到的同頻道訊息引用；原回合對象不變，已完成的操作不要重做：\n"
            + json.dumps(
                {"arrivals": list(reversed(records)), "omitted_total": turn.merge_omitted},
                ensure_ascii=False,
            )
        )
