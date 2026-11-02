from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4


def event_times(config, arguments):
    from zoneinfo import ZoneInfo

    def date(value):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo(config.sleep.timezone))

    start, end = date(arguments["start"]), date(arguments["end"])
    if end <= start or start <= datetime.now(timezone.utc):
        raise ValueError("Invalid future event interval")
    return start, end


def prepare(executor, turn, name, arguments):
    """Synchronous, side-effect-free checks before claiming an action receipt."""
    if name == "SERVER_EVENT":
        event_times(executor.config, arguments)
    if name == "REACT" and (
        not callable(getattr(turn.source, "add_reaction", None))
        or getattr(turn.source, "id", None) != turn.event.id
    ):
        raise ValueError(
            "REACT requires the original Discord message; this turn has no reaction target"
        )
    if name in {"DM_USER", "SEND_CHANNEL_MESSAGE", "THREAD"} and arguments["content"]:
        from .protocol import auxiliary_parts

        auxiliary_parts(arguments["content"])


async def run(executor, turn, name, a):
    import discord

    event = turn.event
    guild = executor.bot.get_guild(event.guild_id)
    channel = executor.access.channel(turn, event.channel_id)
    if name == "SEND_STICKER":
        from sayuki_bot.delivery.service import Delivery
        from sayuki_bot.discord.stickers import guild_sticker

        sticker = guild_sticker(executor.bot, turn, a["sticker_id"])
        return await Delivery(executor.config, executor.db, executor.bot).send_sticker(
            turn, sticker
        )
    if name == "REACT":
        await turn.source.add_reaction(a["emoji"])
        return "reaction added"
    if name == "MUTE":
        member = executor.access.member(turn, a["user_id"])
        seconds = int(a["seconds"])
        if not 1 <= seconds <= 300:
            raise ValueError("Timeout must be 1–300 seconds")
        await member.timeout(timedelta(seconds=seconds), reason="Sayuki bounded social timeout")
        return "timeout applied"
    if name == "NICKNAME":
        member = executor.access.member(turn, a["user_id"])
        if len(a["nickname"]) > 32:
            raise ValueError("Nickname exceeds 32 characters")
        await member.edit(nick=a["nickname"] or None, reason="Sayuki nickname action")
        return "nickname updated"
    if name == "STATUS":
        await executor.bot.change_presence(activity=discord.Game(a["text"][:128]))
        executor.bot.sayuki_status_until = datetime.now(timezone.utc) + timedelta(minutes=30)
        return "status updated"
    if name == "REMIND":
        minutes = float(a["minutes"])
        if not 0 < minutes <= 525600:
            raise ValueError("Reminder must be within one year")
        identifier = uuid4().hex
        with executor.db.connection:
            executor.db.connection.execute(
                "INSERT INTO reminders(id,guild_id,channel_id,user_id,due_at,content) VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    str(event.guild_id),
                    str(event.channel_id),
                    str(event.author_id),
                    (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(),
                    a["content"][:2000],
                ),
            )
        return {"reminder_id": identifier}
    if name == "DM_USER":
        target = executor.access.member(turn, a["user_id"])
        destination = await target.create_dm()
        return await executor.send_aux(
            turn,
            destination,
            a["content"],
            validate=lambda: executor.access.auxiliary(turn, destination, name, a),
        )
    if name == "SEND_CHANNEL_MESSAGE":
        target = executor.access.channel(turn, int(a["channel_id"]))
        if not target.permissions_for(executor.access.requester(turn)).send_messages:
            raise PermissionError("Cannot send in destination")
        return await executor.send_aux(
            turn,
            target,
            a["content"],
            validate=lambda: executor.access.auxiliary(turn, target, name, a),
        )
    if name == "THREAD":
        thread = await channel.create_thread(
            name=a["name"][:100], type=discord.ChannelType.public_thread
        )
        if a["content"]:
            await executor.send_aux(
                turn,
                thread,
                a["content"],
                validate=lambda: executor.access.auxiliary(turn, thread, name, a),
            )
        return {"id": str(thread.id)}
    if name == "POLL":
        options = [x.strip() for x in re.split("[,，]", a["options"]) if x.strip()]
        minutes = float(a["minutes"])
        poll = discord.Poll(
            question=a["title"][:300],
            duration=timedelta(minutes=minutes),
            multiple=a["multiple"].lower() == "true",
        )
        for text in options:
            poll.add_answer(text=text[:55])
        msg = await channel.send(poll=poll)
        return {
            "url": msg.jump_url,
            "title": poll.question,
            "options": [answer.text for answer in poll.answers],
            "duration_minutes": int(minutes),
            "multiple": poll.multiple,
            "vote_change_allowed_until_close": True,
            "interpretation": "一小時等duration是投票有效期限，不是禁止改票期間；原生投票未結束前可移除選票再投。不能套用BUTTON_UI的每人一次限制。",
        }
    if name == "BUTTON_UI":
        from sayuki_bot.discord.interactions import ChoiceView

        options = [x.strip() for x in re.split("[,，]", a["options"]) if x.strip()]
        if not 1 <= len(options) <= 25:
            raise ValueError("Panel needs 1–25 options")
        if any(len(x) > 80 for x in options) or len(set(options)) != len(options):
            raise ValueError("Panel labels must be unique and at most 80 characters")
        recipient = executor.access.member(turn, a["recipient_id"]) if a["recipient_id"] else None
        title, description = a["title"][:256], a["description"][:4000]
        context = {
            "kind": "button_choice",
            "guild_id": str(event.guild_id),
            "channel_id": str(event.channel_id),
            "title": title,
            "description": description[:1000],
            "description_truncated": len(description) > 1000,
            "requester": {"id": str(event.author_id), "name": event.author_name},
            "recipient": {"id": str(recipient.id), "name": recipient.display_name}
            if recipient
            else None,
            "source": {
                "message_id": f"msg_{event.id}"
                if getattr(turn.source, "id", None) == event.id
                else None,
                "text": event.text[:1000],
                "truncated": len(event.text) > 1000,
            },
        }
        view = ChoiceView(executor.bot, options, title, context=context, source_text=event.text)
        try:
            msg = await channel.send(
                embed=discord.Embed(title=title, description=description), view=view
            )
        except BaseException:
            view.stop()
            raise
        view.bind(msg)
        return {"url": msg.jump_url}
    if name == "SERVER_EVENT":
        start, end = event_times(executor.config, a)
        result = await guild.create_scheduled_event(
            name=a["name"][:100],
            start_time=start,
            end_time=end,
            entity_type=discord.EntityType.external,
            location=a["location"][:100],
            description=a["description"][:1000],
            privacy_level=discord.PrivacyLevel.guild_only,
        )
        return {"id": str(result.id)}
    raise ValueError(f"Unsupported tool: {name}")
