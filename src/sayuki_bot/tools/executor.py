from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

from .registry import REGISTRY


class ToolExecutor:
    def __init__(self, bot, config, database, memory, model, access):
        self.bot, self.config, self.db, self.memory, self.model, self.access = (
            bot,
            config,
            database,
            memory,
            model,
            access,
        )

    async def execute(self, turn, name, body):
        tool = REGISTRY[name]
        attempted = False
        try:
            args = tool.parse(body)
            if tool.action and turn.cancelled:
                raise PermissionError("Turn was cancelled before this action")
            if turn.reason == "reminder" and tool.action:
                raise PermissionError("Reminder turns may convey content, not execute new actions")
            signature = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)
            if signature in turn.seen_calls:
                return {"status": "already_attempted", "tool": name}
            self.access.authorize(turn, name, args)
            if tool.group == "social":
                from .social_actions import prepare

                prepare(self, turn, name, args)
            turn.seen_calls.add(signature)
            key = turn.id + ":tool:" + hashlib.sha256(signature.encode()).hexdigest()
            if tool.action and not self.db.claim(key, turn.id, name):
                return {
                    "status": "unknown",
                    "tool": name,
                    "error": "previous attempt must not be replayed",
                }
            try:
                attempted = tool.action
                result = await self.dispatch(turn, name, args)
            except BaseException as exc:
                # Memory validation runs before its local transaction. A denied
                # scope/invalid revision is a known non-write, not an uncertain
                # Discord action. Unexpected failures retain unknown semantics.
                if (
                    tool.group == "memory"
                    and tool.action
                    and isinstance(exc, (ValueError, PermissionError))
                ):
                    self.db.settle(key, "failed", type(exc).__name__)
                    return {"status": "rejected", "tool": name, "error": str(exc)}
                if tool.action:
                    self.db.settle(key, "unknown", type(exc).__name__)
                raise
            if tool.action:
                if isinstance(result, dict) and result.get("status") == "ignored":
                    self.db.settle(key, "failed", result.get("reason", "not applied"))
                    return {"status": "not_applied", "tool": name, "result": result}
                self.db.settle(key, "sent")
            return {"status": "ok", "tool": name, "result": result}
        except (ValueError, PermissionError, KeyError) as exc:
            return {
                "status": "unknown" if attempted else "rejected",
                "tool": name,
                "error": str(exc),
            }
        except Exception as exc:
            return {
                "status": "unknown" if tool.action else "unavailable",
                "tool": name,
                "error": type(exc).__name__,
            }

    def message_channel(self, turn, value):
        value = re.sub(r"^#?msg_", "", value.strip())
        if not value.isdigit():
            raise ValueError("Use a full msg_ID")
        sources = [turn.event, *turn.history, *turn.supplemental, *turn.cross_sources]
        if turn.reply_source:
            sources.append(turn.reply_source)
        event = next((e for e in sources if e.id == int(value) and e.guild_id == turn.event.guild_id), None)
        stored = self.db.connection.execute(
            "SELECT channel_id FROM events WHERE id=? AND guild_id=? AND deleted=0",
            (value, str(turn.event.guild_id)),
        ).fetchone()
        if not event and not stored:
            raise PermissionError("Message is not in this turn's known sources")
        return self.access.channel(
            turn, event.channel_id if event else int(stored["channel_id"])
        )

    async def message(self, turn, value):
        channel = self.message_channel(turn, value)
        value = re.sub(r"^#?msg_", "", value.strip())
        return await channel.fetch_message(int(value))

    async def dispatch(self, turn, name, arguments):
        from . import (
            discovery,
            discord_queries,
            memory_tools,
            social_actions,
            math_actions,
            web_tools,
        )

        modules = {
            "discovery": discovery,
            "discord": discord_queries,
            "memory": memory_tools,
            "social": social_actions,
            "math": math_actions,
            "web": web_tools,
        }
        return await modules[REGISTRY[name].group].run(self, turn, name, arguments)

    async def send_aux(self, turn, channel, text, *, validate):
        from sayuki_bot.conversation.models import Turn
        from sayuki_bot.delivery.service import Delivery, DeliveryStopped
        from sayuki_bot.tools.protocol import auxiliary_parts
        from types import SimpleNamespace

        child = Turn(
            turn.event, SimpleNamespace(channel=channel), [], id=turn.id + ":aux:" + uuid4().hex
        )
        parts = auxiliary_parts(text)

        def check():
            if turn.cancelled:
                raise DeliveryStopped("cancelled")
            validate()

        try:
            await Delivery(self.config, self.db, self.bot).send_parts(child, parts, check=check)
        finally:
            turn.delivery_uncertain |= child.delivery_uncertain
            turn.aux_sent.extend(
                {"channel_id": str(channel.id), "text": text, "delivery_id": child.id}
                for text in child.sent
            )
        return "messages sent"
