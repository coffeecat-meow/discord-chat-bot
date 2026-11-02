"""Offline 4.0/4.6 assembly comparison.

Each case is written once in a version-neutral form and then turned into each
version's own data structures. 4.6 runs in this process through the real
``ContextCompiler``/``MemoryService``; 4.0 runs through ``legacy_v4`` in an
isolated child process. Nothing here rewrites a prompt: the report only measures
what each version's own code produced, and names the regions that have no
equivalent on the other side instead of pretending they match.

The default path reads no credentials, opens no socket and starts no bot. The
first-generation check drives each version's real request builder with a fake
transport, so request arguments are genuine while token usage stays unknown.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

from sayuki_bot.evaluation import legacy_v4

# Comparison levels, per the work plan.
LEVEL_ASSEMBLY = "P"  # assembly + first generation request
LEVEL_FLOW = "E"  # full replayed turn; not implemented in this batch

REQUIRED_CASE_FIELDS = ("id", "title", "level", "now", "current", "trigger")
VALID_TRIGGERS = {"mention", "name", "reply", "proactive"}
BACKGROUND_PREFIX = "以下為背景引用資料：\n"
FACT_PREFIX_CHARS = 40


class CaseError(ValueError):
    """A case that cannot be run as written."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cases(path) -> list[dict]:
    """Read the shared JSONL suite and reject anything ambiguous."""
    cases, seen = [], set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseError(f"line {number}: unreadable JSON ({exc})") from exc
        validate_case(case, number)
        if case["id"] in seen:
            raise CaseError(f"line {number}: duplicate case id {case['id']}")
        seen.add(case["id"])
        cases.append(case)
    if not cases:
        raise CaseError(f"No cases in {path}")
    return cases


def validate_case(case: dict, number: int | None = None) -> dict:
    """Fixed, timezone-qualified times and full ids; no implicit 'now'."""
    where = f"line {number}: " if number else ""
    for field in REQUIRED_CASE_FIELDS:
        if field not in case:
            raise CaseError(f"{where}missing required field {field!r}")
    if case["level"] not in {LEVEL_ASSEMBLY, LEVEL_FLOW}:
        raise CaseError(f"{where}unknown comparison level {case['level']!r}")
    if case["trigger"] not in VALID_TRIGGERS:
        raise CaseError(f"{where}unknown trigger {case['trigger']!r}")
    require_aware(case["now"], f"{where}case.now")
    declared = [*case.get("history", []), *case.get("stored_events", []), case["current"]]
    for item in declared:
        for field in ("id", "author_id", "author_name", "text", "time"):
            if field not in item:
                raise CaseError(f"{where}message {item.get('id')} missing {field!r}")
        require_aware(item["time"], f"{where}message {item['id']} time")
    ids = [item["id"] for item in declared]
    if len(ids) != len(set(ids)):
        raise CaseError(f"{where}duplicate message ids in case {case['id']}")
    sources = {item["id"]: item for item in declared}
    for item in case.get("memories", []):
        for field in ("subject_id", "kind", "key", "value", "source_id"):
            if field not in item:
                raise CaseError(f"{where}memory entry missing {field!r}")
        source = sources.get(item["source_id"])
        if source is None:
            # Without a real source the adapters would have to invent an author,
            # a time and a channel for the memory. Reject the case instead.
            raise CaseError(
                f"{where}memory {item['key']!r} names source_id {item['source_id']} "
                f"which case {case['id']} never declares"
            )
        if item.get("source_time"):
            require_aware(item["source_time"], f"{where}memory source_time")
            if item["source_time"] != source["time"]:
                raise CaseError(
                    f"{where}memory {item['key']!r} declares source_time "
                    f"{item['source_time']} but source {item['source_id']} is at "
                    f"{source['time']}"
                )
    legacy_v4.self_memory_entries(case)
    return case


def require_aware(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CaseError(f"{label} is not an ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise CaseError(f"{label} requires a timezone offset: {value!r}")
    return parsed


# ------------------------------------------------------------------ 4.6 side


class FakeCompletions:
    """Captures the request 4.6's own ModelClient builds; no network call."""

    def __init__(self, sink, reply):
        self.sink, self.reply = sink, reply

    async def create(self, **kwargs):
        self.sink.append(legacy_v4.request_fingerprint(kwargs))
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=self.reply), finish_reason="stop")
            ],
            usage=None,  # offline: provider usage is unavailable, never guessed
        )


