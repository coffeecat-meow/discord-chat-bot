"""Tool adapters for the optional voice feature package."""


async def run(executor, turn, name, arguments):
    plugin = getattr(executor.bot, "voice_plugin", None)
    if plugin is None:
        raise RuntimeError("Voice plugin is disabled")
    if name == "VOICE_JOIN":
        member = executor.access.requester(turn)
        channel = getattr(getattr(member, "voice", None), "channel", None)
        if not channel or channel.guild.id != turn.event.guild_id:
            raise ValueError("Join a voice channel first")
        return await plugin.join(turn.event.guild_id, channel.id, turn.event.channel_id)
    if name == "VOICE_LEAVE":
        return await plugin.leave(turn.event.guild_id)
    raise ValueError(f"Unsupported voice tool: {name}")
