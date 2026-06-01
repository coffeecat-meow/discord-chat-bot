from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .config import TW_TZ


@dataclass
class BotStats:
    total_messages: int = 0
    today_messages: int = 0
    last_date: str = ""

    def record_sent_message(self) -> None:
        today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")
        if self.last_date != today_str:
            self.today_messages = 0
            self.last_date = today_str

        self.total_messages += 1
        self.today_messages += 1


@dataclass
class BotState:
    stats: BotStats = field(default_factory=BotStats)
    channel_message_cache: dict[str, deque] = field(default_factory=dict)
    vl_description_cache: dict[int, str] = field(default_factory=dict)
    proactive_cooldowns: dict[int, datetime] = field(default_factory=dict)
    echoed_messages: dict[str, dict[str, datetime]] = field(default_factory=dict)
