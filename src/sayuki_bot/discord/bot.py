from __future__ import annotations

import asyncio
import logging
import hashlib
from collections import defaultdict, deque
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
from discord.ext import commands

from sayuki_bot.conversation.models import Event, Turn
from .events import event_from_message
from sayuki_bot.conversation.attention import Attention
from sayuki_bot.conversation.engine import Engine
from sayuki_bot.context.compiler import ContextCompiler
from sayuki_bot.context.snapshots import ConversationContext
from sayuki_bot.delivery.service import Delivery
from sayuki_bot.discord.access import Access
from sayuki_bot.llm.client import ModelClient
from sayuki_bot.memory.service import MemoryService
from sayuki_bot.memory.background import BackgroundMemory
from sayuki_bot.storage.database import Database, stable_attachments
from sayuki_bot.tools.executor import ToolExecutor

logger = logging.getLogger(__name__)


class SayukiBot(commands.Bot):
    def __init__(self, config, api_key):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.presences = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, replied_user=False
            ),
        )
        self.config = config
        self.db = Database(
            config.path("database"),
            diagnostic_retention_days=config.get("storage", "diagnostic_retention_days", 30),
        )
        self.memory = MemoryService(self.db)
        self.model = ModelClient(config, self.db, api_key)
        self.access = Access(self, config)
        self.compiler = ContextCompiler(config, self.memory)
        self.delivery = Delivery(config, self.db, self)
        self.executor = ToolExecutor(self, config, self.db, self.memory, self.model, self.access)
        self.background = BackgroundMemory(
            config,
            self.memory,
            self.model,
            access=self.access,
            bot_id=lambda: self.user.id if self.user else None,
        )
        self.engine = Engine(
            config,
            self.compiler,
            self.model,
            self.executor,
            self.delivery,
            self.access,
            self.background,
        )
        self.attention = Attention(config)
        self.history = defaultdict(lambda: deque(maxlen=200))
        self.runtime_guilds = set(config.guilds)
        self.engine.context = ConversationContext(self)
        from sayuki_bot.conversation.reminders import Reminders

        self.reminders = Reminders(self)
        self.maintenance_task = None
        self.sayuki_status_until = None
        self._closed_resources = False
        self.choice_views = set()
        from .admin import register_admin

        register_admin(self)
        from .tasks import register_tasks

        register_tasks(self)

    async def on_ready(self):
        if not self.runtime_guilds:
            self.runtime_guilds = {
                g.id for g in self.guilds if any(g.get_member(uid) for uid in self.config.admins)
            }
        for row in self.db.connection.execute("SELECT guild_id,enabled FROM trusted_guilds"):
            if row["enabled"]:
                self.runtime_guilds.add(int(row["guild_id"]))
            else:
                self.runtime_guilds.discard(int(row["guild_id"]))
        logger.info("Ready: %s trusted guilds", len(self.runtime_guilds))
        if not self.maintenance_task:
            self.maintenance_task = asyncio.create_task(
                self.maintenance(), name="sayuki-maintenance"
            )
            # Guild-specific command sync avoids editing commands in other guilds.
            for guild_id in self.runtime_guilds:
                target = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=target)
                await self.tree.sync(guild=target)

    async def on_message(self, message):
        if not message.guild or message.guild.id not in self.runtime_guilds or not self.user:
            return
        if message.author.bot and message.author.id != self.user.id:
            return
        event = event_from_message(message, self.user.id)
        self.db.record_event(event)
        history = self.history[event.channel_id]
        if not any(x.id == event.id for x in history):
            history.append(event)
        if message.author.bot:
            return
        # Populate live recent context once per channel after restart.
        if len(history) == 1:
            try:
                recent = [
                    m
                    async for m in message.channel.history(
                        limit=self.config.get("conversation", "history_limit", 25)
                    )
                ]
                for msg in reversed(recent):
                    if msg.author.bot and msg.author.id != self.user.id:
                        continue
                    old = event_from_message(msg, self.user.id)
                    self.db.record_event(old)
                    if not any(x.id == old.id for x in history):
                        history.append(old)
                ordered = sorted(history, key=lambda x: x.created_at)
                history.clear()
                history.extend(ordered)
            except discord.HTTPException:
                logger.warning("Could not load recent channel history")
        reason = self.attention.decide(
            event, list(history), self.user.id, datetime.now(timezone.utc)
        )
        turn = Turn(event, message, list(history), reason)
        turn.bot_id = self.user.id
        if reason != "echo" and not self.config.sleep.sleeping(datetime.now(timezone.utc)):
            from sayuki_bot.context.cross_channel import discover

            discover(self, turn)
            if turn.cross_sources and reason in {"ignore", "proactive", "continuation"}:
                reason = turn.reason = "cross_continuation"
        if reason == "ignore":
            return
        turn.reason_facts = self.attention.facts(
            event, list(history), self.user.id, datetime.now(timezone.utc)
        )
        if turn.cross_context:
            turn.reason_facts += (
                "cross_channel_cue：有跨頻道接續或轉場原文，需依作者、來源頻道與時間判斷",
            )
        if not self.engine.submit(turn):
            logger.warning("Queue full for channel %s", event.channel_id)

    def record_delivered(self, message):
        """Called only after Discord confirms send; gateway echo is idempotent."""
        event = event_from_message(message, self.user.id)
        self.db.record_event(event)
        history = self.history[event.channel_id]
        if not any(item.id == event.id for item in history):
            history.append(event)

    async def receive_interaction(
        self, interaction, text, reason="interaction", *, panel_context=None, panel_view=None
    ):
        # Buttons never count as an @ that wakes a sleeping bot.
        if not interaction.guild or interaction.guild.id not in self.runtime_guilds:
            return {"status": "rejected"}
        if self.config.sleep.sleeping(datetime.now(timezone.utc)):
            return {"status": "sleeping"}
        event = Event(
            interaction.id,
            interaction.guild.id,
            interaction.channel_id,
            interaction.user.id,
            interaction.user.display_name,
            text,
            datetime.now(timezone.utc),
        )
        source = SimpleNamespace(channel=interaction.channel, author=interaction.user)
        turn = Turn(event, source, list(self.history[event.channel_id]), reason)
        turn.bot_id = self.user.id
        turn.interaction_context = panel_context
        try:
            channel = self.access.channel(turn, event.channel_id)
            if panel_context:
                origin = panel_context["source"]
                if origin["message_id"]:
                    try:
                        original = await asyncio.wait_for(
                            channel.fetch_message(int(origin["message_id"][4:])), timeout=3
                        )
                    except (discord.HTTPException, asyncio.TimeoutError):
                        return {"status": "source_unavailable"}
                    if original.content[:1000] != origin["text"] or (
                        panel_view is not None
                        and hashlib.sha256(original.content.encode()).hexdigest()
                        != panel_view.source_hash
                    ):
                        return {"status": "source_unavailable"}
                    turn.supplemental.append(event_from_message(original, self.user.id))
                self.access.channel(turn, event.channel_id)
                if panel_view is not None and not panel_view.active():
                    return {"status": "expired"}
                if self.config.sleep.sleeping(datetime.now(timezone.utc)):
                    return {"status": "sleeping"}
        except PermissionError:
            return {"status": "rejected"}
        self.db.record_event(event)
        if not self.engine.submit(turn):
            return {"status": "queue_full"}
        return {"status": "queued"}

    async def on_raw_message_delete(self, payload):
        self.db.invalidate_event(payload.message_id, deleted=True)
        if payload.channel_id in self.history:
            self.history[payload.channel_id] = deque(
                (x for x in self.history[payload.channel_id] if x.id != payload.message_id),
                maxlen=200,
            )
        self.engine.invalidate_source(payload.message_id)

    async def on_raw_message_edit(self, payload):
        # Discord also sends updates for link previews, pins and our own panel
        # edits. Only a real text/attachment change may invalidate sources,
        # memories and in-flight replies.
        message = payload.message
        if (
            not self.user
            or payload.guild_id not in self.runtime_guilds
            or (message.author.bot and message.author.id != self.user.id)
        ):
            return
        event = event_from_message(message, self.user.id)
        history = self.history.get(payload.channel_id, ())
        old = next((x for x in history if x.id == event.id), None)
        stored = self.db.connection.execute(
            "SELECT text,attachments FROM events WHERE id=? AND deleted=0", (str(event.id),)
        ).fetchone()
        current = (event.text, stable_attachments(list(event.attachments)))
        if old is not None:
            changed = (old.text, stable_attachments(list(old.attachments))) != current
        elif stored is not None:
            changed = (stored["text"], stable_attachments(stored["attachments"])) != current
        else:
            changed = False  # never seen, so nothing can depend on it
        if not changed:
            return
        logger.info("訊息已編輯，重新整理來源 message=%s", event.id)
        self.db.invalidate_event(event.id, deleted=False)
        self.engine.invalidate_source(event.id)
        # Refresh stored source without triggering another reply; keep order.
        self.db.record_event(event)
        if old is not None:
            self.history[payload.channel_id] = deque(
                (event if x.id == event.id else x for x in history), maxlen=200
            )

    async def maintenance(self):
        previous_sleep = None
        while not self.is_closed():
            try:
                now = datetime.now(timezone.utc)
                asleep = self.config.sleep.sleeping(now)
                expired = self.sayuki_status_until and self.sayuki_status_until <= now
                if asleep != previous_sleep or expired:
                    await self.change_presence(
                        status=discord.Status.idle if asleep else discord.Status.online,
                        activity=discord.Game(self.config.sleep.status) if asleep else None,
                    )
                    previous_sleep = asleep
                    self.sayuki_status_until = None
                self.reminders.tick(now, asleep)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Maintenance failed")
            await asyncio.sleep(15)

    async def close(self):
        if not self._closed_resources:
            self._closed_resources = True
            for view in list(self.choice_views):
                view.stop()
            if self.maintenance_task:
                self.maintenance_task.cancel()
                await asyncio.gather(self.maintenance_task, return_exceptions=True)
            await self.engine.close()
            await self.reminders.close()
            await self.model.close()
            self.db.close()
        await super().close()
