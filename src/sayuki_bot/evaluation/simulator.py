"""Local engine replay with timestamped sources and optional real memory/read tools."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from sayuki_bot.context.compiler import ContextCompiler
from sayuki_bot.conversation.engine import Engine
from sayuki_bot.api import Event, Generation, Turn
from sayuki_bot.delivery.service import Delivery
from sayuki_bot.memory.service import MemoryService
from sayuki_bot.tools.registry import REGISTRY


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)

    async def generate(self, *args, **kwargs):
        return Generation(next(self.responses))


class FixtureTools:
    def __init__(self, transcript, fixtures):
        self.transcript, self.fixtures = transcript, fixtures

    async def execute(self, turn, name, body):
        self.transcript.append({"kind": "tool", "name": name, "body": body})
        if name == "TOOL_HELP":
            from sayuki_bot.tools.discovery import run

            return await run(self, turn, name, REGISTRY[name].parse(body))
        values = self.fixtures.get(name, [])
        if not values:
            return {
                "status": "unavailable",
                "reason": "No simulated result configured; no external tool was executed.",
            }
        return values.pop(0)


def parse_time(value, default):
    parsed = datetime.fromisoformat(value) if value else default
    if parsed.tzinfo is None:
        raise ValueError("Simulation timestamps require a timezone offset")
    return parsed


async def simulate(config, database, scenario, model):
    from copy import deepcopy
    from sayuki_bot.discord.access import Access
    from sayuki_bot.evaluation.environment import LocalChannel, LocalGuild, LocalTools
    from sayuki_bot.memory.governance import Governance

    config = deepcopy(config)
    transcript = []
    now = parse_time(scenario.get("now"), datetime.now(timezone.utc))
    simulation_clock = now
    guild = LocalGuild(int(scenario.get("guild_id", 30)))
    local_tools = scenario.get("local_tools", False)
    config.sections.setdefault("memory", {})["shared_channel_ids"] = scenario.get(
        "shared_channel_ids", []
    )
    source_events = {}
    history = []

    def make_event(raw, identifier, default_channel=20):
        raw = {"text": raw} if isinstance(raw, str) else raw
        author = int(raw.get("author_id", 10))
        name = raw.get("author_name", "朋友")
        guild.add_member(author, name)
        channel_id = int(raw.get("channel_id", default_channel))
        if channel_id not in guild.channels:
            readers = scenario.get("channel_readers", {}).get(str(channel_id))
            guild.channels[channel_id] = LocalChannel(
                guild, channel_id, transcript, readers=readers
            )
        return Event(
            int(raw.get("id", identifier)),
            guild.id,
            channel_id,
            author,
            name,
            raw["text"],
            parse_time(raw.get("time"), now),
            raw.get("direct_mention", True),
            raw.get("reply_to"),
            tuple(raw.get("attachments", [])),
        )

    def store(event, *, live=True):
        database.record_event(event)
        source_events[event.id] = event
        if live:
            guild.channels[event.channel_id].add(event)

    for category in ("stored_events", "history"):
        for i, raw in enumerate(scenario.get(category, [])):
            event = make_event(raw, (400000 if category == "stored_events" else 100000) + i)
            store(event, live=not isinstance(raw, dict) or raw.get("live", True))
            if category == "history":
                history.append(event)
    memory = MemoryService(database, clock=lambda: simulation_clock)
    for item in scenario.get("memories", []):
        memory.update(
            source_events[int(item["source_id"])],
            int(item["subject_id"]),
            item.get("kind", "profile"),
            item["key"],
            item["value"],
        )
    for user_id in scenario.get("memory_disabled_users", []):
        Governance(database).set_enabled(guild.id, int(user_id), False)
    bot = SimpleNamespace(
        user=guild.me,
        runtime_guilds={guild.id},
        transcript=transcript,
        get_guild=lambda identifier: guild if identifier == guild.id else None,
    )
    access = Access(bot, config)
    for index, batch in enumerate(scenario.get("extraction_batches", [])):
        from sayuki_bot.memory.background import BackgroundMemory

        simulation_clock = parse_time(batch.get("at"), now)
        batch_events = []
        for offset, raw in enumerate(batch["events"]):
            item = make_event(raw, 500000 + index * 1000 + offset)
            store(item)
            batch_events.append(item)
        if not batch_events:
            raise ValueError("Extraction batch must contain source events")
        worker = BackgroundMemory(
            config, memory, model, clock=lambda: simulation_clock, access=access
        )
        checkpoint = Turn(batch_events[-1], None, batch_events)
        success = await worker.extract(checkpoint)
        audit = database.connection.execute(
            "SELECT data FROM traces WHERE turn_id=? AND stage='memory_extraction' ORDER BY id DESC LIMIT 1",
            (checkpoint.id,),
        ).fetchone()
        transcript.append(
            {
                "kind": "memory_checkpoint",
                "success": success,
                "audit": json.loads(audit["data"]) if audit else None,
                "memories": memory.recall(guild.id, list(guild.channels), limit=50),
                "summary": memory.summary(
                    guild.id,
                    checkpoint.event.channel_id,
                    24,
                    allowed_channels=access.allowed_channels(checkpoint),
                ),
            }
        )
        await worker.close()
    simulation_clock = now
    executor = (
        LocalTools(bot, config, database, memory, model, access)
        if local_tools
        else FixtureTools(transcript, deepcopy(scenario.get("tool_results", {})))
    )
    engine = Engine(
        config,
        ContextCompiler(config, memory),
        model,
        executor,
        Delivery(config, database, bot),
        access,
    )
    try:
        for i, raw in enumerate(scenario.get("messages", [])):
            event = make_event(raw, 200000 + i)
            simulation_clock = event.created_at
            store(event)
            channel = guild.channels[event.channel_id]
            transcript.append(
                {
                    "kind": "input",
                    "text": event.text,
                    "channel_id": event.channel_id,
                    "time": event.created_at.isoformat(),
                }
            )
            future = asyncio.get_running_loop().create_future()
            turn = Turn(
                event,
                SimpleNamespace(channel=channel, author=guild.get_member(event.author_id)),
                [e for e in history if e.channel_id == event.channel_id],
                completion=future,
            )
            if not engine.submit(turn):
                raise RuntimeError("Simulation queue full")
            outcome = await future
            transcript.append({"kind": "outcome", **outcome})
            history.append(event)
            for j, text in enumerate(turn.sent):
                reply = Event(
                    300000 + i * 1000 + j,
                    guild.id,
                    event.channel_id,
                    99,
                    "紗月",
                    text,
                    event.created_at,
                )
                history.append(reply)
                store(reply)
    finally:
        await engine.close()
    return transcript
