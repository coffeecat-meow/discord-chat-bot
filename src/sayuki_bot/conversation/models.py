from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .continuation import ContinuationState


@dataclass(frozen=True)
class Event:
    id: int
    guild_id: int
    channel_id: int
    author_id: int
    author_name: str
    text: str
    created_at: datetime
    direct_mention: bool = False
    reply_to: int | None = None
    attachments: tuple[dict, ...] = ()


@dataclass
class Turn:
    event: Event
    source: Any
    history: list[Event]
    reason: str = "message"
    id: str = field(default_factory=lambda: uuid4().hex)
    cancelled: bool = False
    sent: list[str] = field(default_factory=list)
    loaded_tools: set[str] = field(default_factory=set)
    seen_calls: set[str] = field(default_factory=set)
    required_response: str | None = None
    completion: Any = None
    bot_id: int | None = None
    reason_facts: tuple[str, ...] = ()
    reply_source: Event | None = None
    reply_status: str | None = None
    context_seen_ids: set[int] = field(default_factory=set)
    supplemental: list[Event] = field(default_factory=list)
    merge_chars: int = 0
    merge_count: int = 0
    merge_omitted: int = 0
    interaction_context: dict | None = None
    aux_sent: list[dict] = field(default_factory=list)
    delivery_uncertain: bool = False
    participation: dict | None = None
    cross_sources: list[Event] = field(default_factory=list)
    cross_context: dict | None = None
    empty_retries: int = 0
    verification: dict | None = None
    continuation: ContinuationState | None = None


@dataclass(frozen=True)
class Generation:
    text: str
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
