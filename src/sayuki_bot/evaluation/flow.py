"""E layer: replay one whole turn through each version's real flow, offline.

The P layer (``comparison.py``) stops at the first request. This layer lets the
turn finish: queueing, tool rounds, continuation generations, segmentation and
delivery all run through the version's own code. 4.6 uses the real ``Engine``,
``Delivery`` and ``ToolExecutor``; 4.0 uses its real ``Scheduler`` in the
isolated child process.

Only three things are substituted, identically on both sides:

* the model transport, which replays a scripted reply per round and records the
  request the version's own code built;
* every boundary that could leave the machine, which is refused loudly and
  listed in the report rather than left to chance;
* the pacing clock, which is recorded rather than slept through, so human wait
  is counted in full instead of being deleted to look faster.

Nothing here decides what a version should do with those inputs.
"""

from __future__ import annotations

import asyncio
import json
import threading
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace

# Event kinds shared by both versions, so one reader can compare timelines.
QUEUE_IN = "enqueue"
QUEUE_OUT = "dequeue"
MODEL_REQUEST = "model_request"
MODEL_RESPONSE = "model_response"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
VISIBLE = "visible"
PACING = "pacing"
ACTION = "action"
DONE = "done"

PACING_SIMULATED = "simulated"
PACING_REAL = "real"

TAG_PATTERN = re.compile(r"\[\[([A-Z_$][A-Z_$-]*)")

# Control markers and tools this harness can actually serve offline on both
# versions. Anything outside the list is refused before a case runs, so an
# unsupported boundary cannot be discovered by accidentally reaching it.
SUPPORTED_TAGS = frozenset(
    {
        "SPLIT",
        "SPLIT-WAIT",
        "REPLY_TO",
        "PING",
        "$NO_NEED_TO_ANSWER$",
        "LOOKUP_MEMORY",
        "MEMORY_SEARCH",
        "USER_STATS",
        "CHECK_ROLES",
        "CHECK_PRESENCE",
        "READ_MESSAGE",
        "READ_CHANNEL",
        "CHANNEL_LOOKUP",
        "MEM_UPDATE",
        "MEM_REVISE",
        "MEM_DELETE",
        "STATUS",
    }
)

# Why each excluded boundary is refused, reported alongside the sentinels.
REFUSED_TAGS = {
    "READ_WEB": "external browser subprocess; no offline fixture this round",
    "SEARCH": "external browser subprocess; no offline fixture this round",
    "MATH_CALC": "unbounded local computation",
    "MATH_PLOT": "unbounded local computation",
    "MATH_LATEX": "unbounded local computation",
    "VIEW_IMAGE": "needs a vision model request",
    "VIEW_STICKER": "Discord CDN download and vision model request",
    "SEND_STICKER": "real Discord sticker side effect",
    "REACT": "needs a real Discord message object",
    "DM_USER": "real Discord side effect",
    "THREAD": "real Discord side effect",
    "NICKNAME": "real Discord side effect",
    "SERVER_EVENT": "real Discord side effect",
    "MUTE": "real Discord side effect",
    "POLL": "real Discord side effect",
    "BUTTON_UI": "real Discord side effect",
    "SEND_CHANNEL_MESSAGE": "real Discord side effect",
    "REMIND": "writes a scheduled reminder",
    "PERMANENT_MEMORY": "changes self memory semantics",
    "SERVER_MEMORY": "changes server memory semantics",
}


class Timeline:
    """Monotonic event record, measured at the substituted boundaries.

    Both versions are observed the same way — at the transport, the tool results
    and the channel — rather than through one version's internal traces, which
    only 4.6 has. ``origin`` is set when the turn is enqueued, so every published
    offset is measured from the same point on both sides and excludes the
    harness's own scene-building cost.
    """

    def __init__(self, clock=perf_counter):
        self.clock = clock
        self.created = clock()
        self.origin = None
        self.events: list[dict] = []

    def _offset(self, moment):
        base = self.origin if self.origin is not None else self.created
        return round((moment - base) * 1000, 3)

    def start(self) -> None:
        """Set the common measurement origin (the moment the turn is enqueued)."""
        self.origin = self.clock()

    def mark(self, kind: str, at=None, **detail) -> dict:
        moment = self.clock() if at is None else at
        event = {"t_ms": self._offset(moment), "kind": kind, **detail}
        self.events.append(event)
        return event

    @contextmanager
    def tool(self, name: str):
        """Record the start before the call and the end after it."""
        started = self.clock()
        self.mark(TOOL_CALL, at=started, tool=name)
        carrier = {}
        try:
            yield carrier
        finally:
            finished = self.clock()
            self.mark(
                TOOL_RESULT,
                at=finished,
                tool=name,
                duration_ms=round((finished - started) * 1000, 3),
                **carrier,
            )

    def pacing(self, seconds: float, mode: str, actual_seconds: float = 0.0) -> None:
        """Human wait the version asked for, kept as a locatable event."""
        self.mark(
            PACING,
            requested_ms=round(max(0.0, seconds) * 1000, 3),
            actual_ms=round(max(0.0, actual_seconds) * 1000, 3),
            mode=mode,
        )


class RecordingSleeper:
    """Stands in for 4.6's pacing clock and keeps the requested wait on record."""

    def __init__(self, timeline: Timeline, mode: str, real_module):
        self.timeline, self.mode, self._real = timeline, mode, real_module

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def sleep(self, seconds, *args, **kwargs):
        if self.mode == PACING_REAL:
            started = perf_counter()
            result = await self._real.sleep(seconds, *args, **kwargs)
            self.timeline.pacing(seconds, self.mode, perf_counter() - started)
            return result
        self.timeline.pacing(seconds, self.mode, 0.0)
        return None


def scripted_tags(case: dict) -> set[str]:
    tags = set()
    for replies in case.get("scripted", {}).values():
        for reply in replies or []:
            tags.update(TAG_PATTERN.findall(reply))
    return tags


def check_scripted_tags(case: dict) -> list[dict]:
    """Refuse a case that would reach a boundary this harness does not serve."""
    refused = []
    for tag in sorted(scripted_tags(case)):
        if tag in SUPPORTED_TAGS:
            continue
        refused.append(
            {
                "tag": tag,
                "reason": REFUSED_TAGS.get(tag, "not served by the offline E layer"),
            }
        )
    return refused