class FakeChatClient:
    def __init__(self, sink, reply):
        self.chat = SimpleNamespace(completions=FakeCompletions(sink, reply))

    async def close(self):
        return None


def build_scene(case: dict, config, work_dir: Path, model: str):
    """Build 4.6's real runtime objects for a case: one shared path for P and E.

    Both layers must see the same memories, permissions and Turn shape, so the
    scene is constructed once here instead of twice. The caller owns the database
    and must close it.
    """
    from sayuki_bot.conversation.models import Event, Turn
    from sayuki_bot.discord.access import Access
    from sayuki_bot.evaluation.environment import LocalChannel, LocalGuild
    from sayuki_bot.memory.service import MemoryService
    from sayuki_bot.storage.database import Database

    config = deepcopy(config)
    # The comparator deliberately pins one common synthetic/model route for
    # both engines; deployable per-role defaults must not override that choice.
    config.sections.setdefault("model", {}).pop("routes", None)
    config.sections["model"]["chat"] = model
    config.sections.setdefault("memory", {})["shared_channel_ids"] = case.get(
        "shared_channel_ids", []
    )

    scenario_now = require_aware(case["now"], "case.now")
    guild_id = int(case.get("guild_id", 30))
    channel_id = int(case.get("channel_id", 20))
    bot_id = int(case.get("bot", {}).get("id", 99))
    bot_name = case.get("bot", {}).get("name", "紗月")

    database = Database(work_dir / "comparison.sqlite3")
    guild = LocalGuild(guild_id)
    guild.me = guild.add_member(bot_id, bot_name)
    transcript = []
    for person in case.get("participants", []):
        guild.add_member(int(person["id"]), person["name"])

    def channel_for(identifier):
        identifier = int(identifier)
        if identifier not in guild.channels:
            readers = case.get("channel_readers", {}).get(str(identifier))
            guild.channels[identifier] = LocalChannel(
                guild, identifier, transcript, readers=readers
            )
        return guild.channels[identifier]

    def make_event(raw):
        author_id = int(raw["author_id"])
        guild.add_member(author_id, raw["author_name"])
        target = channel_for(raw.get("channel_id", channel_id))
        event = Event(
            int(raw["id"]),
            guild_id,
            target.id,
            author_id,
            raw["author_name"],
            raw["text"],
            require_aware(raw["time"], f"message {raw['id']} time"),
            raw.get("trigger", case["trigger"]) == "mention",
            raw.get("reply_to"),
            tuple(raw.get("attachments", [])),
        )
        database.record_event(event)
        target.add(event)
        return event

    try:
        channel_for(channel_id)
        history = [make_event(raw) for raw in case.get("history", [])]
        stored = [make_event(raw) for raw in case.get("stored_events", [])]
        current = make_event(case["current"])
        sources = {event.id: event for event in [*history, *stored, current]}

        memory = MemoryService(database, clock=lambda: scenario_now)
        unmapped = []
        for item in case.get("memories", []):
            # A memory must come from a source the case actually declares.
            # Substituting another message would silently change the author,
            # time and channel the memory is attributed to.
            source = sources.get(int(item["source_id"]))
            if source is None:
                raise CaseError(
                    f"memory {item['key']!r} names source_id {item['source_id']} "
                    "which the case never declares"
                )
            try:
                memory.update(
                    source,
                    int(item["subject_id"]),
                    item.get("kind", "profile"),
                    item["key"],
                    item["value"],
                    context=item.get("context", ""),
                    ttl_hours=item.get("ttl_hours"),
                )
            except (ValueError, PermissionError) as exc:
                unmapped.append({"key": item["key"], "reason": f"{type(exc).__name__}: {exc}"})
        for entry in legacy_v4.self_memory_entries(case):
            # These version-neutral fixtures declare no real source or category.
            # Use the actual legacy importer, not the retired free-text writer.
            from sayuki_bot.memory.migration import import_records

            report = import_records(
                database,
                [
                    dict(
                        guild_id=str(current.guild_id),
                        channel_id=str(current.channel_id),
                        subject_id=str(bot_id),
                        kind="self",
                        key=entry["key"],
                        value=entry["value"],
                        source_id=None,
                        source_author=None,
                        updated_at=current.created_at.isoformat(),
                        expires_at=None,
                        provenance="legacy_v4_self",
                    )
                ],
                apply=True,
            )
            if report["skipped"]:
                raise CaseError("Invalid legacy self-memory fixture")

        for raw in [*case.get("history", []), case["current"]]:
            if raw.get("image_description"):
                source = sources[int(raw["id"])]
                database.save_observation(
                    source.id,
                    raw["image_description"],
                    source.text,
                    json.dumps(source.attachments),
                )

        bot = SimpleNamespace(
            user=guild.me,
            runtime_guilds={guild_id},
            transcript=transcript,
            get_guild=lambda identifier: guild if identifier == guild_id else None,
        )
        access = Access(bot, config)
        # discord/bot.py appends the triggering event to the channel history
        # before building the Turn, and the compiler takes its window from that
        # list and then skips the current message. Preparing the history without
        # the current event would keep one extra old message once the window is
        # full, so the adapter matches the production entry point exactly.
        channel_history = [event for event in history if event.channel_id == current.channel_id]
        turn_history = [*channel_history, current]
        turn = Turn(
            current,
            SimpleNamespace(
                channel=guild.channels[current.channel_id],
                author=guild.get_member(current.author_id),
            ),
            turn_history,
            reason="proactive" if case["trigger"] == "proactive" else "message",
            bot_id=bot_id,
        )
        return SimpleNamespace(
            config=config,
            database=database,
            guild=guild,
            bot=bot,
            access=access,
            memory=memory,
            turn=turn,
            current=current,
            channel_history=channel_history,
            turn_history=turn_history,
            transcript=transcript,
            unmapped=unmapped,
            scenario_now=scenario_now,
        )
    except BaseException:
        database.close()
        raise


