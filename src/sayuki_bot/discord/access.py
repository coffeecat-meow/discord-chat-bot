"""Live Discord access checks, including source-to-destination disclosure scope."""

from __future__ import annotations


def can_read(channel, member):
    if channel is None or member is None:
        return False
    permissions = channel.permissions_for(member)
    if not permissions.view_channel or not permissions.read_message_history:
        return False
    if getattr(channel, "is_private", lambda: False)():
        if not permissions.manage_threads and not channel.get_member(member.id):
            return False
    return True


class Access:
    def __init__(self, bot, config):
        self.bot, self.config = bot, config

    def requester(self, turn):
        guild = self.bot.get_guild(turn.event.guild_id)
        candidate = getattr(turn.source, "author", None) or getattr(turn.source, "user", None)
        return (
            candidate
            if getattr(getattr(candidate, "guild", None), "id", None) == turn.event.guild_id
            else guild.get_member(turn.event.author_id)
            if guild
            else None
        )

    def channel(self, turn, channel_id):
        if getattr(turn, "plugin_route", None) is not None and int(channel_id) == turn.event.channel_id:
            return turn.source.channel
        trusted = getattr(self.bot, "runtime_guilds", None)
        if trusted is not None and turn.event.guild_id not in trusted:
            raise PermissionError("Guild is no longer trusted")
        guild = self.bot.get_guild(turn.event.guild_id)
        channel = guild.get_channel_or_thread(int(channel_id)) if guild else None
        member = self.requester(turn)
        if not channel or not can_read(channel, member) or not can_read(channel, guild.me):
            raise PermissionError("Channel is unavailable or not readable")
        shared = set(self.config.get("memory", "shared_channel_ids", []))
        if getattr(turn, "cross_sources", None):
            from sayuki_bot.context.cross_channel import validate

            validate(self.bot, turn, guild, member, shared)
        if (
            int(channel_id) != turn.event.channel_id
            and not {int(channel_id), turn.event.channel_id} <= shared
        ):
            raise PermissionError("Cross-channel disclosure is not configured for this pair")
        return channel

    def allowed_channels(self, turn):
        result = [turn.event.channel_id]
        for channel_id in self.config.get("memory", "shared_channel_ids", []):
            try:
                channel = self.channel(turn, channel_id)
                if channel.id not in result:
                    result.append(channel.id)
            except PermissionError:
                pass
        return result

    def member(self, turn, user_id):
        guild = self.bot.get_guild(turn.event.guild_id)
        member = guild.get_member(int(user_id)) if guild else None
        if member is None:
            raise PermissionError("Target must be a current member of this guild")
        return member

    def auxiliary(self, turn, target, name, args):
        """Recheck the original authority and actual destination before each chunk."""
        self.authorize(turn, name, args)
        if name == "DM_USER":
            self.member(turn, args["user_id"])
            return
        if name == "THREAD":
            # Only the newly created child of this source channel gets this
            # exception. An arbitrary existing thread still needs shared scope.
            if (
                getattr(target, "parent_id", None) != turn.event.channel_id
                or target.guild.id != turn.event.guild_id
            ):
                raise PermissionError("Unexpected created thread destination")
            current = target
            permission = "send_messages_in_threads"
        else:
            current = self.channel(turn, int(args["channel_id"]))
            if target.id != current.id:
                raise PermissionError("Destination changed")
            permission = (
                "send_messages_in_threads"
                if getattr(current, "parent_id", None)
                else "send_messages"
            )
        for member in (self.requester(turn), current.guild.me):
            if not can_read(current, member) or not getattr(
                current.permissions_for(member), permission, False
            ):
                raise PermissionError("Cannot send in destination")

    def authorize(self, turn, name, args):
        if getattr(turn, "plugin_route", None) is not None:
            from sayuki_bot.tools.registry import REGISTRY
            if not REGISTRY[name].route_safe:
                raise PermissionError("This tool is unavailable from a plugin virtual route")
            return
        channel = self.channel(turn, turn.event.channel_id)
        member = self.requester(turn)
        is_admin = turn.event.author_id in self.config.admins
        if name in {"VOICE_JOIN", "VOICE_LEAVE"}:
            if not is_admin:
                raise PermissionError("Voice controls are admin-only")
            if not getattr(self.bot, "voice_plugin", None):
                raise PermissionError("Voice plugin is disabled")
        if name == "SEND_STICKER":
            from .stickers import guild_sticker

            guild_sticker(self.bot, turn, args["sticker_id"])
        if name == "BUTTON_UI" and args.get("recipient_id"):
            self.member(turn, args["recipient_id"])
        required = {"NICKNAME": "manage_nicknames", "SERVER_EVENT": "manage_events"}.get(name)
        if required and not is_admin and not getattr(member.guild_permissions, required, False):
            raise PermissionError(f"Missing requester permission: {required}")
        if name == "DM_USER" and args["user_id"] != str(turn.event.author_id) and not is_admin:
            if not (
                member.guild_permissions.moderate_members
                or member.guild_permissions.manage_messages
            ):
                raise PermissionError("Cannot direct the bot to DM another member")
        if name in {"MUTE", "NICKNAME"}:
            target = self.member(turn, args["user_id"])
            guild = channel.guild
            if target.id == guild.owner_id or (name == "MUTE" and target.bot):
                raise PermissionError("Protected member target")
            if guild.me.top_role <= target.top_role:
                raise PermissionError("Bot role is not above target")
            if (
                name == "NICKNAME"
                and not is_admin
                and member.id != guild.owner_id
                and member.id != target.id
                and member.top_role <= target.top_role
            ):
                raise PermissionError("Requester role is not above target")
        # Bot's own permissions are additionally enforced by Discord at execution.