def derive_metrics(events, sent, actions=(), *, assembly_ms=None) -> dict:
    """Turn a timeline into the report fields. One implementation for both versions.

    Every offset is already relative to the enqueue event, so `first_visible_ms`
    and `total_ms` measure the turn rather than the harness's setup. Ordering
    questions are answered from the event sequence, never from rounded times.
    """
    order = {id(event): index for index, event in enumerate(events)}
    first = next((e for e in events if e["kind"] == VISIBLE), None)
    done = next((e for e in reversed(events) if e["kind"] == DONE), None)
    dequeue = next((e for e in events if e["kind"] == QUEUE_OUT), None)
    model_calls = [e for e in events if e["kind"] == MODEL_RESPONSE]
    tool_results = [e for e in events if e["kind"] == TOOL_RESULT]
    visible = [e for e in events if e["kind"] == VISIBLE]
    pacing = [e for e in events if e["kind"] == PACING]
    first_tool = next((e for e in events if e["kind"] == TOOL_CALL), None)
    before_first_visible = [e for e in pacing if first is None or order[id(e)] < order[id(first)]]
    return {
        "queue_ms": dequeue["t_ms"] if dequeue else None,
        "assembly_ms": assembly_ms,
        "model_rounds": len(model_calls),
        "model_time_ms": round(sum(e.get("duration_ms", 0.0) for e in model_calls), 3),
        "tool_calls": len(tool_results),
        "tool_time_ms": round(sum(e.get("duration_ms", 0.0) for e in tool_results), 3),
        "tool_sequence": [{"tool": e.get("tool"), "status": e.get("status")} for e in tool_results],
        "first_visible_ms": first["t_ms"] if first else None,
        "visible_chunks": len(visible),
        "delivery_targets": [
            {"chars": e.get("chars"), "reply_to": e.get("reply_to")} for e in visible
        ],
        "total_ms": done["t_ms"] if done else None,
        "pacing": {
            "requested_ms": round(sum(e["requested_ms"] for e in pacing), 3),
            "actual_ms": round(sum(e["actual_ms"] for e in pacing), 3),
            "before_first_visible_ms": round(
                sum(e["requested_ms"] for e in before_first_visible), 3
            ),
            "events": len(pacing),
            "mode": pacing[0]["mode"] if pacing else None,
        },
        # Sequence-based, so two events sharing a rounded millisecond cannot
        # flip the answer.
        "spoke_before_first_tool": bool(
            first_tool and first and order[id(first)] < order[id(first_tool)]
        ),
        "has_tool_round": bool(first_tool),
        "sent_segments": list(sent),
        "actions": [
            {"tool": e.get("tool"), "status": e.get("status")}
            for e in events
            if e["kind"] == ACTION
        ],
        "action_records": list(actions),
        # Which segment is the first real answer is a human judgement; the report
        # carries the ordered segments and leaves this for a person to fill in.
        "substantive_answer_index": None,
    }


# ------------------------------------------------------------------- 4.6 side


class ScriptedTransport:
    """Replays one scripted reply per round through 4.6's own request builder."""

    def __init__(self, timeline: Timeline, replies, requests):
        self.timeline, self.replies, self.requests = timeline, list(replies), requests
        self.chat = SimpleNamespace(completions=self)
        self.exhausted = None

    async def create(self, **kwargs):
        from sayuki_bot.evaluation import legacy_v4

        round_number = len(self.requests) + 1
        self.requests.append(legacy_v4.request_fingerprint(kwargs))
        self.timeline.mark(MODEL_REQUEST, round=round_number, messages=len(kwargs["messages"]))
        if not self.replies:
            # The engine's worker records a failed turn rather than propagating,
            # so the flag is what surfaces this as a harness error, not a result.
            self.exhausted = round_number
            raise legacy_v4.ScriptExhausted(f"ran out of scripted replies at round {round_number}")
        text = self.replies.pop(0)
        self.timeline.mark(MODEL_RESPONSE, round=round_number, chars=len(text), duration_ms=0.0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")],
            usage=None,  # offline: provider usage is unavailable, never guessed
        )

    async def close(self):
        return None


def _timed_transport(inner, timeline: Timeline, requests):
    """Wrap a live transport so its rounds appear on the shared timeline."""

    class TimedCompletions:
        async def create(self, **kwargs):
            round_number = len(requests) + 1
            timeline.mark(MODEL_REQUEST, round=round_number, messages=len(kwargs["messages"]))
            started = perf_counter()
            response = await inner.chat.completions.create(**kwargs)
            choice = response.choices[0]
            text = getattr(choice.message, "content", "") or ""
            requests.append(inner.requests[-1] if inner.requests else {})
            timeline.mark(
                MODEL_RESPONSE,
                round=round_number,
                chars=len(text),
                duration_ms=round((perf_counter() - started) * 1000, 3),
                finish_reason=getattr(choice, "finish_reason", None),
                # The model's own text, separate from whatever delivery sends.
                raw_text=text,
            )
            return response

    async def close():
        # The live SDK client is owned by the caller and closed once at the end,
        # not per case; ModelClient.close() must still find something to call.
        return None

    return SimpleNamespace(
        chat=SimpleNamespace(completions=TimedCompletions()), inner=inner, close=close
    )


def install_modern_guards(record):
    """Refuse every 4.6 boundary that could leave the machine, and list it.

    ``LocalTools`` already answers unsupported tools with "unavailable", so these
    are the backstop: if a future change let a web or subprocess path through,
    the case fails loudly instead of reaching the network.
    """
    from sayuki_bot.evaluation.legacy_v4 import ToolExecutionAttempted
    from sayuki_bot.tools import safe_web_proxy, search

    restore = []

    def refuse(label, reason):
        def guard(*args, **kwargs):
            record["calls"].append(label)
            raise ToolExecutionAttempted(f"offline replay reached the real {label}: {reason}")

        return guard

    for module, attribute, reason in (
        (search, "search_web", "external browser subprocess"),
        (search, "read_web_page", "external browser subprocess"),
        (search, "_fetch_with_obscura", "external browser subprocess"),
        (safe_web_proxy, "validate_public_url", "outbound network resolution"),
        (safe_web_proxy, "public_egress_proxy", "outbound network proxy"),
    ):
        if not hasattr(module, attribute):
            continue
        label = f"{module.__name__}.{attribute}"
        restore.append((module, attribute, getattr(module, attribute)))
        setattr(module, attribute, refuse(label, reason))
        record["installed"].append(label)
        record["boundaries"].append(
            {"boundary": label, "mode": "refused", "reason": reason, "version": "4.6"}
        )
    return restore


def mark_dequeue_on_run(engine, timeline: Timeline):
    """Record the dequeue where the worker really picks the turn up.

    ``submit()`` only puts the turn on the queue and starts a worker task, so
    marking there would report a queue wait of nearly zero and push the real wait
    into the first-visible measurement.
    """
    original_run = engine.run

    async def timed_run(turn):
        timeline.mark(QUEUE_OUT)
        return await original_run(turn)

    engine.run = timed_run
    return timed_run


def recording_tools(base_class, timeline: Timeline):
    """Wrap 4.6's real tool executor to time each call at its true boundaries."""

    class RecordingTools(base_class):
        # STATUS is the one action this harness can serve; everything else keeps
        # the product's own "unavailable" answer.
        SUPPORTED = base_class.SUPPORTED | {"STATUS"}

        async def execute(self, turn, name, body):
            with timeline.tool(name) as carrier:
                result = await super().execute(turn, name, body)
                status = result.get("status") if isinstance(result, dict) else "ok"
                carrier["status"] = status
                # Bounded preview so a hit and a miss are distinguishable.
                carrier["preview"] = json.dumps(result, ensure_ascii=False)[:200]
            from sayuki_bot.tools.registry import REGISTRY

            if name in REGISTRY and REGISTRY[name].action:
                timeline.mark(ACTION, tool=name, status=status)
            return result

    RecordingTools.__name__ = "RecordingTools"
    return RecordingTools


