from __future__ import annotations

import importlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, cast

from plugins.api import InboundEvent, JsonValue, Route, ToolArguments
from sayuki_bot.api import Config, Event, Tool, Turn
from sayuki_bot.tools.registry import register

from .api import Plugin

if TYPE_CHECKING:
    from sayuki_bot.discord.bot import SayukiBot

ToolHandler = Callable[[Turn, str, ToolArguments], Awaitable[JsonValue] | JsonValue]
SubmitResult = Literal["rejected_guild", "queued", "queue_full"]

logger = logging.getLogger(__name__)


class PluginManager:
    """Loads configured plugin modules and owns virtual conversation routes."""
    def __init__(self, bot: SayukiBot) -> None:
        self.bot = bot
        self.plugins: dict[str, Plugin] = {}
        self.routes: dict[tuple[str, str], Route] = {}
        self.handlers: dict[str, ToolHandler] = {}

    @staticmethod
    def validate_config(config: Config, names: Iterable[str]) -> None:
        for name in names:
            if not isinstance(name, str) or not name.replace("_", "").isalnum():
                raise ValueError("plugins.enabled accepts simple module names only")
            module = importlib.import_module(f"plugins.{name}")
            validator = getattr(module, "validate_config", None)
            if validator:
                validator(config)

    @staticmethod
    def startup_checks(config: Config, names: Iterable[str]) -> list[str]:
        errors: list[str] = []
        for name in names:
            module = importlib.import_module(f"plugins.{name}")
            check = getattr(module, "startup_check", None)
            if check:
                errors.extend(check(config) or ())
        return errors

    def add_tool(self, tool: Tool, handler: ToolHandler) -> None:
        register(tool)
        self.handlers[tool.name] = handler

    def register_route(self, route: Route) -> Route:
        if self.bot.runtime_guilds and route.guild_id not in self.bot.runtime_guilds:
            raise PermissionError("Plugin route belongs to an untrusted guild")
        self.routes[route.key] = route
        return route

    def load(self) -> None:
        for name in self.bot.config.get("plugins", "enabled", []):
            if not isinstance(name, str) or not name.replace("_", "").isalnum():
                raise ValueError("plugins.enabled accepts simple module names only")
            module = importlib.import_module(f"plugins.{name}")
            factory = getattr(module, "create_plugin", None)
            if not callable(factory):
                raise ValueError(f"Plugin {name} does not expose create_plugin(bot)")
            plugin = cast(Callable[[object], Plugin], factory)(self.bot)
            if getattr(plugin, "name", name) != name:
                raise ValueError(f"Plugin name mismatch: {name}")
            plugin.register(self)
            self.plugins[name] = plugin
            logger.info("Loaded plugin: %s", name)

    async def start(self) -> None:
        for plugin in self.plugins.values():
            await plugin.start()

    async def stop(self) -> None:
        for plugin in reversed(tuple(self.plugins.values())):
            await plugin.stop()

    async def dispatch(
        self, turn: Turn, name: str, arguments: ToolArguments
    ) -> JsonValue:
        result = self.handlers[name](turn, name, arguments)
        if isinstance(result, Awaitable):
            return await result
        return result

    def submit(self, inbound: InboundEvent) -> SubmitResult:
        route = self.register_route(inbound.route)
        if route.guild_id not in self.bot.runtime_guilds:
            return "rejected_guild"
        event = Event(
            inbound.event_id
            if inbound.event_id is not None
            else -abs(hash((route.key, inbound.created_at))),
            route.guild_id,
            route.channel_id,
            inbound.author_id,
            inbound.author_name,
            inbound.text,
            inbound.created_at,
        )
        source = inbound.source or SimpleNamespace(
            channel=SimpleNamespace(id=route.channel_id), author=SimpleNamespace(id=inbound.author_id)
        )
        turn = Turn(event, source, list(self.bot.history[event.channel_id]), reason="plugin")
        turn.plugin_route, turn.bot_id, turn.reason_facts = route, (self.bot.user.id if self.bot.user else None), tuple(inbound.facts)
        self.bot.history[event.channel_id].append(event)
        return "queued" if self.bot.engine.submit(turn) else "queue_full"