async def assemble_v46(case: dict, config, work_dir: Path, model: str) -> dict:
    """Compile the case through 4.6's real context/memory path (P layer)."""
    from sayuki_bot.context.compiler import ContextCompiler
    from sayuki_bot.llm.client import ModelClient

    scene = build_scene(case, config, work_dir, model)
    config, database, memory = scene.config, scene.database, scene.memory
    turn, current = scene.turn, scene.current
    access, unmapped = scene.access, scene.unmapped
    channel_history, turn_history = scene.channel_history, scene.turn_history
    try:
        compiler = ContextCompiler(config, memory)

        started = perf_counter()
        allowed = access.allowed_channels(turn)
        messages = compiler.compile(turn, allowed)
        assembly_ms = (perf_counter() - started) * 1000

        requests = []
        client = ModelClient(
            config,
            database,
            "offline-placeholder-not-used",
            client=FakeChatClient(requests, case.get("fake_reply", "（離線假模型回覆）")),
        )
        generation_started = perf_counter()
        try:
            generation = await client.generate(messages, turn_id=turn.id, role="chat")
        finally:
            generation_ms = (perf_counter() - generation_started) * 1000
            await client.close()

        persona_block = messages[0]["content"]
        background = json.loads(messages[2]["content"][len(BACKGROUND_PREFIX) :])
        regions = {
            "persona": len(persona_block),
            "instructions": len(messages[1]["content"]),
            "background_total": len(messages[2]["content"]),
            "recent_history": len(json.dumps(background.get("recent_history"), ensure_ascii=False)),
            "older_summary": len(json.dumps(background.get("older_summary"), ensure_ascii=False)),
            "related_memory": len(json.dumps(background.get("related_memory"), ensure_ascii=False)),
            "self_memory": len(json.dumps(background.get("self_memory"), ensure_ascii=False)),
            "current_message": len(messages[3]["content"]),
        }
        return {
            "case_id": case["id"],
            "status": "ok",
            "version": "4.6",
            "persona": {
                "source": str(config.path("persona")),
                "source_sha256": legacy_v4.sha256_file(config.path("persona")),
                "block_sha256": sha256_text(persona_block),
                "chars": len(persona_block),
            },
            "messages": messages,
            "regions": regions,
            # The compiler excludes the current message from its history block,
            # so the record count covers prior messages only.
            "history_block_entries": len(background.get("recent_history") or []),
            "history_includes_current": any(
                record.get("message_id") == f"msg_{current.id}"
                for record in background.get("recent_history") or []
            ),
            "prior_messages_available": len(channel_history),
            "turn_history_entries": len(turn_history),
            "history_window_limit": config.get("conversation", "history_limit", 25),
            "history_chars_limit": config.get("conversation", "history_chars", 14000),
            "allowed_channels": allowed,
            "loaded_tools": sorted(turn.loaded_tools),
            "tools_compact": config.get("tools", "compact", True),
            "unmapped_memories": unmapped,
            "request": requests[0] if requests else None,
            "request_count": len(requests),
            "fake_reply_chars": len(generation.text),
            "usage": {
                "status": "unknown",
                "reason": "offline fake transport returns no usage",
            },
            "timing_ms": {
                "assembly": round(assembly_ms, 3),
                "fake_generation": round(generation_ms, 3),
            },
            "calls": [
                {
                    "function": "context.compiler.ContextCompiler.compile",
                    "module_file": str(
                        Path(sys.modules[ContextCompiler.__module__].__file__).resolve()
                    ),
                    "module_sha256": legacy_v4.sha256_file(
                        sys.modules[ContextCompiler.__module__].__file__
                    ),
                },
                {"function": "memory.service.MemoryService.recall"},
                {"function": "memory.service.MemoryService.summary_context"},
                {"function": "discord.access.Access.allowed_channels"},
                {"function": "llm.client.ModelClient.generate"},
            ],
        }
    finally:
        database.close()


