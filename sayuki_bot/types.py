"""Provider-independent chat objects.

The bot consumes messages from more than Discord (for example ASR output from
the voice receiver).  These objects provide the small, stable surface that
the application pipeline needs without manufacturing ``discord.Message``
instances, which Discord.py does not support.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from typing import Any, AsyncIterator, Iterable
from types import SimpleNamespace

import discord
from discord.ext import commands

class ChatBot(commands.Bot):
    def __init__(self, command_prefix: str, intents: discord.Intents) -> None:
        super().__init__(command_prefix=command_prefix, intents=intents)
        self.sayuki: SimpleNamespace

@dataclass(frozen=True)
class Content:
    """A message payload that can be extended without changing ``Message``."""

    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class Status:
    TRANSLATION = {
        "online": "在線",
        "idle": "閒置",
        "dnd": "請勿打擾",
        "do_not_disturb": "請勿打擾",
        "offline": "離線",
        "invisible": "隱身/離線",
    }

    def __init__(self, name: str) -> None:
        self.localized_name = self.TRANSLATION.get(name, name)
        self.raw_name = name
        self.name = name

    @classmethod
    def from_discord(cls, status: discord.Status) -> Status:
        return cls(status.name)


class User:
    def __init__(
        self,
        guild_id: int | None,
        user_id: int,
        display_name: str,
        status: Status,
        *,
        bot: bool = False,
        source: Any = None,
        activities: Iterable[Any] = (),
    ) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        self.id = user_id
        self.display_name = display_name
        self.name = display_name
        self.status = status
        self.bot = bot
        self.source = source
        self.activities = tuple(activities)

    @classmethod
    def from_discord(cls, ori_user: discord.Member | discord.abc.User) -> User:
        guild = getattr(ori_user, "guild", None)
        guild_id = getattr(guild, "id", None)
        raw_status = getattr(ori_user, "status", None)
        status = Status.from_discord(raw_status) if raw_status is not None else Status("unknown")
        return cls(
            guild_id,
            ori_user.id,
            ori_user.display_name,
            status,
            bot=ori_user.bot,
            source=ori_user,
            activities=tuple(getattr(ori_user, "activities", ()) or ())
        )


class Room:
    """A chat room backed by an optional provider object and local history."""

    def __init__(
        self,
        room_id: int,
        name: str,
        *,
        guild: Any = None,
        source: Any = None,
        cache_limit: int = 80,
    ) -> None:
        self.id = room_id
        self.name = name
        self.guild = guild
        self.source = source
        self.registry: RoomRegistry | None = None
        self._messages: deque[Message] = deque(maxlen=max(1, cache_limit))
        self._history_loaded = False
        self._history_lock = asyncio.Lock()

    @classmethod
    def from_discord(
        cls,
        channel: Any,
        registry: RoomRegistry | None = None,
    ) -> Room:
        if registry is not None:
            return registry.get_or_create(channel)
        return cls(
            channel.id,
            getattr(channel, "name", str(channel.id)),
            guild=getattr(channel, "guild", None),
            source=channel,
        )

    async def get_messages(self, current: Message | None = None) -> list[Message]:
        """Load provider history once, then append new messages without duplicates."""
        async with self._history_lock:
            if not self._history_loaded:
                if self.source is not None:
                    async for old_message in self.source.history(
                        limit=self._messages.maxlen,
                        oldest_first=True,
                    ):
                        self._append(Message.from_discord(old_message, registry=self.registry))
                self._history_loaded = True

            if current is not None:
                self._append(current)
            return list(self._messages)

    def _append(self, message: Message) -> None:
        if any(existing.id == message.id for existing in self._messages):
            return
        self._messages.append(message)

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        if self.source is None:
            raise RuntimeError(f"Room {self.id} has no provider channel")
        return await self.source.send(*args, **kwargs)

    def history(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        if self.source is None:
            raise RuntimeError(f"Room {self.id} has no provider channel")
        return self.source.history(*args, **kwargs)

    def typing(self) -> Any:
        if self.source is None:
            raise RuntimeError(f"Room {self.id} has no provider channel")
        return self.source.typing()


class RoomRegistry:
    """Provides stable Room instances for provider/guild/channel identities."""

    def __init__(self, cache_limit: int = 80) -> None:
        self.cache_limit = max(1, cache_limit)
        self._rooms: dict[tuple[str, int | None, int], Room] = {}

    def get_or_create(self, channel: Any) -> Room:
        guild = getattr(channel, "guild", None)
        key = ("discord", getattr(guild, "id", None), channel.id)
        room = self._rooms.get(key)
        if room is None:
            room = Room(
                channel.id,
                getattr(channel, "name", str(channel.id)),
                guild=guild,
                source=channel,
                cache_limit=self.cache_limit,
            )
            room.registry = self
            self._rooms[key] = room
        else:
            room.source = channel
            room.guild = guild
            room.name = getattr(channel, "name", room.name)
        return room


class Message:
    """A provider-independent message consumed by the bot pipeline."""

    _ids = count(1)

    def __init__(
        self,
        author: User,
        room: Room,
        contents: Iterable[Content],
        *,
        message_id: int | None = None,
        created_at: datetime | None = None,
        source: discord.Message | None = None,
        reference: Any = None,
        mentions: Iterable[User] = (),
        attachments: Iterable[Any] = (),
        clean_content: str | None = None,
        embeds: Iterable[Any] = (),
        components: Iterable[Any] = (),
        poll: Any = None,
        thread: Any = None,
    ) -> None:
        self.id = message_id if message_id is not None else -next(self._ids)
        self.author = author
        self.room = room
        self.channel = room
        self.contents = tuple(contents)
        self.created_at = created_at or discord.utils.utcnow()
        self.source = source
        self.message = source
        self.reference = reference
        self.mentions = list(mentions)
        self.attachments = list(attachments)
        self._clean_content = clean_content
        self.embeds = list(embeds)
        self.components = list(components)
        self.poll = poll
        self.thread = thread
        self.type = getattr(source, "type", None)
        self.guild = room.guild

    @property
    def content(self) -> str:
        return "\n".join(part.text for part in self.contents if part.text).strip()

    @property
    def clean_content(self) -> str:
        return self._clean_content if self._clean_content is not None else self.content

    async def reply(self, *args: Any, **kwargs: Any) -> Any:
        if self.source is not None:
            return await self.source.reply(*args, **kwargs)
        return await self.channel.send(*args, **kwargs)

    @classmethod
    def from_discord(
        cls,
        ori_msg: discord.Message,
        *,
        registry: RoomRegistry | None = None,
    ) -> Message:
        return cls(
            author=User.from_discord(ori_msg.author),
            room=Room.from_discord(ori_msg.channel, registry=registry),
            contents=(Content("text", ori_msg.content),),
            message_id=ori_msg.id,
            created_at=ori_msg.created_at,
            source=ori_msg,
            reference=ori_msg.reference,
            mentions=(User.from_discord(user) for user in ori_msg.mentions),
            attachments=ori_msg.attachments,
            clean_content=ori_msg.clean_content,
            embeds=ori_msg.embeds,
            components=ori_msg.components,
            poll=getattr(ori_msg, "poll", None),
            thread=getattr(ori_msg, "thread", None),
        )

    @classmethod
    def voice(cls, author: User, room: Room, text: str, *, pcm_data: bytes = b"") -> Message:
        ret = cls(
            author,
            room,
            (Content("voice_transcript", text, {"pcm_size": len(pcm_data)}),)
        )
        ret.type = "voice_transcript"
        return ret