async def run_v46_flow(
    case: dict,
    config,
    work_dir: Path,
    model: str,
    *,
    pacing=PACING_SIMULATED,
    transport=None,
    on_cancel=None,
):
    """Run one whole turn through 4.6's real Engine, Delivery and ToolExecutor."""
    from sayuki_bot.evaluation.comparison import build_scene

    timeline = Timeline()
    guard_record = {"installed": [], "calls": [], "boundaries": []}
    # Cleanup is registered as each resource is acquired, so a failure during
    # setup — a missing script, a work_dir that cannot hold the database — still
    # restores the global tool guards instead of leaking them into the next case.
    with ExitStack() as resources:
        restore_guards = install_modern_guards(guard_record)
        resources.callback(restore_modern_guards, restore_guards)
        scripted = case.get("scripted", {}).get("v46")
        if not scripted and transport is None:
            raise ValueError(f"case {case['id']} has no scripted v46 replies")

        assembly_started = perf_counter()
        scene = build_scene(case, config, work_dir, model)
        resources.callback(scene.database.close)
        assembly_ms = round((perf_counter() - assembly_started) * 1000, 3)
        return await _run_v46_turn(
            case,
            scene,
            timeline,
            guard_record,
            scripted,
            assembly_ms,
            pacing,
            resources,
            transport=transport,
            on_cancel=on_cancel,
        )


def restore_modern_guards(restore_guards) -> None:
    for module, attribute, original in restore_guards:
        setattr(module, attribute, original)


async def _run_v46_turn(  # noqa: C901 - one linear turn around the real Engine
    case,
    scene,
    timeline,
    guard_record,
    scripted,
    assembly_ms,
    pacing,
    resources,
    transport=None,
    on_cancel=None,
):
    """The turn itself; every resource it takes is registered for cleanup.

    ``on_cancel`` receives a snapshot of the partial turn if the batch is
    cancelled. It runs while the scene and timeline are still alive, because the
    cleanup below tears them down and the caller could not rebuild them.
    """
    from sayuki_bot.context.compiler import ContextCompiler
    from sayuki_bot.conversation.engine import Engine
    from sayuki_bot.delivery import service as delivery_module
    from sayuki_bot.evaluation.environment import LocalTools
    from sayuki_bot.evaluation.legacy_v4 import ScriptExhausted, ToolExecutionAttempted
    from sayuki_bot.evaluation.live import BudgetStopped
    from sayuki_bot.llm.client import ModelClient

    failures = case.get("tool_failures", {})

    async def change_presence(**kwargs):
        if "STATUS" in failures:
            raise TimeoutError(failures["STATUS"])
        scene.transcript.append({"kind": "presence", "detail": str(kwargs)})

    scene.bot.change_presence = change_presence
    scene.bot.sayuki_status_until = None

    requests: list[dict] = []
    # Live mode swaps the scripted replay for a real transport; the request the
    # version's own ModelClient builds is unchanged either way.
    scripted_transport = None
    if transport is None:
        scripted_transport = ScriptedTransport(timeline, scripted, requests)
        transport = scripted_transport
    else:
        transport = _timed_transport(transport, timeline, requests)
    client = ModelClient(
        scene.config, scene.database, "offline-placeholder-not-used", client=transport
    )
    executor = recording_tools(LocalTools, timeline)(
        scene.bot, scene.config, scene.database, scene.memory, client, scene.access
    )
    delivery = delivery_module.Delivery(scene.config, scene.database, scene.bot)

    workers = []
    channel = scene.turn.source.channel
    original_send = channel.send

    async def recording_send(**kwargs):
        message = await original_send(**kwargs)
        reference = kwargs.get("reference")
        timeline.mark(
            VISIBLE,
            chars=len(kwargs.get("content", "") or ""),
            reply_to=getattr(reference, "message_id", None),
        )
        return message

    channel.send = recording_send
    sleeper = RecordingSleeper(timeline, pacing, delivery_module.asyncio)
    delivery_module.asyncio = sleeper

    engine = Engine(
        scene.config,
        ContextCompiler(scene.config, scene.memory),
        client,
        executor,
        delivery,
        scene.access,
    )
    mark_dequeue_on_run(engine, timeline)

    outcome = None
    try:
        future = asyncio.get_running_loop().create_future()
        scene.turn.completion = future
        timeline.start()
        timeline.mark(QUEUE_IN)
        if not engine.submit(scene.turn):
            raise RuntimeError("Engine queue full")
        # The worker removes itself from engine.workers when it ends, so close()
        # would not gather it; keep the handle to retrieve any harness error.
        workers = list(engine.workers.values())
        outcome = await future
        live_transport = getattr(transport, "inner", None)
        if getattr(live_transport, "stopped", None):
            # Engine.worker turns an exception into a failed outcome, so without
            # this the batch stop would read as a clean run with a failed turn.
            from sayuki_bot.evaluation.live import BudgetStopped

            raise BudgetStopped(live_transport.stopped)
        if scripted_transport is not None and scripted_transport.exhausted:
            raise ScriptExhausted(
                f"case {case['id']} ran out of scripted replies at "
                f"round {scripted_transport.exhausted}"
            )
        if guard_record["calls"]:
            raise ToolExecutionAttempted(", ".join(guard_record["calls"]))
        timeline.mark(DONE, outcome=outcome.get("status") if isinstance(outcome, dict) else None)
        return {
            "case_id": case["id"],
            "version": "4.6",
            # The harness ran cleanly; whether the turn itself succeeded is a
            # separate field, so a refused turn cannot read as a clean result.
            "harness_status": "ok",
            "engine_outcome": outcome,
            "pacing_mode": pacing,
            "timeline": timeline.events,
            "metrics": derive_metrics(timeline.events, scene.turn.sent, assembly_ms=assembly_ms),
            "requests": requests,
            "external_boundaries": guard_record["boundaries"],
            "guard_calls": guard_record["calls"],
            "usage": {
                "status": "unknown",
                "reason": "offline scripted transport returns no usage",
            },
            "transcript": scene.transcript,
        }
    except (ScriptExhausted, ToolExecutionAttempted, BudgetStopped) as exc:
        # Known harness-control failures: keep the partial evidence rather than
        # losing the case. Cancellation and interrupts are deliberately not
        # caught here, so a stopped run still stops.
        live_transport = getattr(transport, "inner", None)
        return partial_modern_result(
            case,
            exc,
            timeline,
            requests or getattr(live_transport, "requests", []),
            scene.turn.sent,
            outcome,
            pacing,
            assembly_ms,
            guard_record,
        )
    except asyncio.CancelledError as exc:
        # The cancellation still travels; it does not become a result. But what
        # this turn had already delivered is real, and the `finally` below is
        # about to close the scene, so the snapshot is taken here and handed to
        # the caller before the stop carries on.
        if on_cancel is not None:
            live_transport = getattr(transport, "inner", None)
            on_cancel(
                partial_modern_result(
                    case,
                    exc,
                    timeline,
                    requests or getattr(live_transport, "requests", []),
                    scene.turn.sent,
                    outcome,
                    pacing,
                    assembly_ms,
                    guard_record,
                    status="cancelled",
                )
            )
        raise
    finally:
        delivery_module.asyncio = sleeper._real
        channel.send = original_send
        # A worker still running at cleanup means the turn did not finish on its
        # own: cancel it first, or the "cleanup" would wait for it to run the
        # rest of the turn — more tool rounds and more delivered text — after the
        # batch had already been cancelled.
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await engine.close()
        await client.close()