# ------------------------------------------------------- region pairing/diff

# Regions that carry the same purpose on both sides. Anything not listed here is
# reported as one-sided instead of being folded into a total that looks like a win.
PAIRED_REGIONS = {
    "persona": ("persona", "persona"),
    "history": ("chat_history", "recent_history"),
    "related_memory": ("memory_context", "related_memory"),
    "self_memory": ("permanent_memory", "self_memory"),
    "summary": ("short_term_context", "older_summary"),
    "current_message": ("current_message", "current_message"),
}

# One-sided regions: real input on one version with no counterpart on the other.
LEGACY_ONLY_REGIONS = ("server_memory", "presence_context")
MODERN_ONLY_REGIONS = ("instructions",)


def redact_persona(messages: list[dict]) -> list[dict]:
    """Keep every synthetic block readable; replace persona text with hash + size.

    Mirrors 4.0's own ``_messages_for_log`` convention so a saved report can be
    reviewed without carrying the private persona into the results directory.
    """
    redacted = []
    for index, message in enumerate(messages):
        content = message.get("content", "")
        if index == 0 and message.get("role") == "system":
            redacted.append(
                {
                    "role": message["role"],
                    "persona_omitted": True,
                    "chars": len(content),
                    "sha256": sha256_text(content),
                }
            )
        else:
            redacted.append(message)
    return redacted


def prompt_text(result: dict) -> str:
    return "\n".join(message["content"] for message in result["messages"])


def fact_coverage(case: dict, result: dict) -> list[dict]:
    """Did this version's own assembly actually carry each declared fact?"""
    haystack = prompt_text(result)
    facts = []
    for raw in case.get("history", []):
        facts.append({"kind": "history", "id": raw["id"], "text": raw["text"]})
    facts.append({"kind": "current", "id": case["current"]["id"], "text": case["current"]["text"]})
    for item in case.get("memories", []):
        facts.append(
            {
                "kind": f"memory:{item.get('kind', 'profile')}",
                "id": item["key"],
                "text": item["value"],
            }
        )
    for index, item in enumerate(case.get("self_memory", [])):
        facts.append({"kind": "self_memory", "id": index, "text": item})
    covered = []
    for fact in facts:
        text = fact["text"]
        if text and text in haystack:
            match = "exact"
        elif text and text[:FACT_PREFIX_CHARS] in haystack:
            match = "prefix"
        else:
            match = "absent"
        covered.append({**fact, "match": match})
    return covered


