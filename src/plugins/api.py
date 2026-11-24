"""Public types for plugin tools, input events, and virtual routes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Protocol


@dataclass(frozen=True)
class Route:
    plugin: str
    session_id: str
    guild_id: int
    channel_id: int
    deliver: Callable[[str], Awaitable[None]]

    @property
    def key(self):
        return self.plugin, self.session_id


@dataclass(frozen=True)
class InboundEvent:
    route: Route
    author_id: int
    author_name: str
    text: str
    created_at: datetime
    event_id: int | None = None
    facts: tuple[str, ...] = ()
    source: object | None = None


class Plugin(Protocol):
    name: str
    def register(self, runtime) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
