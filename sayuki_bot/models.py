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
    user_name: str = ""
    original_message: str = ""
    is_interaction: bool = field(init=False)

    def __post_init__(self) -> None:
        self.is_interaction = isinstance(self.interaction_obj, discord.Interaction)
