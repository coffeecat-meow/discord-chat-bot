from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import discord


@dataclass
class Request:
    messages: list
    interaction_obj: Any
    is_proactive: bool = False
    prefix_text: str = ""
    reply_targets: dict[str, Any] = field(default_factory=dict)
    image_targets: dict[str, list[str]] = field(default_factory=dict)
    image_target_message_ids: dict[str, int] = field(default_factory=dict)
    image_description_cache: dict[int, str] = field(default_factory=dict)
    image_description_cache_times: dict[int, Any] = field(default_factory=dict)
    user_name: str = ""
    target_user_id: int | None = None
    target_user_name: str = ""
    target_channel_id: int | None = None
    target_channel_name: str = ""
    target_guild_id: int | None = None
    target_guild_name: str = ""
    trigger_message_id: int | None = None
    attention_reason: str = ""
    original_message: str = ""
    allow_reminders: bool = True
    is_interaction: bool = field(init=False)

    def __post_init__(self) -> None:
        self.is_interaction = isinstance(self.interaction_obj, discord.Interaction)
