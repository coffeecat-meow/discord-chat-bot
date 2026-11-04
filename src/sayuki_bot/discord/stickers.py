"""Sticker metadata carried in the existing typed media list (no schema change)."""

import re


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


def _loose(text):
    return re.sub(r"[\s_\-:]+", "", str(text)).casefold()


def guild_sticker(bot, turn, identifier):
    """Find a current-guild sticker by numeric ID or by name."""
    guild = bot.get_guild(turn.event.guild_id)
    text = str(identifier).strip()
    stickers = guild.stickers if guild else ()
    if text.isdigit():
        found = next((s for s in stickers if s.id == int(text)), None)
    else:
        found = next((s for s in stickers if _loose(s.name) == _loose(text)), None)
    if found is None or not getattr(found, "available", True):
        emoji = next(
            (
                e
                for e in (guild.emojis if guild else ())
                if (text.isdigit() and e.id == int(text)) or _loose(e.name) == _loose(text)
            ),
            None,
        )
        if emoji is not None:
            raise ValueError(
                f"「{emoji.name}」是表情符號不是貼圖；要顯示就把 {emoji} 寫進回覆文字，"
                "要看圖用VIEW_EMOJI"
            )
        raise ValueError("Sticker is not an available sticker of the current guild")
    return found
