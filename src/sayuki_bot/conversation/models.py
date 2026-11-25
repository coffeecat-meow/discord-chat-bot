from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from plugins.api import Route

if TYPE_CHECKING:
    from .continuation import ContinuationState


@dataclass(frozen=True)
class Event:
    """Normalized, immutable input message consumed by conversation logic.

    Discord messages and plugin supplied events share this shape. IDs and
    channel keys are scoped by guild; plugin routes may use synthetic channel
    keys to isolate their conversation history.
    """
    id: int
    """Source event identifier, unique within its guild/channel context."""
    guild_id: int
    """Owning server or tenant identifier."""
    channel_id: int
    """Conversation key; for plugins this may be a virtual channel key."""
    author_id: int
    """Stable identity of the event author."""
    author_name: str
    """Display name captured when the event was received."""
    text: str
    """Plain text content presented to the conversation engine."""
    created_at: datetime
    """Timestamp of the original input, preferably timezone-aware."""
    direct_mention: bool = False
    reply_to: int | None = None
    attachments: tuple[dict, ...] = ()


@dataclass
class Turn:
    """Mutable state for processing one event through the conversation engine.

    A turn tracks tool calls, generated output, delivery certainty, and any
    contextual material gathered while responding. ``plugin_route`` marks
    turns originating from a plugin and directs visible output to that route.
    """
    event: Event
    """Input event this turn answers."""
    source: Any
    """Platform or plugin object used for permissions and output delivery."""
    history: list[Event]
    """Prior events available to the model for this conversation."""
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
    """True when an output call may have succeeded but its result is unknown."""
    participation: dict | None = None
    cross_sources: list[Event] = field(default_factory=list)
    cross_context: dict | None = None
    empty_retries: int = 0
    verification: dict | None = None
    continuation: ContinuationState | None = None
    debug: dict = field(default_factory=dict)
    """Non-user-facing diagnostics and intermediate state."""
    plugin_route: Route | None = None
    """Optional plugin-owned virtual route for input and reply delivery."""


@dataclass(frozen=True)
class Generation:
    """One model completion and its normalized usage metadata."""
    text: str
    finish_reason: str = "stop"
    usage: dict = field(default_factory=dict)