def _block(text: str, tag: str) -> str:
    found = re.search(rf"<{tag}>.*?</{tag}>", text, flags=re.S)
    return found.group(0) if found else ""


def persona_difference(legacy: dict, modern: dict, persona_source: Path) -> dict:
    """Decompose the persona difference from what was actually assembled.

    Measured, not recomputed from assumptions: the registry block is read back out
    of 4.6's own assembled persona (it is appended last), so on-demand tool loading
    and `tools.compact` are reflected automatically. The memory-guidance sentence
    the compiler rewrites is accounted for separately instead of being folded into
    "tool compression", and `residual_chars` must be 0 for the split to be honest.
    """
    source = Path(persona_source).read_text(encoding="utf-8-sig")
    modern_block = modern["messages"][0]["content"]

    source_tools = _block(source, "tools")
    body_source = source.replace(source_tools, "", 1) if source_tools else source
    # 4.6 appends the registry block last, so it is the final <tools> span.
    tail = modern_block.rfind("<tools>")
    registry = modern_block[tail:] if tail != -1 else ""
    prefix = modern_block[:tail] if tail != -1 else modern_block

    source_guidance = _block(source, "memory_guidance")
    modern_guidance = _block(modern_block, "memory_guidance")
    guidance_delta = len(modern_guidance) - len(source_guidance)
    # Separate the whitespace the compiler adds from the persona's own trailing
    # newlines, so the join is not confused with untouched text.
    source_tail_newlines = len(body_source) - len(body_source.rstrip("\n"))
    join_chars = len(prefix) - len(prefix.rstrip("\n")) - source_tail_newlines
    accounted = len(body_source) + guidance_delta + join_chars + len(registry)
    # Everything outside the two swapped regions must still be byte-identical.
    outside_source = body_source.replace(source_guidance, "\x00", 1) if source_guidance else ""
    outside_modern = prefix.replace(modern_guidance, "\x00", 1) if modern_guidance else ""
    unchanged_outside = bool(outside_source) and outside_modern.rstrip("\n") == (
        outside_source.rstrip("\n")
    )

    return {
        "persona_source_sha256": legacy["persona"]["source_sha256"],
        "same_source_file": legacy["persona"]["source_sha256"]
        == modern["persona"]["source_sha256"],
        "v40_block_sha256": legacy["persona"]["block_sha256"],
        "v46_block_sha256": modern["persona"]["block_sha256"],
        "source_chars": len(source),
        "v40_chars": legacy["persona"]["chars"],
        "v46_chars": modern["persona"]["chars"],
        "delta_chars": modern["persona"]["chars"] - legacy["persona"]["chars"],
        "v40_normalisation_chars": legacy["persona"]["chars"] - len(source),
        "breakdown": {
            "source_tools_block_removed": -len(source_tools),
            "registry_block_added": len(registry),
            "memory_guidance_rewritten": guidance_delta,
            "join_whitespace": join_chars,
            "residual_chars": modern["persona"]["chars"] - accounted,
        },
        "unchanged_outside_swapped_blocks": unchanged_outside,
        "registry_inputs": {
            "loaded_tools": modern.get("loaded_tools", []),
            "compact": modern.get("tools_compact"),
        },
        "note": "4.0 sends the persona as written (only leading/trailing newlines are "
        "normalised). 4.6 removes the persona <tools> block, appends a registry block "
        "sized by this turn's loaded_tools and tools.compact, and rewrites the retired "
        "tool-selection sentence inside <memory_guidance>. Role and rules text is "
        "otherwise unchanged; the registry figure is measured from the assembled "
        "persona, not from an empty tool set.",
    }


