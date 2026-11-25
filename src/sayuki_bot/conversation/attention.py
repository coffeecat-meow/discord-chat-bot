"""Deterministic participation gates; no extra model for direct conversation."""

from datetime import datetime

from .models import Event
from .participation import candidate_history


class Attention:
    def __init__(self, config):
        self.config = config
        self.last_proactive = {}
        self.echoes = {}

    def decide(
        self,
        event: Event,
        history: list[Event],
        bot_id: int,
        now: datetime,
        *,
        allow_echo: bool = True,
    ) -> str:
        # Gate before every conversational path, including direct echo sends.
        if self.config.sleep.sleeping(now):
            return "mention" if event.direct_mention else "ignore"
        if event.direct_mention:
            return "mention"
        aliases = self.config.get("discord", "aliases", [])
        if any(alias and alias.casefold() in event.text.casefold() for alias in aliases):
            return "name"
        if event.reply_to and any(
            x.id == event.reply_to and x.author_id == bot_id for x in history
        ):
            return "reply"
        window = self.config.get("conversation", "echo_window_seconds", 60)
        self.echoes = {k: v for k, v in self.echoes.items() if (now - v).total_seconds() <= window}
        recent = [
            x
            for x in history
            if 0 <= (now - x.created_at).total_seconds() <= window and x.author_id != bot_id
        ]
        if not any(x.id == event.id for x in recent):
            recent.append(event)
        matching = {x.author_id for x in recent if x.text.strip() == event.text.strip()}
        key = (event.channel_id, event.text.strip())
        if (
            allow_echo
            and self.config.get("conversation", "echo_enabled", True)
            and key[1]
            and len(matching) >= self.config.get("conversation", "echo_min_users", 3)
            and key not in self.echoes
        ):
            self.echoes[key] = now
            return "echo"
        if candidate_history(self.config, event, history, bot_id):
            return "continuation"
        if not self.config.get("conversation", "proactive_enabled", True):
            return "ignore"
        last = self.last_proactive.get(event.channel_id)
        if last and (now - last).total_seconds() < self.config.get(
            "conversation", "proactive_cooldown_seconds", 120
        ):
            return "ignore"
        authors = {x.author_id for x in recent if (now - x.created_at).total_seconds() <= 60}
        if len(authors) >= self.config.get(
            "conversation", "proactive_min_users", 4
        ) and not event.text.startswith("!"):
            self.last_proactive[event.channel_id] = now
            return "proactive"
        return "ignore"

    def facts(self, event, history, bot_id, now):
        """Event-local observations; never use mutable 'last decision' state."""
        facts = []
        if event.direct_mention:
            facts.append("direct_mention：這則訊息直接 @ 你")
        if any(
            alias and alias.casefold() in event.text.casefold()
            for alias in self.config.get("discord", "aliases", [])
        ):
            facts.append("name_in_text：這則訊息提到你的名字")
        if event.reply_to and any(
            x.id == event.reply_to and x.author_id == bot_id for x in history
        ):
            facts.append("reply_to_self：這則訊息回覆了你先前的訊息")
        if candidate_history(self.config, event, history, bot_id):
            facts.append("recent_participation：你近期在此頻道說過話；普通文字可能接續，尚未確認對象")
        return tuple(facts)