def partial_modern_result(
    case,
    exc,
    timeline,
    requests,
    sent,
    outcome,
    pacing,
    assembly_ms,
    guard_record=None,
    status=None,
):
    """Keep what a failed case did produce instead of discarding the batch."""
    from sayuki_bot.evaluation.live import BudgetStopped

    return {
        "case_id": case["id"],
        "version": "4.6",
        # A batch-level stop is not a harness failure; both keep their evidence.
        "harness_status": status
        or ("stopped" if isinstance(exc, BudgetStopped) else "error"),
        "error": f"{type(exc).__name__}: {exc}",
        "engine_outcome": outcome,
        "pacing_mode": pacing,
        "timeline": list(timeline.events) if timeline else [],
        "metrics": derive_metrics(
            list(timeline.events) if timeline else [], list(sent), assembly_ms=assembly_ms
        ),
        "requests": list(requests),
        "external_boundaries": (guard_record or {}).get("boundaries", []),
        "guard_calls": (guard_record or {}).get("calls", []),
        "usage": {"status": "unknown", "reason": "case did not complete"},
    }


# ------------------------------------------------------- comparison + report


def _settings_of(request: dict) -> dict:
    from sayuki_bot.evaluation.comparison import COMPARED_SETTINGS

    return {name: request.get(name) for name in COMPARED_SETTINGS}


def _round_settings(legacy_requests, modern_requests) -> dict:
    """Keep every round's real fingerprint, and check every main-chat request.

    Two separate questions. The fingerprints (`message_roles`, `message_chars`,
    `messages_sha256` and the settings) are kept verbatim so a later reader can
    identify each round's input; no persona text is carried. The settings verdict
    is derived on top: a shared baseline is taken from round 1 when both versions
    agree, and **every** request on both sides is checked against it, extra
    continuations included. Differing round counts stay a flow difference rather
    than a loss, but an unchecked or off-baseline round cannot be reported as
    "settings are the same".
    """
    from sayuki_bot.evaluation.comparison import COMPARED_SETTINGS, _settings_match

    paired = min(len(legacy_requests), len(modern_requests))
    rounds = [
        {"round": index + 1, **_settings_match(legacy_requests[index], modern_requests[index])}
        for index in range(paired)
    ]
    unmatched = [
        {"round": index + 1, "version": "4.0", "request": legacy_requests[index]}
        for index in range(paired, len(legacy_requests))
    ] + [
        {"round": index + 1, "version": "4.6", "request": modern_requests[index]}
        for index in range(paired, len(modern_requests))
    ]

    baseline = None
    if (
        legacy_requests
        and modern_requests
        and _settings_of(legacy_requests[0]) == _settings_of(modern_requests[0])
    ):
        baseline = _settings_of(legacy_requests[0])

    checks = []
    for version, requests in (("4.0", legacy_requests), ("4.6", modern_requests)):
        for index, request in enumerate(requests, start=1):
            values = _settings_of(request)
            checks.append(
                {
                    "version": version,
                    "round": index,
                    "values": values,
                    "matches_baseline": None if baseline is None else values == baseline,
                    "differences": []
                    if baseline is None
                    else [
                        {"setting": name, "baseline": baseline[name], "actual": values[name]}
                        for name in COMPARED_SETTINGS
                        if values[name] != baseline[name]
                    ],
                }
            )
    off_baseline = [item for item in checks if item["matches_baseline"] is False]
    unverified = [item for item in checks if item["matches_baseline"] is None]
    return {
        "captured": bool(legacy_requests) and bool(modern_requests),
        "rounds_aligned": len(legacy_requests) == len(modern_requests),
        "comparable": bool(rounds)
        and baseline is not None
        and all(item["comparable"] for item in rounds)
        and not off_baseline
        and not unverified,
        "baseline": baseline,
        "rounds": rounds,
        "round_checks": checks,
        "off_baseline_rounds": off_baseline,
        "unverified_rounds": unverified,
        "unmatched_rounds": unmatched,
        "round_counts": {"v40": len(legacy_requests), "v46": len(modern_requests)},
        # Verbatim per-round request identity; message content is never included.
        "fingerprints": {"v40": list(legacy_requests), "v46": list(modern_requests)},
    }


def compare_flow(case: dict, legacy: dict, modern: dict) -> dict:
    """Put the two whole-turn replays side by side without ranking them."""
    legacy_ok = legacy.get("harness_status") == "ok"
    modern_ok = modern.get("harness_status") == "ok"
    if not (legacy_ok and modern_ok):
        return {
            "case_id": case["id"],
            "title": case.get("title"),
            "status": "error",
            "v40_harness_status": legacy.get("harness_status", "missing"),
            "v46_harness_status": modern.get("harness_status", "missing"),
            "v40_error": legacy.get("error"),
            "v46_error": modern.get("error"),
            # Partial evidence is kept so an aborted case is still reviewable.
            "partial": {
                "v40": {
                    "timeline": legacy.get("timeline", []),
                    "requests": legacy.get("requests", []),
                    "sent_segments": legacy.get("sent_segments", []),
                },
                "v46": {
                    "timeline": modern.get("timeline", []),
                    "requests": modern.get("requests", []),
                    "sent_segments": modern.get("metrics", {}).get("sent_segments", []),
                },
            },
        }
    left = derive_metrics(
        legacy["timeline"],
        legacy["sent_segments"],
        legacy["actions"],
        assembly_ms=legacy.get("assembly_ms"),
    )
    right = modern["metrics"]
    settings = _round_settings(legacy.get("requests", []), modern.get("requests", []))

    differences = []
    for field, note in (
        ("model_rounds", "每次主生成都會在付費比較中計入請求數與用量"),
        ("spoke_before_first_tool", "工具前是否已有可見文字，直接影響使用者感受到的第一段延遲"),
        ("visible_chunks", "實際送出的分段數量"),
        ("tool_calls", "工具計數依各版自己的分類：4.0 的動作標記不走查詢工具路徑"),
    ):
        if left[field] != right[field]:
            differences.append(
                {"field": field, "v40": left[field], "v46": right[field], "note": note}
            )
    if round(left["pacing"]["requested_ms"]) != round(right["pacing"]["requested_ms"]):
        differences.append(
            {
                "field": "pacing_requested_ms",
                "v40": left["pacing"]["requested_ms"],
                "v46": right["pacing"]["requested_ms"],
                "note": "人工等待要完整計入，不能從首段時間扣除",
            }
        )

    # Did the turn itself succeed, separately from whether the harness ran?
    legacy_outcome = legacy.get("engine_outcome")
    modern_outcome = modern.get("engine_outcome")
    expectation = case.get("expected_outcome")
    outcomes = {
        "v40": legacy_outcome,
        "v46": modern_outcome,
        "v46_delivered": bool(right["sent_segments"]),
        "v40_delivered": bool(left["sent_segments"]),
        "expected": expectation,
        "expectation_met": None
        if expectation is None
        else (modern_outcome or {}).get("status") == expectation,
    }
    status = "compared"
    if not outcomes["v40_delivered"] or not outcomes["v46_delivered"]:
        # A turn that delivered nothing must not read as a clean comparison.
        status = "compared-with-incomplete-turn"
    return {
        "case_id": case["id"],
        "title": case["title"],
        "task": case["task"],
        "status": status,
        "level": "E",
        "metrics": {"v40": left, "v46": right},
        "differences": differences,
        "request_settings": settings,
        "outcomes": outcomes,
        "scripted_replies": {
            "v40": case["scripted"]["v40"],
            "v46": case["scripted"]["v46"],
            "note": "工具標記語法依各版協定不同，任務與工具返回事實相同",
        },
        "timelines": {"v40": legacy["timeline"], "v46": modern["timeline"]},
        "tokens": {
            "status": "unknown",
            "reason": "scripted offline replay has no provider usage; model time is not measured",
        },
        "notes": case.get("notes", ""),
    }


