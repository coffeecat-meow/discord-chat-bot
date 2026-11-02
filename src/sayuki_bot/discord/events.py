import re

from sayuki_bot.conversation.models import Event
from .stickers import records


def bot_role_ids(message, bot_id):
    """The bot's own managed integration role; pinging it counts as @Sayuki."""
    guild = message.guild
    role = getattr(guild, "self_role", None)
    ids = {role.id} if role is not None else set()
    for role in getattr(guild, "roles", ()):
        tags = getattr(role, "tags", None)
        if tags is not None and getattr(tags, "bot_id", None) == bot_id:
            ids.add(role.id)
    return ids


def event_from_message(message, bot_id):
    content = message.content
    mentioned = any(x.id == bot_id for x in message.mentions)
    pinged_roles = set(getattr(message, "raw_role_mentions", ())) & bot_role_ids(message, bot_id)
    if pinged_roles:
        mentioned = True
        # Downstream text checks and the model only know <@bot_id>.
        for role_id in pinged_roles:
            content = content.replace(f"<@&{role_id}>", f"<@{bot_id}>")
    return Event(
        message.id,
        message.guild.id,
        message.channel.id,
        message.author.id,
        message.author.display_name,
        content,
        message.created_at,
        mentioned,
        message.reference.message_id if message.reference else None,
        tuple(
            {"filename": a.filename, "url": a.url, "content_type": a.content_type}
            for a in message.attachments
        )
        + records(message),
    )


def readable_mentions(text, guild):
    """Turn user/role pings into plain "@name" text so repeating never pings."""

    def user(match):
        member = guild.get_member(int(match.group(1))) if guild else None
        return f"@{member.display_name}" if member else "@某人"

    def role(match):
        found = guild.get_role(int(match.group(1))) if guild else None
        return f"@{found.name}" if found else "@某個身分組"

    text = re.sub(r"<@&(\d+)>", role, text)
    return re.sub(r"<@!?(\d+)>", user, text)
