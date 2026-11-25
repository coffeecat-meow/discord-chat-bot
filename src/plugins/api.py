"""Stable contracts shared by the bot core and dynamically loaded plugins.

Plugins register model-callable ``Tool`` objects through the runtime, submit
``InboundEvent`` instances, and attach each event to a ``Route``. Routes let
non-Discord sources use the conversation engine while owning their own output
delivery callback.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, TypeAlias

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
ToolArguments: TypeAlias = dict[str, str]


@dataclass(frozen=True)
class Route:
    """Identifies one plugin conversation and where its replies are delivered.

    ``session_id`` should remain stable for the lifetime of the external
    conversation. ``channel_id`` is a synthetic conversation key, not
    necessarily a Discord channel ID. ``deliver`` receives each visible
    response chunk produced by the bot.
    """
    plugin: str
    session_id: str
    guild_id: int
    channel_id: int
    deliver: Callable[[str], Awaitable[None]]

    @property
    def key(self) -> tuple[str, str]:
        """Unique route identity; sessions from different plugins stay apart."""
        return self.plugin, self.session_id


@dataclass(frozen=True)
class InboundEvent:
    """An external message submitted to the normal bot conversation pipeline."""
    route: Route
    author_id: int
    author_name: str
    text: str
    created_at: datetime
    event_id: int | None = None
    facts: tuple[str, ...] = ()
    source: object | None = None
    # Plugins may run the same admission policy as native messages and pass
    # its result here; ordinary integrations default to an accepted plugin turn.
    reason: str = "plugin"


class Plugin(ABC):
    """Base class for a loadable plugin.

    A module under ``src/plugins/<name>`` exposes ``create_plugin(bot)``.
    The manager calls ``register`` during startup, then ``start`` after the bot
    connects and ``stop`` during shutdown. Plugins may register tools and
    submit events through the runtime object passed to ``register``.
    """
    name: str = ""

    def __init__(self, bot: object) -> None:
        """Store the host bot; subclasses may extend initialization."""
        self.bot = bot

    @abstractmethod
    def register(self, runtime: object) -> None: ...
    """Register tools and other synchronous hooks before turns are processed."""

    async def start(self) -> None:
        """Start plugin resources after the bot is ready; override if needed."""

    async def stop(self) -> None:
        """Release plugin resources during bot shutdown; override if needed."""