def blind_transcript(segments, targets, tools, label: str) -> dict:
    """Reading copy without a version label.

    Not a blind review on its own: X and Y map to fixed versions and the answer
    key lives in the same report. A real blind pass needs a separate document
    with a stored, shuffled mapping.
    """
    return {
        "label": label,
        "segments": list(segments),
        "delivery_targets": list(targets),
        "tool_sequence": list(tools),
        "substantive_answer_index": None,
    }


def load_flow_cases(path) -> list[dict]:
    from sayuki_bot.evaluation.comparison import CaseError, validate_case

    cases, seen = [], set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        case = json.loads(line)
        validate_case(case, number)
        if case["level"] != "E":
            raise CaseError(f"line {number}: flow cases must declare level E")
        for version in ("v40", "v46"):
            if not case.get("scripted", {}).get(version):
                raise CaseError(
                    f"line {number}: case {case['id']} lacks scripted {version} replies"
                )
        if not case.get("task"):
            raise CaseError(f"line {number}: case {case['id']} lacks a shared task description")
        if case["id"] in seen:
            # Results are keyed by id; a duplicate would overwrite the earlier
            # case and pair its declared script with the later case's output.
            raise CaseError(f"line {number}: duplicate case id {case['id']}")
        seen.add(case["id"])
        refused = check_scripted_tags(case)
        if refused:
            raise CaseError(
                f"line {number}: case {case['id']} scripts a boundary the offline E layer "
                f"refuses: {refused}"
            )
        cases.append(case)
    if not cases:
        raise CaseError(f"No flow cases in {path}")
    return cases


def check_batch(cases) -> None:
    """Same identity rules for callers that build a batch in memory."""
    from sayuki_bot.evaluation.comparison import CaseError

    seen = set()
    for case in cases:
        if case["id"] in seen:
            raise CaseError(f"duplicate case id {case['id']} in this batch")
        seen.add(case["id"])
        refused = check_scripted_tags(case)
        if refused:
            raise CaseError(
                f"case {case['id']} scripts a boundary the offline E layer refuses: {refused}"
            )


def flow_limits(pacing: str, legacy_environment: dict) -> list[str]:
    limits = [
        "腳本回覆重播：模型時間為 0，這一輪只比較流程結構（輪數、工具序列、可見輸出順序、分段與等待），"
        "不能當成速度或品質比較。真實模型時間要等付費成對比較。",
        "沒有連 Discord、沒有網路、沒有真實工具外部呼叫。可到達的外部邊界一律被哨兵拒絕並列在 "
        "`external_boundaries`；腳本若寫入不支援的工具標記，案例在執行前就被拒絕。",
        "token 一律 unknown；`first_visible_ms`／`total_ms` 以入列為共同起點，是本機重播的牆鐘時間，"
        "不是使用者端延遲；場景建構成本另記在 `assembly_ms`。",
        "4.0 的 `digest_pending` 前置摘要沒有進入本層；4.0 的短期摘要與 4.6 的背景抽取在本輪都沒有執行，"
        "所以不能稱為完整端到端。",
        "4.0 把動作標記（例如 STATUS）走 `_apply_*` 路徑而不是查詢工具迴圈，所以兩版的 tool_calls 定義本就不同，"
        "報告分別列出而不換算。",
        "`--order` 目前只交換「整批 4.0 先跑／整批 4.6 先跑」，**不是**計劃要求的同案例 AB／BA 成對交錯；"
        "盲評稿的 X 固定對應 4.6、Y 固定對應 4.0，而且與答案在同一份報告，因此**不構成正式盲評**。",
        "真實模型階段仍待補：固定供應商與關閉 fallback、每案例多次成對交錯觀測、"
        "usage／finish reason／重試／cached token 的保存、實際模型呼叫上限與停測條件，"
        "以及另存保留映射的獨立盲評閱讀稿。",
    ]
    if pacing == PACING_SIMULATED:
        limits.append(
            "分段等待採記錄不睡眠：`pacing.requested_ms` 是各版真的要求的人工等待，已完整計入並保留為事件，"
            "但牆鐘時間不含它；不能拿牆鐘時間宣稱任何一版比較快。"
        )
    if legacy_environment.get("opencc") == "identity-shim":
        limits.append(
            "本輪環境沒有 opencc：4.0 在模組匯入時需要它，因此注入 identity 轉換器；"
            "4.6 已按使用者決策移除自動繁簡轉換。4.0 的 identity 墊片不執行原轉換，"
            "因此仍屬舊版環境偏差，不能宣稱完整重現正式4.0的文字轉換。"
        )
    if legacy_environment.get("aiofiles") == "stdlib-shim":
        limits.append("aiofiles 仍為 stdlib 墊片（沿用 O1-R1 的揭露範圍），只替換非同步檔案開啟。")
    return limits


