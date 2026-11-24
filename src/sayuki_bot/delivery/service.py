from __future__ import annotations

import asyncio
import logging
import random
from time import perf_counter

from sayuki_bot.tools.protocol import visible_segments


logger = logging.getLogger(__name__)


class DeliveryStopped(RuntimeError):
    pass


class Delivery:
    def __init__(self, config, database, bot):
        self.config, self.db, self.bot = config, database, bot

    async def delay(self, turn, channel, text, token):
        if not token:
            return
        values = self.config.sections.get("delivery", {})
        if token == "SPLIT-WAIT":
            async with channel.typing():
                await asyncio.sleep(
                    random.uniform(
                        values.get("wait_typing_min", 0.8), values.get("wait_typing_max", 2.0)
                    )
                )
            await asyncio.sleep(
                random.uniform(values.get("wait_pause_min", 3), values.get("wait_pause_max", 8))
            )
        seconds = min(
            len(text) * values.get("typing_seconds_per_char", 0.1),
            values.get("typing_max_seconds", 2.0),
        )
        async with channel.typing():
            await asyncio.sleep(seconds)

    async def send_parts(self, turn, parts, *, check=None):
        import discord

        route = getattr(turn, "plugin_route", None)
        channel = turn.source.channel
        for text, reply, token in visible_segments(parts):
            for offset in range(0, len(text), 1900):
                if check:
                    check()
                if turn.cancelled:
                    raise DeliveryStopped("cancelled")
                chunk = text[offset : offset + 1900]
                delay_start = perf_counter()
                await self.delay(turn, channel, chunk, token if offset == 0 else None)
                self.db.trace(
                    turn.id, "pacing", {"duration_ms": (perf_counter() - delay_start) * 1000}
                )
                if turn.cancelled:
                    raise DeliveryStopped("cancelled")
                if check:
                    check()
                key = f"{turn.id}:text:{len(turn.sent)}"
                if route is not None:
                    await route.deliver(chunk)
                    self.db.trace(turn.id, "virtual_visible", {"plugin": route.plugin, "chars": len(chunk)})
                    turn.sent.append(chunk)
                    continue
                access = getattr(self.bot, "access", None)
                if access:
                    access.channel(turn, turn.event.channel_id)
                if not self.db.claim(key, turn.id, "message"):
                    raise DeliveryStopped("duplicate or ambiguous delivery")
                kwargs = {
                    "content": chunk,
                    "allowed_mentions": discord.AllowedMentions(
                        everyone=False, roles=False, users=True, replied_user=False
                    ),
                }
                if reply and offset == 0:
                    # A reply may only target known messages in this channel.
                    if not any(
                        e.id == reply and e.channel_id == turn.event.channel_id
                        for e in [
                            turn.event,
                            *turn.history,
                            *turn.supplemental,
                            *([turn.reply_source] if turn.reply_source else []),
                        ]
                    ):
                        self.db.settle(key, "failed", "unknown reply target")
                        raise DeliveryStopped("unknown reply target")
                    kwargs["reference"] = discord.MessageReference(
                        message_id=reply, channel_id=channel.id, fail_if_not_exists=False
                    )
                start = perf_counter()
                try:
                    message = await channel.send(**kwargs)
                except BaseException as exc:
                    turn.delivery_uncertain = True
                    self.db.settle(key, "unknown", type(exc).__name__)
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    logger.warning(
                        "訊息送出失敗 turn=%s channel=%s: %s: %s",
                        turn.id, getattr(channel, "id", "?"), type(exc).__name__, exc,
                    )
                    raise DeliveryStopped("delivery outcome unknown") from exc
                self.db.settle(key, "sent", getattr(message, "id", ""))
                self.db.trace(
                    turn.id,
                    "first_visible" if not turn.sent else "visible_chunk",
                    {"duration_ms": (perf_counter() - start) * 1000, "chars": len(chunk)},
                )
                turn.sent.append(chunk)
                recorder = getattr(self.bot, "record_delivered", None)
                if recorder and getattr(message, "guild", None) is not None:
                    recorder(message)

    async def send_text(self, turn, text):
        from sayuki_bot.tools.protocol import Part

        await self.send_parts(turn, [Part("text", text)])

    async def send_sticker(self, turn, sticker):
        """The tool executor owns the single action receipt; no second claim."""
        import discord
        from sayuki_bot.discord.stickers import guild_sticker

        if turn.cancelled:
            raise DeliveryStopped("cancelled")
        channel = self.bot.access.channel(turn, turn.event.channel_id)
        current = guild_sticker(self.bot, turn, sticker.id)
        message = await channel.send(
            stickers=[current], allowed_mentions=discord.AllowedMentions.none()
        )
        self.db.trace(
            turn.id,
            "first_visible" if not turn.sent else "visible_chunk",
            {"kind": "sticker", "sticker_id": str(current.id), "chars": 0},
        )
        turn.sent.append(f"[sticker:{current.id}]")
        self.bot.record_delivered(message)
        return {
            "message_id": str(message.id),
            "url": message.jump_url,
            "sticker_id": str(current.id),
        }
