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


NAP_LIMIT = timedelta(hours=3)
WAKE_LATEST = "12:00"


def _clock(text):
    match = re.fullmatch(r"(\d{1,2})[:：](\d{2})", text)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        return None
    from datetime import time

    return time(int(match[1]), int(match[2]))


def sleep_plan(policy, now, until_text, wake_text, status):
    """Validate a SLEEP request against the daily schedule; pure, no side effects.

    Returns {"apply": {...}, "message": str} or {"status": "ignored", "reason": str}
    (a nap running into daily sleep needs her choice). Raises ValueError.
    """
    from datetime import datetime as _dt, time
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(policy.timezone)
    local = now.astimezone(zone).replace(tzinfo=None)
    end_date, win_start, win_end = policy.next_window(now)
    in_daily = policy.daily_sleeping(now)
    until = None
    if until_text:
        if in_daily:
            raise ValueError("現在已經是日常睡眠時間；只能用wake_time調整這次的起床時間")
        minutes = re.fullmatch(r"(\d{1,3})\s*(?:分鐘|分|min|m)?", until_text)
        clock = _clock(until_text)
        if minutes:
            until = local + timedelta(minutes=int(minutes[1]))
        elif clock:
            until = _dt.combine(local.date(), clock)
            if until <= local:
                until += timedelta(days=1)
        else:
            raise ValueError("until要填醒來時間HH:MM或分鐘數")
        if until <= local:
            raise ValueError("醒來時間要在現在之後")
        if until - local > NAP_LIMIT:
            raise ValueError(f"小睡最長3小時，最晚只能睡到{local + NAP_LIMIT:%H:%M}")
    wake = None
    if wake_text:
        wake = _clock(wake_text)
        wake_at = _dt.combine(end_date, wake) if wake else None
        if (
            wake is None
            or wake_at <= win_start
            or wake_at <= local
            or wake > time.fromisoformat(WAKE_LATEST)
        ):
            raise ValueError(
                f"wake_time要填HH:MM，在{win_start:%H:%M}睡著之後、{WAKE_LATEST}之前，且晚於現在"
            )
    if until is None and wake is None:
        raise ValueError("至少要填until（小睡醒來時間）或wake_time（今晚起床時間）")
    if until is not None and until > win_start and wake is None:
        return {
            "status": "ignored",
            "reason": (
                f"還沒去睡：小睡到{until:%H:%M}會接上日常睡眠（{win_start:%H:%M}開始），"
                f"照原本作息會一路睡到{win_end:%H:%M}才醒。可以選擇："
                f"1) 把醒來時間改到{win_start:%H:%M}之前；"
                f"2) 照睡，並用wake_time調整今晚的起床時間（只限今晚，最晚{WAKE_LATEST}），"
                f"例如[[SLEEP: {until_text} | {until:%H:%M}]]就是{until:%H:%M}起床。"
            ),
        }
    nap_until = min(until, win_start) if until is not None else None
    apply = {"wake_override": (end_date, wake) if wake else None}
    if nap_until is not None:
        apply["nap_until"] = nap_until.replace(tzinfo=zone).astimezone(timezone.utc)
        apply["nap_status"] = status[:128]
    parts = []
    if nap_until is not None and until > win_start:
        parts.append(f"去睡了，小睡接上日常睡眠，{wake:%H:%M}起床")
    elif nap_until is not None:
        parts.append(f"去小睡了，預計{nap_until:%H:%M}醒來")
    if wake and not (nap_until is not None and until > win_start):
        parts.append(f"{'這次' if in_daily else '今晚'}起床時間改為{wake:%H:%M}（只限這一次）")
    return {"apply": apply, "message": "；".join(parts)}


def reaction_emoji(bot, turn, value):
    """Accept <:name:id>, a bare ID, :name: or name for custom emoji; Unicode as is.

    Discord rejects a bare custom-emoji ID (10014 Unknown Emoji), so resolve it
    before the action receipt is claimed.
    """
    text = value.strip()
    custom = re.fullmatch(r"<a?:(\w+):(\d+)>", text)
    identifier = int(custom[2]) if custom else int(text) if text.isdigit() else None
    name = None if identifier else re.fullmatch(r":?([\w~-]+):?", text, flags=re.ASCII)
    if identifier is None and name is None:
        return text  # Unicode emoji
    guild = bot.get_guild(turn.event.guild_id)
    loose = lambda s: re.sub(r"[\s_\-]+", "", s).casefold()
    found = next(
        (
            e
            for e in (guild.emojis if guild else ())
            if (e.id == identifier if identifier else loose(e.name) == loose(name[1]))
        ),
        None,
    )
    if found is None and identifier:
        found = bot.get_emoji(identifier)  # emoji from another server the bot is in
    if found is None or not getattr(found, "available", True):
        raise ValueError(f"找不到可用的表情「{text}」；先用GUILD_ASSET_SEARCH查本群表情")
    return f"{found.name}:{found.id}"


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
    if name == "REACT":
        arguments["emoji"] = reaction_emoji(executor.bot, turn, arguments["emoji"])
    if name == "SEND_CHANNEL_MESSAGE":
        # Destination scope is known before anything is sent; rejecting here
        # keeps a denied channel a clean non-write instead of an unknown receipt.
        target = executor.access.channel(turn, int(arguments["channel_id"]))
        if not target.permissions_for(executor.access.requester(turn)).send_messages:
            raise PermissionError("Cannot send in destination")


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
    if name == "SLEEP":
        policy = executor.config.sleep
        plan = sleep_plan(
            policy, datetime.now(timezone.utc), a["until"], a["wake_time"], a["status"]
        )
        if "apply" not in plan:
            return plan
        state = policy.state
        if "nap_until" in plan["apply"]:
            state.nap_until = plan["apply"]["nap_until"]
            state.nap_status = plan["apply"]["nap_status"]
        if plan["apply"]["wake_override"] or "nap_until" not in plan["apply"]:
            state.wake_override = plan["apply"]["wake_override"]
        executor.db.set_state("sleep", state.to_json())
        now = datetime.now(timezone.utc)
        if policy.sleeping(now):
            await executor.bot.change_presence(
                status=discord.Status.idle, activity=discord.Game(policy.status_text(now))
            )
        return plan["message"]
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