async def run_flow_comparison(
    cases,
    *,
    config,
    persona_path,
    model,
    legacy_root=None,
    python=None,
    pacing=PACING_SIMULATED,
    order="v40-first",
    work_root=None,
    on_case=None,
    journal=None,
    run_id=None,
):
    """Replay every flow on both versions and build the E-layer report.

    ``journal`` is called the moment each version finishes each case, before the
    other version has started, so an interrupted batch still leaves valid records
    behind. ``on_case`` additionally receives the merged comparison. The run id is
    created before anything executes so every record can be tied to this run.
    """
    import tempfile
    from datetime import datetime
    from uuid import uuid4

    from sayuki_bot.evaluation import legacy_v4
    from sayuki_bot.evaluation.comparison import modern_environment

    check_batch(cases)
    run_id = run_id or uuid4().hex
    effort = config.get("model", "reasoning_effort", "") or ""

    def record(version, result):
        if not journal:
            return
        journal(
            {
                "kind": "version-case",
                "run_id": run_id,
                "order": order,
                "version": version,
                "case_id": result.get("case_id"),
                "harness_status": result.get("harness_status"),
                "engine_outcome": result.get("engine_outcome"),
                "requests": result.get("requests", []),
                "sent_segments": (
                    result.get("sent_segments")
                    or result.get("metrics", {}).get("sent_segments", [])
                ),
                "timeline": result.get("timeline", []),
                "error": result.get("error"),
            }
        )

    async def run_legacy():
        return legacy_v4.run_flow_cases(
            cases,
            legacy_root=legacy_root,
            persona_path=persona_path,
            model=model,
            history_limit=config.get("conversation", "history_limit", 25),
            use_reasoning_effort=bool(effort),
            reasoning_effort=effort or "medium",
            pacing=pacing,
            python=python,
            on_record=lambda result: record("4.0", result),
        )

    async def run_modern():
        results = {}
        with tempfile.TemporaryDirectory(prefix="flow46-") as directory:
            for index, case in enumerate(cases):
                work_dir = Path(work_root or directory) / f"case-{index}-{case['id']}"
                work_dir.mkdir(parents=True, exist_ok=True)
                try:
                    results[case["id"]] = await run_v46_flow(
                        case, config, work_dir, model, pacing=pacing
                    )
                except Exception as exc:
                    # Unexpected failures stop this case only; CancelledError and
                    # KeyboardInterrupt are BaseExceptions and still propagate.
                    results[case["id"]] = {
                        "case_id": case["id"],
                        "version": "4.6",
                        "harness_status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "timeline": [],
                        "requests": [],
                        "metrics": derive_metrics([], []),
                    }
                record("4.6", results[case["id"]])
        return results

    if order == "v46-first":
        modern_results = await run_modern()
        legacy_output = await run_legacy()
    else:
        legacy_output = await run_legacy()
        modern_results = await run_modern()
    legacy_results = {item["case_id"]: item for item in legacy_output["results"]}

    compared, blind = [], []
    for case in cases:
        legacy = legacy_results.get(
            case["id"],
            {"harness_status": "missing", "error": "4.0 flow worker returned no result"},
        )
        modern = modern_results[case["id"]]
        entry = compare_flow(case, legacy, modern)
        compared.append(entry)
        if on_case:
            on_case(entry)
        if journal:
            journal({"kind": "comparison", "run_id": run_id, "case": entry})
        blind.append(
            {
                "case_id": case["id"],
                "task": case["task"],
                "mapping_note": "X 固定為 4.6、Y 固定為 4.0；這是閱讀稿，不是盲評",
                "X": blind_transcript(
                    modern.get("metrics", {}).get("sent_segments", []),
                    modern.get("metrics", {}).get("delivery_targets", []),
                    modern.get("metrics", {}).get("tool_sequence", []),
                    "X",
                ),
                "Y": blind_transcript(
                    legacy.get("sent_segments", []),
                    [
                        {"chars": e.get("chars"), "reply_to": e.get("reply_to")}
                        for e in legacy.get("timeline", [])
                        if e["kind"] == VISIBLE
                    ],
                    [
                        {"tool": e.get("tool"), "status": e.get("status")}
                        for e in legacy.get("timeline", [])
                        if e["kind"] == TOOL_RESULT
                    ],
                    "Y",
                ),
            }
        )
    return {
        "run_id": run_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "mode": "offline-flow-replay",
        "level": "E",
        "pacing_mode": pacing,
        "order": order,
        "order_note": "整批版本先後，不是同案例 AB／BA 成對交錯",
        "case_count": len(cases),
        "compared_count": sum(1 for item in compared if item["status"].startswith("compared")),
        "settings": {
            "model": model,
            "reasoning_effort": effort or None,
            "persona": {
                "path": str(persona_path),
                "sha256": legacy_v4.sha256_file(persona_path),
            },
            "network": "none",
            "credentials": "none read",
            "model_calls": "scripted replay; no provider request was sent",
        },
        "versions": {"v40": legacy_output["environment"], "v46": modern_environment()},
        "external_boundaries": (
            legacy_output["environment"].get("external_boundaries", [])
            + next(
                (
                    item.get("external_boundaries", [])
                    for item in modern_results.values()
                    if item.get("external_boundaries")
                ),
                [],
            )
        ),
        "cases": compared,
        "blind_transcripts": blind,
        "limits": flow_limits(pacing, legacy_output["environment"]),
    }


def summarize_flows(report: dict) -> dict:
    rows = [case for case in report["cases"] if case["status"].startswith("compared")]
    clean = [case for case in rows if case["status"] == "compared"]
    return {
        "compared": len(rows),
        "incomplete_turns": [case["case_id"] for case in rows if case["status"] != "compared"],
        "errors": [
            {"case_id": case["case_id"], "v40": case.get("v40_error"), "v46": case.get("v46_error")}
            for case in report["cases"]
            if case["status"] == "error"
        ],
        "cases_with_differences": [case["case_id"] for case in rows if case["differences"]],
        "settings_comparable_all": all(case["request_settings"]["comparable"] for case in rows)
        if rows
        else False,
        "settings_problems": [
            {
                "case_id": case["case_id"],
                "comparable": case["request_settings"]["comparable"],
                "rounds_aligned": case["request_settings"]["rounds_aligned"],
                "rounds": [
                    item for item in case["request_settings"]["rounds"] if not item["comparable"]
                ],
                "off_baseline_rounds": case["request_settings"]["off_baseline_rounds"],
                "unverified_rounds": case["request_settings"]["unverified_rounds"],
                "unmatched_rounds": case["request_settings"]["unmatched_rounds"],
            }
            for case in rows
            if not case["request_settings"]["comparable"]
            or not case["request_settings"]["rounds_aligned"]
        ],
        "model_rounds": {
            "v40": sum(case["metrics"]["v40"]["model_rounds"] for case in clean),
            "v46": sum(case["metrics"]["v46"]["model_rounds"] for case in clean),
        },
        "visible_chunks": {
            "v40": sum(case["metrics"]["v40"]["visible_chunks"] for case in clean),
            "v46": sum(case["metrics"]["v46"]["visible_chunks"] for case in clean),
        },
        "pacing_requested_ms": {
            "v40": round(
                sum(case["metrics"]["v40"]["pacing"]["requested_ms"] for case in clean), 3
            ),
            "v46": round(
                sum(case["metrics"]["v46"]["pacing"]["requested_ms"] for case in clean), 3
            ),
        },
        "spoke_before_first_tool": {
            "v40": [c["case_id"] for c in clean if c["metrics"]["v40"]["spoke_before_first_tool"]],
            "v46": [c["case_id"] for c in clean if c["metrics"]["v46"]["spoke_before_first_tool"]],
        },
        "tokens": "unknown (scripted offline replay)",
    }


# ------------------------------------------------------------ live (E, paired)