def compare_case(case: dict, legacy: dict, modern: dict, persona_source: Path) -> dict:
    """Region-level accounting; incomparable parts stay visible as incomparable."""
    if legacy["status"] != "ok" or modern["status"] != "ok":
        return {
            "case_id": case["id"],
            "status": "error",
            "v40_status": legacy["status"],
            "v46_status": modern["status"],
            "v40_error": legacy.get("error"),
            "v46_error": modern.get("error"),
        }
    paired = {}
    for name, (legacy_key, modern_key) in PAIRED_REGIONS.items():
        left = legacy["regions"].get(legacy_key, 0)
        right = modern["regions"].get(modern_key, 0)
        paired[name] = {
            "v40_chars": left,
            "v46_chars": right,
            "delta_chars": right - left,
            "ratio": round(right / left, 3) if left else None,
        }
    incomparable = []
    for name in LEGACY_ONLY_REGIONS:
        size = legacy["regions"].get(name, 0)
        incomparable.append(
            {
                "region": name,
                "present_in": "4.0",
                "chars": size,
                "reason": "4.6 has no equivalent block; not counted as a 4.6 saving",
            }
        )
    for name in MODERN_ONLY_REGIONS:
        incomparable.append(
            {
                "region": name,
                "present_in": "4.6",
                "chars": modern["regions"].get(name, 0),
                "reason": "4.6 carries per-turn instructions as a separate system message; "
                "4.0 folds the same purpose into its [系統資訊] scaffolding",
            }
        )
    incomparable.append(
        {
            "region": "summary",
            "present_in": "both",
            "chars": paired["summary"],
            "reason": "not exercised offline: 4.0's short-term block needs digest_pending "
            "(a model call) and 4.6's older_summary needs background extraction "
            "(default off), so neither side's summary is evidence in this batch",
        }
    )
    if legacy["regions"].get("time_context"):
        incomparable.append(
            {
                "region": "stats_narration",
                "present_in": "4.0",
                "chars": legacy["regions"]["time_context"],
                "reason": "4.0's native 現在時間/發言數 narration is kept; only the clock and "
                "the message counters come from the case",
            }
        )
    for entry in legacy.get("unmapped_memories", []):
        incomparable.append({"region": "memory_mapping", "present_in": "4.0", **entry})
    for entry in modern.get("unmapped_memories", []):
        incomparable.append({"region": "memory_mapping", "present_in": "4.6", **entry})

    legacy_total = sum(len(message["content"]) for message in legacy["messages"])
    modern_total = sum(len(message["content"]) for message in modern["messages"])
    return {
        "case_id": case["id"],
        "title": case["title"],
        "status": "compared",
        "level": case["level"],
        "trigger": case["trigger"],
        "v40_trigger_check": legacy.get("trigger_check"),
        "total_input_chars": {
            "v40": legacy_total,
            "v46": modern_total,
            "delta": modern_total - legacy_total,
            "ratio": round(modern_total / legacy_total, 3) if legacy_total else None,
        },
        "paired_regions": paired,
        "history_window": {
            "v40_block_entries": legacy["history_block_entries"],
            "v40_includes_current": legacy["history_includes_current"],
            "v46_block_entries": modern["history_block_entries"],
            "v46_includes_current": modern["history_includes_current"],
            "prior_messages_available": modern["prior_messages_available"],
            "v40_window_limit": legacy["history_window_limit"],
            "v46_window_limit": modern["history_window_limit"],
            "v46_chars_limit": modern["history_chars_limit"],
            "v46_turn_history_entries": modern["turn_history_entries"],
            "note": "Both adapters feed their version's production entry shape: 4.0's "
            "cache and 4.6's Turn.history both include the triggering message, and each "
            "version then applies its own window. 4.0 prints it inside the history block, "
            "4.6 skips it there and sends it as a separate message, so block entry counts "
            "differ by one even when the same prior messages are covered.",
        },
        "persona": persona_difference(legacy, modern, persona_source),
        "request": {
            "v40": legacy.get("request"),
            "v46": modern.get("request"),
            "settings_match": _settings_match(legacy.get("request"), modern.get("request")),
        },
        "tokens": {
            "status": "unknown",
            "reason": "offline run has no provider usage; character counts are chars, not tokens",
        },
        "fact_coverage": {
            "method": "string/prefix containment against each version's assembled prompt; "
            "it shows which declared facts survived each version's own window and "
            "truncation rules. It is not a semantic equivalence proof and says nothing "
            "about author, source time or context being carried identically.",
            "v40": fact_coverage(case, legacy),
            "v46": fact_coverage(case, modern),
        },
        "incomparable": incomparable,
        "timing_ms": {"v40": legacy["timing_ms"], "v46": modern["timing_ms"]},
    }


