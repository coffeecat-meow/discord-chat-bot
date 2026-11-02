from sayuki_bot.conversation.models import Event
from .stickers import records


def event_from_message(message, bot_id):
    return Event(
        message.id,
        message.guild.id,
        message.channel.id,
        message.author.id,
        message.author.display_name,
        message.content,
        message.created_at,
        any(x.id == bot_id for x in message.mentions),
        message.reference.message_id if message.reference else None,
        tuple(
            {"filename": a.filename, "url": a.url, "content_type": a.content_type}
            for a in message.attachments
        )
        + records(message),
    )