async def run_live_comparison(
    cases,
    *,
    config,
    persona_path,
    model,
    transport,
    replicates=1,
    legacy_root=None,
    python=None,
    pacing=PACING_SIMULATED,
    journal=None,
    run_id=None,
    work_root=None,
    start_with="4.0",
):
    """Run the same flows against a real model, one version at a time, in pairs.

    Unlike the offline entry — which runs one whole version and then the other —
    this interleaves per case (AB, then BA on the next replicate) so a drift in
    provider load cannot land on one version only. Every run carries
    (case_id, pair_id, replicate, version); rounds are counted separately,
    because one case is not one model request.

    The 4.0 side still runs in its isolated child: it asks this process for each
    completion over the pipe, so the API key never leaves here.
    """
    import tempfile
    from datetime import datetime
    from uuid import uuid4

    from sayuki_bot.evaluation import legacy_v4
    from sayuki_bot.evaluation.comparison import modern_environment
    from sayuki_bot.evaluation.live import (
        BudgetStopped,
        fairness_verdict,
        live_limits,
        summarize_live,
        provider_evidence,
        settings_baseline,
    )

    check_batch(cases)
    run_id = run_id or uuid4().hex
    by_id = {case["id"]: case for case in cases}
    schedule = legacy_v4_pair_schedule(cases, replicates, start_with)
    loop = asyncio.get_running_loop()
    results: dict[tuple, dict] = {}
    legacy_environment = {}
    stopped_reason = None

    ledger = []

    def collect(record):
        """Every model-call the transport reports, kept for the fairness check.

        Forwarded to both the batch journal and any journal the transport was
        built with, without writing the same record twice when they are the same.
        """
        if record.get("kind") == "model-call":
            ledger.append(record)
        sinks = []
        for sink in (journal, transport.journal):
            if sink and not any(sink is existing for existing in sinks):
                sinks.append(sink)
        for sink in sinks:
            sink(record)

    def record(entry, result):
        if not journal:
            return
        journal(
            {
                "kind": "version-case",
                "run_id": run_id,
                "case_id": entry["case_id"],
                "pair_id": entry["pair_id"],
                "replicate": entry["replicate"],
                "version": entry["version"],
                "position": entry["position"],
                "order": entry["order"],
                "harness_status": result.get("harness_status"),
                "engine_outcome": result.get("engine_outcome"),
                "requests": result.get("requests", []),
                "sent_segments": (
                    result.get("sent_segments")
                    or result.get("metrics", {}).get("sent_segments", [])
                ),
                "timeline": result.get("timeline", []),
                "error": result.get("error"),
            }
        )

    with tempfile.TemporaryDirectory(prefix="live46-") as directory:
        for entry in schedule:
            if stopped_reason:
                break
            case = by_id[entry["case_id"]]
            bound = transport.for_case(
                journal=collect,
                run_id=run_id,
                case_id=entry["case_id"],
                pair_id=entry["pair_id"],
                replicate=entry["replicate"],
                version=entry["version"],
            )
            # Filled in by the version that was running when the stop arrived,
            # while its scene and timeline still existed.
            cancelled: dict = {}

            def keep(result, _cancelled=cancelled):
                _cancelled["result"] = result

            try:
                if entry["version"] == "4.6":
                    work_dir = (
                        Path(work_root or directory) / f"{entry['pair_id']}-{entry['version']}"
                    )
                    work_dir.mkdir(parents=True, exist_ok=True)
                    result = await run_v46_flow(
                        case,
                        config,
                        work_dir,
                        model,
                        pacing=pacing,
                        transport=bound,
                        on_cancel=keep,
                    )
                else:
                    result, legacy_environment = await _run_legacy_live(
                        case,
                        bound,
                        loop,
                        config=config,
                        persona_path=persona_path,
                        model=model,
                        legacy_root=legacy_root,
                        python=python,
                        pacing=pacing,
                        on_cancel=keep,
                    )
            except asyncio.CancelledError:
                # Per-version cleanup has already run. Keep the turn that was
                # actually in flight — the segments it had delivered, where they
                # went and the events it had recorded — on the journal before the
                # cancellation travels, because nothing downstream can rebuild
                # it once the scene and the child are gone.
                stopped_reason = stopped_reason or "the batch was cancelled"
                partial = cancelled.get("result")
                if partial is not None:
                    key = (entry["case_id"], entry["replicate"], entry["version"])
                    results[key] = partial
                    record(entry, partial)
                raise
            except BudgetStopped as stop:
                stopped_reason = str(stop)
                result = {
                    "case_id": case["id"],
                    "version": entry["version"],
                    "harness_status": "stopped",
                    "error": f"BudgetStopped: {stop}",
                    "timeline": [],
                    "requests": [],
                    "metrics": derive_metrics([], []),
                }
            except Exception as exc:
                result = {
                    "case_id": case["id"],
                    "version": entry["version"],
                    "harness_status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "timeline": [],
                    "requests": [],
                    "metrics": derive_metrics([], []),
                }
            results[(entry["case_id"], entry["replicate"], entry["version"])] = result
            record(entry, result)
            if bound.stopped and not stopped_reason:
                stopped_reason = bound.stopped

    compared = []
    for case in cases:
        for replicate in range(1, replicates + 1):
            legacy = results.get((case["id"], replicate, "4.0"))
            modern = results.get((case["id"], replicate, "4.6"))
            if legacy is None and modern is None:
                continue
            entry = compare_flow(
                case,
                legacy or {"harness_status": "not-run", "error": stopped_reason},
                modern or {"harness_status": "not-run", "error": stopped_reason},
            )
            pair_id = f"{case['id']}#r{replicate}"
            pair_ledger = [item for item in ledger if item.get("pair_id") == pair_id]
            provider = provider_evidence(pair_ledger, requested=transport.provider)
            entry.update(
                pair_id=pair_id,
                replicate=replicate,
                raw_model_text={
                    "v40": _raw_texts(legacy),
                    "v46": _raw_texts(modern),
                },
                # Settings and provider are separate questions; neither stands in
                # for the other, and the report shows both with their reasons.
                fairness=fairness_verdict(provider, entry.get("request_settings") or {}),
                # The event identity travels with the summary so several events
                # for one request merge back into a single call.
                model_calls=[
                    {
                        key: item[key]
                        for key in (
                            "kind",
                            "event",
                            "request_id",
                            "version",
                            "round",
                            "status",
                            "response_id",
                            "provider",
                            "usage",
                            "finish_reason",
                            "settings",
                            "duration_ms",
                            "reason",
                            "error_type",
                        )
                        if key in item
                    }
                    for item in pair_ledger
                ],
            )
            compared.append(entry)
            if journal:
                journal({"kind": "comparison", "run_id": run_id, "case": entry})

    report = {
        "run_id": run_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "mode": "live-flow-paired",
        "level": "E",
        "pacing_mode": pacing,
        "replicates": replicates,
        "schedule": schedule,
        "stopped_reason": stopped_reason,
        "settings": {
            "baseline": settings_baseline(config, model),
            "requested_provider": transport.provider,
            "persona": {
                "path": str(persona_path),
                "sha256": legacy_v4.sha256_file(persona_path),
            },
            "model_calls": "real requests through the recorded transport",
        },
        "budget": transport.budget.snapshot(),
        "versions": {"v40": legacy_environment, "v46": modern_environment()},
        "cases": compared,
        "fair_comparison": bool(compared)
        and all(case.get("fairness", {}).get("comparable") for case in compared),
        "provider_consistent": bool(compared)
        and all(case.get("fairness", {}).get("provider_consistent") for case in compared),
        # A live run has its own limits; the offline replay's wording ("no
        # network", "model time is 0", fixed X/Y) would be false here.
        "limits": live_limits(
            all(case.get("fairness", {}).get("routing_verified") for case in compared)
            if compared
            else False,
            stopped_reason,
        )
        + _environment_limits(legacy_environment),
    }
    report["summary"] = summarize_live(report)
    return report


def _environment_limits(legacy_environment):
    """The shim disclosures that apply in any mode."""
    limits = []
    if legacy_environment.get("opencc") == "identity-shim":
        limits.append(
            "本輪環境沒有 opencc：4.0 在模組匯入時需要它，因此注入 identity 轉換器；"
            "4.6 已移除自動繁簡轉換。此墊片仍是4.0的環境偏差，不代表正式舊版文字轉換。"
        )
    if legacy_environment.get("aiofiles") == "stdlib-shim":
        limits.append("aiofiles 仍為 stdlib 墊片，只替換非同步檔案開啟。")
    return limits


