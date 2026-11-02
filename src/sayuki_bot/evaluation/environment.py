"""In-memory Discord objects for real read/permission/memory code, with no network."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord

from sayuki_bot.tools.executor import ToolExecutor


class LocalChannel:
    def __init__(self, guild, identifier, transcript, *, readers=None):
        self.guild, self.id, self.transcript = guild, identifier, transcript
        self.name = f"模擬頻道-{identifier}"
        self.readers = set(readers) if readers is not None else None
        self.messages = {}

    def permissions_for(self, member):
        readable = self.readers is None or member.id in self.readers
        return SimpleNamespace(view_channel=readable, read_message_history=readable)

    @asynccontextmanager
    async def typing(self):
        yield

    async def send(self, **kwargs):
        self.transcript.append(
            {"kind": "message", "channel_id": self.id, "text": kwargs.get("content", "")}
        )
        return SimpleNamespace(id=900000 + len(self.transcript))

    async def fetch_message(self, identifier):
        if identifier not in self.messages:
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"
            )
        return self.messages[identifier]

    async def history(self, limit=25):
        for message in sorted(self.messages.values(), key=lambda m: m.created_at, reverse=True)[
            :limit
        ]:
            yield message

    def add(self, event):
        self.messages[event.id] = SimpleNamespace(
            id=event.id,
            guild=self.guild,
            channel=self,
            author=self.guild.get_member(event.author_id),
            content=event.text,
            created_at=event.created_at,
            mentions=[],
            reference=SimpleNamespace(message_id=event.reply_to) if event.reply_to else None,
            attachments=[
                SimpleNamespace(
                    filename=a.get("filename", ""),
                    url=a.get("url", ""),
                    content_type=a.get("content_type"),
                )
                for a in event.attachments
            ],
            jump_url=f"https://discord.com/channels/{event.guild_id}/{event.channel_id}/{event.id}",
        )


class LocalGuild:
    def __init__(self, identifier=30):
        self.id = identifier
        self.channels, self.members = {}, {}
        self.me = self.add_member(99, "紗月")

    def add_member(self, identifier, name):
        self.members[identifier] = SimpleNamespace(
            id=identifier, display_name=name, guild=self, bot=identifier == 99
        )
        return self.members[identifier]

    def get_member(self, identifier):
        return self.members.get(identifier)

    def get_channel_or_thread(self, identifier):
        return self.channels.get(identifier)


class LocalTools(ToolExecutor):
    # Everything else stays unavailable. Even fixtures cannot execute external actions.
    SUPPORTED = {
        "TOOL_HELP",
        "MEMORY_SEARCH",
        "LOOKUP_MEMORY",
        "MEM_UPDATE",
        "MEM_REVISE",
        "MEM_OBSERVE",
        "MEM_DELETE",
        "READ_MESSAGE",
        "READ_CHANNEL",
        "CHANNEL_LOOKUP",
        "USER_STATS",
        "VIEW_IMAGE",
    }

    async def dispatch(self, turn, name, arguments):
        if name not in self.SUPPORTED:
            return {
                "status": "unavailable",
                "interpretation": "此工具未接入本機模擬，沒有執行外部動作。",
            }
        if name == "VIEW_IMAGE":
            message = await self.message(turn, arguments["message_id"])
            if any(
                not a.url.startswith("data:image/png;base64,") or len(a.url) > 100000
                for a in message.attachments
                if (a.content_type or "").startswith("image/")
            ):
                raise ValueError("Local vision accepts only bounded synthetic PNG data URLs")
        return await super().dispatch(turn, name, arguments)

    async def execute(self, turn, name, body):
        # Reject unsupported actions before claims or successful receipts can be created.
        if name not in self.SUPPORTED:
            result = {
                "status": "unavailable",
                "tool": name,
                "error": "Not implemented in local simulation",
            }
        else:
            result = await super().execute(turn, name, body)
        self.bot.transcript.append({"kind": "tool", "name": name, "body": body, "result": result})
        return result