# Every request setting that must agree before a later timing or usage number
# can be read as a version difference rather than a configuration difference.
COMPARED_SETTINGS = ("model", "max_tokens", "temperature", "extra_body")


def _settings_match(left: dict | None, right: dict | None) -> dict:
    """Separate "both requests exist" from "the settings are fair to compare".

    `captured` says each version's own code produced a request. `comparable` is
    the single verdict every report field uses, and it is only true when every
    compared setting agrees: reasoning effort included. A difference is kept with
    its values so it can be fixed in the runner, never by editing a version.
    """
    if not left or not right:
        return {
            "captured": False,
            "comparable": False,
            "differences": [
                {
                    "setting": "request",
                    "reason": "a version produced no request; nothing to compare",
                }
            ],
            "values": {"v40": left, "v46": right},
        }
    differences = [
        {"setting": name, "v40": left.get(name), "v46": right.get(name)}
        for name in COMPARED_SETTINGS
        if left.get(name) != right.get(name)
    ]
    verdict = {
        "captured": True,
        "comparable": not differences,
        "differences": differences,
        "values": {
            "v40": {name: left.get(name) for name in COMPARED_SETTINGS},
            "v46": {name: right.get(name) for name in COMPARED_SETTINGS},
        },
    }
    verdict.update({name: left.get(name) == right.get(name) for name in COMPARED_SETTINGS})
    return verdict


# --------------------------------------------------------------- run + report


def modern_environment() -> dict:
    """Evidence that this process really is 4.6 and never loaded 4.0."""
    import sayuki_bot
    from sayuki_bot.context import compiler as compiler_module
    from sayuki_bot.llm import client as client_module
    from sayuki_bot.memory import service as service_module

    modules = {}
    for module in (compiler_module, service_module, client_module):
        path = Path(module.__file__).resolve()
        modules[module.__name__] = {"file": str(path), "sha256": legacy_v4.sha256_file(path)}
    return {
        "python": sys.executable,
        "package_file": str(Path(sayuki_bot.__file__).resolve()),
        "modules": modules,
    }


def known_limits(legacy_environment: dict) -> list[str]:
    limits = [
        "P 層只比較組裝與首次生成請求；沒有執行工具、排隊、分段或送達，不能當成完整回合或端到端比較。",
        "首次生成使用假傳輸，供應商用量無法取得，token 一律記為 unknown；字元量是 chars，不是 token。",
        "4.0 的 short_term 摘要需要 digest_pending 模型呼叫，4.6 的 older_summary 需要背景抽取（預設關閉）；"
        "本輪兩邊都是空的，摘要區不構成任何一方的證據。",
        "主動查看案例只比較上下文組裝，沒有測試任何一版的觸發政策。",
        "4.0 的 presence 與伺服器記憶在合成案例中沒有來源資料，屬於單邊區塊，不計入任何一方的節省。",
    ]
    if legacy_environment.get("aiofiles") == "stdlib-shim":
        limits.append(
            "借用的 4.5 venv 沒有 aiofiles，子程序以 stdlib 墊片提供非同步檔案開啟，"
            "讓 4.0 原版 MemoryManager／ShortTermMemoryManager 能照原碼執行；"
            "格式化與合併邏輯未被取代，但這是環境偏差，O8 需以真實安裝複驗。"
        )
    return limits


