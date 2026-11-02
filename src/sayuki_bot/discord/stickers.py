"""Sticker metadata carried in the existing typed media list (no schema change)."""


def records(message):
    return tuple(
        {
            "kind": "sticker",
            "sticker_id": str(s.id),
            "filename": s.name[:100],
            "url": str(s.url),
            "format": s.format.name,
            "content_type": "application/x-discord-sticker",
        }
        for s in getattr(message, "stickers", ())[:3]
    )


def guild_sticker(bot, turn, identifier):
    guild = bot.get_guild(turn.event.guild_id)
    found = next((s for s in guild.stickers if s.id == int(identifier)), None) if guild else None
    if found is None or not getattr(found, "available", True):
        raise ValueError("Sticker is not an available sticker of the current guild")
    return found