def legacy_v4_pair_schedule(cases, replicates, start_with):
    from sayuki_bot.evaluation.live import pair_schedule

    return pair_schedule(cases, replicates, start_with=start_with)


# A cancelled 4.0 turn is cleaned up locally: the stop wakes the worker, the
# worker reaps its own group, and the confirmation below is what the caller waits
# for. Both bounds are short because neither waits for the turn to finish.
CANCEL_JOIN_SECONDS = 5.0
CANCEL_CLEANUP_SECONDS = 5.0


def _cancelled_legacy_result(case, snapshot, pacing):
    """The partial 4.0 turn, built from what the child streamed before the stop.

    The child's own events are the source: delivered segments, their targets and
    the request/tool events it had already reported. Nothing here is inferred
    from the model's raw text, and nothing is asked of the child after it is
    stopped.
    """
    snapshot = snapshot or {}
    timeline = list(snapshot.get("timeline", []))
    sent = list(snapshot.get("sent_segments", []))
    return {
        "case_id": case["id"],
        "version": "4.0",
        "harness_status": "cancelled",
        "error": "CancelledError: the batch was cancelled during this turn",
        "pacing_mode": pacing,
        "timeline": timeline,
        "sent_segments": sent,
        "requests": list(snapshot.get("requests", [])),
        "assembly_ms": snapshot.get("assembly_ms"),
        "metrics": derive_metrics(timeline, sent, assembly_ms=snapshot.get("assembly_ms")),
        "usage": {"status": "unknown", "reason": "case did not complete"},
    }


def _raw_texts(result):
    """The model's own text, kept apart from what was actually delivered."""
    if not result:
        return []
    return [
        event.get("raw_text")
        for event in result.get("timeline", [])
        if event.get("kind") == MODEL_RESPONSE and event.get("raw_text")
    ]


async def _run_legacy_live(
    case,
    transport,
    loop,
    *,
    config,
    persona_path,
    model,
    legacy_root,
    python,
    pacing,
    on_cancel=None,
):
    """Drive 4.0's isolated child and serve its completion requests from here."""
    from sayuki_bot.evaluation import legacy_v4
    from sayuki_bot.evaluation.live import BudgetStopped

    async def complete(record):
        kwargs = {**record.get("request", {}), "messages": record.get("messages", [])}
        response = await transport.call(kwargs)
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        return {
            "kind": "model_response",
            "text": getattr(choice.message, "content", "") or "",
            "finish_reason": getattr(choice, "finish_reason", None) or "stop",
            "usage_status": "reported" if usage else "unknown",
            # The fingerprint of what the parent actually sent, routing included,
            # so both versions record the same kind of evidence.
            "fingerprint": transport.requests[-1] if transport.requests else None,
        }

    inflight = set()

    def on_model_request(record, remaining=None):
        # Runs on the subprocess-driving thread; the call itself happens back on
        # the event loop, where the key and the budget live. The wait is bounded
        # by whatever is left of the batch deadline, and a future that runs past
        # it is cancelled rather than left to issue another completion.
        import concurrent.futures

        future = asyncio.run_coroutine_threadsafe(complete(record), loop)
        inflight.add(future)
        try:
            return future.result(timeout=remaining)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return {"kind": "model_stopped", "reason": "the batch deadline expired"}
        except BudgetStopped as stop:
            return {"kind": "model_stopped", "reason": str(stop)}
        except (asyncio.CancelledError, concurrent.futures.CancelledError, KeyboardInterrupt):
            # A cancelled batch stops; it does not become an ordinary refusal
            # that the next case would carry on from. run_coroutine_threadsafe
            # reports a cancelled coroutine as concurrent.futures.CancelledError,
            # which is a different class from asyncio's, so both are handled and
            # re-raised in asyncio's form for the caller.
            future.cancel()
            raise asyncio.CancelledError("the live proxy was cancelled")
        except Exception as exc:
            future.cancel()
            return {"kind": "model_stopped", "reason": f"{type(exc).__name__}: {exc}"}
        finally:
            inflight.discard(future)

    def abandon():
        for pending in list(inflight):
            pending.cancel()

    on_model_request.abandon = abandon

    effort = config.get("model", "reasoning_effort", "") or ""
    # The child cannot be asked for its evidence once it has been killed, so it
    # streams a snapshot as it goes and the newest one is kept here.
    progress: dict = {}

    def on_progress(record):
        progress["latest"] = record

    # Cancelling an `await` does not stop a thread, so the run owns an explicit
    # stop signal and its own thread handle. StopSignal is not a bare flag: the
    # worker registers a wakeup with it, so a stop reaches a wait that is already
    # blocked instead of only being seen the next time the flag is polled.
    stop_event = legacy_v4.StopSignal()
    finished = asyncio.Event()
    holder: dict = {}

    def drive():
        try:
            holder["output"] = legacy_v4.run_flow_cases(
                [case],
                legacy_root=legacy_root,
                persona_path=persona_path,
                model=model,
                history_limit=config.get("conversation", "history_limit", 25),
                use_reasoning_effort=bool(effort),
                reasoning_effort=effort or "medium",
                pacing=pacing,
                python=python,
                on_model_request=on_model_request,
                on_progress=on_progress,
                stop_event=stop_event,
            )
        except BaseException as exc:  # carried back to this coroutine
            holder["error"] = exc
        finally:
            loop.call_soon_threadsafe(finished.set)

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    try:
        await finished.wait()
    except asyncio.CancelledError:
        # Setting the signal wakes the worker wherever it is parked — the stdout
        # queue, the model callback, the reply write-back or the exit wait — and
        # reaps the group it owns. The join is therefore short, and it is not
        # taken as proof of anything: the groups are confirmed gone separately.
        stop_event.set()
        on_model_request.abandon()
        await asyncio.to_thread(thread.join, CANCEL_JOIN_SECONDS)
        leftover = await asyncio.to_thread(stop_event.confirm_cleanup, CANCEL_CLEANUP_SECONDS)
        if on_cancel is not None:
            on_cancel(_cancelled_legacy_result(case, progress.get("latest"), pacing))
        if leftover:
            # Never report a timed-out join as a completed cleanup. The stop
            # still travels as a cancellation — turning it into another
            # exception class would break every caller that awaits it — but it
            # says plainly that the group could not be confirmed stopped.
            unconfirmed = legacy_v4.CleanupUnconfirmed(leftover)
            raise asyncio.CancelledError(str(unconfirmed)) from unconfirmed
        raise
    if "error" in holder:
        raise holder["error"]
    output = holder["output"]
    result = (
        output["results"][0]
        if output["results"]
        else {
            "case_id": case["id"],
            "harness_status": "error",
            "error": "the worker returned no result",
        }
    )
    # A stop is reported through the result the child actually produced, which
    # still carries the requests, timeline and delivered text from before it.
    if transport.stopped and result.get("harness_status") == "ok":
        result = dict(result, harness_status="stopped", error=f"BudgetStopped: {transport.stopped}")
    return result, output["environment"]