async def run_comparison(
    cases,
    *,
    config,
    persona_path,
    model,
    legacy_root=None,
    python=None,
    include_prompts=True,
    include_persona=False,
    work_root=None,
):
    """Assemble every supported case on both versions and build the report."""
    import tempfile

    supported = [case for case in cases if case["level"] == LEVEL_ASSEMBLY]
    unsupported = [
        {
            "case_id": case["id"],
            "title": case.get("title"),
            "level": case["level"],
            "reason": "E 層（共同流程回放）留給 O2，本輪不執行工具接續或送達",
        }
        for case in cases
        if case["level"] != LEVEL_ASSEMBLY
    ]
    for case in supported:
        if case.get("tool_results") or case.get("expected_tools"):
            unsupported.append(
                {
                    "case_id": case["id"],
                    "title": case.get("title"),
                    "level": case["level"],
                    "reason": "案例帶有工具結果；P 層不執行工具，該部分未比較",
                    "partial": True,
                }
            )

    # 4.0 takes reasoning effort through its constructor, 4.6 through config.
    # Give both the same value so a later timing difference cannot be a hidden
    # configuration difference; whatever still differs is reported, not patched.
    effort = config.get("model", "reasoning_effort", "") or ""
    legacy_output = legacy_v4.run_cases(
        supported,
        legacy_root=legacy_root,
        persona_path=persona_path,
        model=model,
        history_limit=config.get("conversation", "history_limit", 25),
        use_reasoning_effort=bool(effort),
        reasoning_effort=effort or "medium",
        python=python,
    )
    legacy_results = {item["case_id"]: item for item in legacy_output["results"]}

    modern_results = {}
    with tempfile.TemporaryDirectory(prefix="compare46-") as directory:
        for index, case in enumerate(supported):
            work_dir = Path(work_root or directory) / f"case-{index}-{case['id']}"
            work_dir.mkdir(parents=True, exist_ok=True)
            try:
                modern_results[case["id"]] = await assemble_v46(case, config, work_dir, model)
            except Exception as exc:  # keep the batch going; report the real reason
                modern_results[case["id"]] = {
                    "case_id": case["id"],
                    "status": "error",
                    "version": "4.6",
                    "error": f"{type(exc).__name__}: {exc}",
                }

    compared = []
    for case in supported:
        legacy = legacy_results.get(
            case["id"],
            {"case_id": case["id"], "status": "missing", "error": "4.0 worker returned no result"},
        )
        modern = modern_results[case["id"]]
        entry = compare_case(case, legacy, modern, Path(persona_path))
        if include_prompts and legacy.get("messages") and modern.get("messages"):
            keep = (lambda messages: messages) if include_persona else redact_persona
            entry["assembled"] = {
                "v40": keep(legacy["messages"]),
                "v46": keep(modern["messages"]),
            }
        compared.append(entry)

    persona = Path(persona_path)
    return {
        "run_id": uuid4().hex,
        "created_at": datetime.now().astimezone().isoformat(),
        "mode": "offline-assembly",
        "level": LEVEL_ASSEMBLY,
        "case_count": len(cases),
        "compared_count": sum(1 for item in compared if item["status"] == "compared"),
        "settings": {
            "model": model,
            "history_limit": config.get("conversation", "history_limit", 25),
            "persona": {"path": str(persona), "sha256": legacy_v4.sha256_file(persona)},
            "scenario_clock": "fixed per case; execution time measured with perf_counter",
            "network": "none",
            "credentials": "none read",
            "persona_text_in_report": include_persona,
        },
        "versions": {"v40": legacy_output["environment"], "v46": modern_environment()},
        "cases": compared,
        "unsupported": unsupported,
        "limits": known_limits(legacy_output["environment"]),
    }


def summarize(report: dict) -> dict:
    """Small aggregate for the operator; never a verdict on which version wins."""
    rows = [case for case in report["cases"] if case["status"] == "compared"]
    totals = {
        "compared": len(rows),
        "errors": sum(1 for case in report["cases"] if case["status"] == "error"),
        "unsupported": len(report["unsupported"]),
    }
    if rows:
        totals["total_input_chars"] = {
            "v40": sum(case["total_input_chars"]["v40"] for case in rows),
            "v46": sum(case["total_input_chars"]["v46"] for case in rows),
        }
        # One verdict, covering reasoning/extra_body too. A case that produced no
        # request counts as not comparable rather than quietly passing.
        totals["settings_comparable_all"] = all(
            case["request"]["settings_match"]["comparable"] for case in rows
        )
        totals["settings_differences"] = [
            {
                "case_id": case["case_id"],
                "differences": case["request"]["settings_match"]["differences"],
            }
            for case in rows
            if case["request"]["settings_match"]["differences"]
        ]
        totals["facts_absent"] = {
            "v40": sum(
                1
                for case in rows
                for fact in case["fact_coverage"]["v40"]
                if fact["match"] == "absent"
            ),
            "v46": sum(
                1
                for case in rows
                for fact in case["fact_coverage"]["v46"]
                if fact["match"] == "absent"
            ),
        }
    totals["tokens"] = "unknown (offline)"
    return totals
