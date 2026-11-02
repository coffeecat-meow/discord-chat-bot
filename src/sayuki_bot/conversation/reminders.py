"""Persistent reminders enter the same per-channel conversation queue."""

import asyncio
from types import SimpleNamespace

from .models import Event, Turn


class Reminders:
    def __init__(self, bot):
        self.bot = bot
        self.tasks = set()

    def tick(self, now, asleep):
        if asleep and not self.bot.config.get("reminders", "during_sleep", False):
            return
        db = self.bot.db
        rows = db.connection.execute(
            "SELECT * FROM reminders WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT 10",
            (now.isoformat(),),
        ).fetchall()
        for row in rows:
            guild_id, channel_id = int(row["guild_id"]), int(row["channel_id"])
            if guild_id not in self.bot.runtime_guilds:
                continue
            guild = self.bot.get_guild(guild_id)
            channel = self.bot.get_channel(channel_id)
            member = guild.get_member(int(row["user_id"])) if guild else None
            if not channel or not member:
                continue
            identifier = row["id"]
            key = "reminder:" + identifier
            if not db.claim(key, identifier, "reminder"):
                with db.connection:
                    db.connection.execute(
                        "UPDATE reminders SET status='unknown' WHERE id=?", (identifier,)
                    )
                continue
            import discord

            event = Event(
                discord.utils.time_snowflake(now),
                guild_id,
                channel_id,
                member.id,
                member.display_name,
                row["content"],
                now,
            )
            future = asyncio.get_running_loop().create_future()
            turn = Turn(
                event,
                SimpleNamespace(channel=channel, author=member),
                list(self.bot.history[channel_id]),
                reason="reminder",
                id=identifier,
                required_response=f"<@{member.id}> {row['content']}",
                completion=future,
            )
            with db.connection:
                db.connection.execute(
                    "UPDATE reminders SET status='queued' WHERE id=?", (identifier,)
                )
            if not self.bot.engine.submit(turn):
                with db.connection:
                    db.connection.execute(
                        "DELETE FROM receipts WHERE key=? AND status='pending'", (key,)
                    )
                    db.connection.execute(
                        "UPDATE reminders SET status='pending' WHERE id=?", (identifier,)
                    )
                continue
            task = asyncio.create_task(
                self.finish(identifier, key, future), name=f"reminder-{identifier}"
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def finish(self, identifier, key, future):
        result = await future
        if result["status"] == "deferred" and not result["sent"]:
            # Dequeued after sleep began: no send was attempted, so it is safe
            # to return this reminder to pending for the next awake tick.
            with self.bot.db.connection:
                self.bot.db.connection.execute(
                    "DELETE FROM receipts WHERE key=? AND status='pending'", (key,)
                )
                self.bot.db.connection.execute(
                    "UPDATE reminders SET status='pending' WHERE id=?", (identifier,)
                )
            return
        state = (
            "sent"
            if result["status"] == "completed" and result["sent"]
            else "unknown"
            if result["sent"] or result["status"] == "unknown"
            else "failed"
        )
        self.bot.db.settle(key, state)
        with self.bot.db.connection:
            self.bot.db.connection.execute(
                "UPDATE reminders SET status=? WHERE id=?", (state, identifier)
            )

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
