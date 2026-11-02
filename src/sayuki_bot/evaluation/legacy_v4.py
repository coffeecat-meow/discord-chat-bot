"""4.0 adapter: the original assembly functions, executed in an isolated child process.

Both projects ship a package named ``sayuki_bot``. The parent side (a 4.6 process)
never imports 4.0; it spawns a child whose ``sys.path`` contains the 4.0 project
root and explicitly excludes every 4.6 location, so the name can only resolve to
4.0. The child then calls the genuine originals

    sayuki_bot.prompts.load_system_prompt
    sayuki_bot.message_context.build_chat_history
    sayuki_bot.message_context.build_system_context

assembled in the order ``sayuki_bot/bot_app.py`` uses for an ordinary message,
and reports the resolved module paths and their hashes so the call is traceable.
No prompt text is re-written here; fake Discord/model objects only satisfy the
interfaces the originals expect.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

WORKER_TIMEOUT_SECONDS = 600

# bot_app.py keeps the relevant-memory selection inside create_bot(), so it cannot
# be imported without building a real Discord bot. The rule below is copied from
# that nested helper; SOURCE_WIRING pins the region so a drift in 4.0 is caught by
# tests instead of silently changing the baseline.
SOURCE_WIRING = {
    "path": "sayuki_bot/bot_app.py",
    "function": "create_bot._collect_relevant_memory_user_ids",
    "first_line": 234,
    "last_line": 256,
    "sha256": "77ec581a480bbce2f95445432752809a3318d02e916e040cb2d426a017a73fa5",
}


class FirstRequestCaptured(BaseException):
    """Ends the P layer as soon as a version's own code has built its request.

    Deliberately a BaseException: 4.0's ``generate_async`` wraps its loop in
    ``except Exception`` and would otherwise swallow this into a normal LLM error
    and retry. Raising it from inside the fake transport means the original code
    builds and submits exactly one request, and nothing past that point runs --
    no response parsing, no SEARCH/MATH_CALC matching, no tool execution.
    """


class ToolExecutionAttempted(BaseException):
    """Safety net: a real tool entry point was reached during the P layer."""


class FlowWorkerStopped(BaseException):
    """The caller asked this run to stop; the child and its group are cleaned up."""


class CleanupUnconfirmed(RuntimeError):
    """A stop finished without proving the owned process group had gone.

    Raised instead of returning quietly, because "the join timed out" and "the
    child is gone" are different outcomes and only one of them is cleanup.
    """

    def __init__(self, groups):
        self.groups = sorted(groups)
        super().__init__(
            "could not confirm the 4.0 process group(s) "
            f"{self.groups} had stopped after the cancellation"
        )


class StopSignal:
    """A stop flag that also *wakes* whatever is currently blocked on it.

    A bare ``threading.Event`` can only be polled, so a driver already parked in
    ``queue.get`` or ``Thread.join`` never sees it and the caller ends up waiting
    out its own join timeout. Each run registers a waker while it owns a child;
    setting the signal runs those wakers, which close the pipes and reap the
    group so every blocking wait returns at once.

    Setting it never extends any deadline: the wakers only unblock waits, they
    do not restart the clock that bounds them.
    """

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._wakers = []
        self._groups = set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout=None) -> bool:
        return self._event.wait(timeout)

    def set(self) -> None:
        with self._lock:
            self._event.set()
            wakers = list(self._wakers)
        for waker in wakers:
            _safely(waker)

    def add_waker(self, waker) -> None:
        """Register a wakeup, running it straight away if the stop already fired."""
        with self._lock:
            if not self._event.is_set():
                self._wakers.append(waker)
                return
        _safely(waker)

    def discard_waker(self, waker) -> None:
        with self._lock:
            self._wakers = [item for item in self._wakers if item is not waker]

    # -- process groups this signal is responsible for ---------------------

    def own_group(self, group) -> None:
        if group is None:
            return
        with self._lock:
            self._groups.add(group)

    def release_group(self, group) -> None:
        """This group was reaped and confirmed gone; stop tracking it."""
        with self._lock:
            self._groups.discard(group)

    def confirm_cleanup(self, limit=5.0):
        """Return the owned groups that are still alive after one more kill.

        Blocking; meant to be called off the event loop. An empty result is the
        only evidence that the cancellation left nothing behind.
        """
        from time import monotonic

        with self._lock:
            groups = set(self._groups)
        if not groups or not _GROUP_SIGNALS:
            return set()
        deadline = monotonic() + max(0.0, limit)
        while True:
            alive = set()
            for group in groups:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    continue
                except (PermissionError, OSError):  # pragma: no cover - defensive
                    continue
                alive.add(group)
                _safely(lambda item=group: os.killpg(item, signal.SIGKILL))
            if not alive or monotonic() >= deadline:
                with self._lock:
                    self._groups &= alive
                return alive
            time.sleep(0.02)


def _safely(call):
    """Run a cleanup callback; a failure there must not mask the stop itself."""
    try:
        call()
    except (ProcessLookupError, PermissionError, BrokenPipeError, ValueError, OSError):
        pass


class LiveStopped(BaseException):
    """The parent refused a completion: budget, settings or transport failure.

    A BaseException for the same reason as the others here — 4.0's
    ``generate_async`` wraps its loop in ``except Exception`` and would otherwise
    absorb a batch-level stop into its own retry and fallback prose.
    """


class ScriptExhausted(BaseException):
    """An E-layer case ran out of scripted replies.

    A BaseException for the same reason as FirstRequestCaptured: 4.0's
    ``generate_async`` would otherwise catch it, retry, and return its own
    "恍神了" fallback, which would be recorded as if the model had really said it.
    """


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def default_legacy_root() -> Path:
    """`sayuki-4.0/` next to this project; the adapter never writes into it."""
    from sayuki_bot.config import ROOT

    return (ROOT.parent / "sayuki-4.0").resolve()


def child_environment(legacy_root: Path) -> dict:
    """A child env that cannot import 4.6, write bytecode, or reach a paid model."""
    dropped = {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "SAYUKI_CONFIG",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "LLM_API_KEY",
        "OPENAI_BASE_URL",
    }
    env = {key: value for key, value in os.environ.items() if key not in dropped}
    env["PYTHONPATH"] = str(legacy_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MPLBACKEND"] = "Agg"
    return env


def run_cases(
    cases,
    *,
    legacy_root=None,
    persona_path,
    model,
    history_limit=25,
    use_reasoning_effort=False,
    reasoning_effort="medium",
    python=None,
    timeout=WORKER_TIMEOUT_SECONDS,
    stop_after_first_request=True,
    monotonic_step_seconds=None,
):
    """Assemble every case with the real 4.0 functions and return their results."""
    legacy_root = Path(legacy_root or default_legacy_root()).resolve()
    if not (legacy_root / "sayuki_bot" / "message_context.py").is_file():
        raise ValueError(f"No 4.0 source at {legacy_root}")
    payload = {
        "legacy_root": str(legacy_root),
        "forbidden_roots": [str(Path(__file__).resolve().parents[2])],
        "persona_path": str(Path(persona_path).resolve()),
        "model": model,
        "history_limit": history_limit,
        "use_reasoning_effort": use_reasoning_effort,
        "reasoning_effort": reasoning_effort,
        # Only the regression test that proves the tool sentinels fire turns this
        # off; the comparison entry point always stops at the first request.
        "stop_after_first_request": stop_after_first_request,
        "monotonic_step_seconds": monotonic_step_seconds,
        "cases": list(cases),
    }
    completed = subprocess.run(
        [python or sys.executable, "-s", str(Path(__file__).resolve()), "--worker"],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        env=child_environment(legacy_root),
        cwd=str(legacy_root.parent),
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"4.0 worker failed (exit {completed.returncode}): {completed.stderr[-4000:]}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Unreadable 4.0 worker output: {completed.stdout[:2000]}") from exc


# --------------------------------------------------------------------- child


def _install_tool_sentinels(llm_module, math_module, scheduler_module=None, search_module=None):
    """Refuse every external boundary 4.0 can reach, and publish the inventory.

    The P layer stops before generation can reach a tool at all; the E layer runs
    the whole turn, so the guard has to cover every entry point that could leave
    the machine rather than only the two the P layer could hit. Each boundary is
    either replaced with a case-provided result (none are, this round) or refused
    loudly, and the inventory is reported so the coverage can be audited instead
    of inferred from "the six cases happened not to trigger it".

    `search_web` and `read_web_page` both drive an external browser through
    ``asyncio.create_subprocess_exec``; that call is guarded too, so a binding
    this list missed still cannot spawn anything.
    """
    record = {"installed": [], "calls": [], "boundaries": []}

    def refuse(label, reason):
        def guard(*args, **kwargs):
            record["calls"].append(label)
            raise ToolExecutionAttempted(f"offline replay reached the real {label}: {reason}")

        return guard

    def install(module, attribute, label, reason, *, static=False):
        if module is None or not hasattr(module, attribute):
            return
        guard = refuse(label, reason)
        setattr(module, attribute, staticmethod(guard) if static else guard)
        record["installed"].append(label)
        record["boundaries"].append(
            {"boundary": label, "mode": "refused", "reason": reason, "version": "4.0"}
        )

    install(llm_module, "search_web", "sayuki_bot.llm.search_web", "external browser subprocess")
    install(
        search_module, "search_web", "sayuki_bot.search.search_web", "external browser subprocess"
    )
    install(
        search_module,
        "read_web_page",
        "sayuki_bot.search.read_web_page",
        "external browser subprocess",
    )
    install(
        scheduler_module,
        "read_web_page",
        "sayuki_bot.scheduler.read_web_page",
        "external browser subprocess; READ_WEB has no offline fixture this round",
    )
    if math_module is not None:
        math_module.MathToolkit.calculate = staticmethod(
            refuse("math_tools.MathToolkit.calculate", "unbounded local computation")
        )
        record["installed"].append("math_tools.MathToolkit.calculate")
        record["boundaries"].append(
            {
                "boundary": "math_tools.MathToolkit.calculate",
                "mode": "refused",
                "reason": "unbounded local computation",
                "version": "4.0",
            }
        )
    if search_module is not None and hasattr(search_module, "asyncio"):
        # Backstop: even a binding this list missed cannot spawn the browser.
        search_module.asyncio = _SubprocessGuard(
            search_module.asyncio, refuse("asyncio.create_subprocess_exec", "subprocess spawn")
        )
        record["installed"].append("sayuki_bot.search.asyncio.create_subprocess_exec")
        record["boundaries"].append(
            {
                "boundary": "asyncio.create_subprocess_exec",
                "mode": "refused",
                "reason": "subprocess spawn backstop",
                "version": "4.0",
            }
        )
    return record


class _SubprocessGuard:
    """Delegates to asyncio but refuses to spawn a process."""

    def __init__(self, real, guard):
        self._real, self._guard = real, guard

    def __getattr__(self, name):
        return getattr(self._real, name)

    def create_subprocess_exec(self, *args, **kwargs):
        return self._guard(*args, **kwargs)


def _install_aiofiles_shim():
    """Let 4.0's genuine memory modules run where `aiofiles` is not installed.

    The independent 4.6 runtime does not require `aiofiles`. Only the async
    file-open wrapper in the isolated legacy worker is replaced; every formatting,
    merge and truncation rule inside `sayuki_bot.memory` stays the original code.
    Returns "real" when the genuine package is importable, "stdlib-shim" otherwise.
    """
    import importlib.util

    if importlib.util.find_spec("aiofiles") is not None:
        return "real"

    import types

    class _AsyncFile:
        def __init__(self, handle):
            self._handle = handle

        async def read(self, *args):
            return self._handle.read(*args)

        async def write(self, data):
            return self._handle.write(data)

        async def close(self):
            self._handle.close()

    class _AsyncOpen:
        def __init__(self, *args, **kwargs):
            self._args, self._kwargs, self._handle = args, kwargs, None

        async def __aenter__(self):
            self._handle = open(*self._args, **self._kwargs)  # noqa: SIM115
            return _AsyncFile(self._handle)

        async def __aexit__(self, *exc_info):
            self._handle.close()
            return False

    module = types.ModuleType("aiofiles")
    module.open = lambda *args, **kwargs: _AsyncOpen(*args, **kwargs)
    module.__sayuki_shim__ = True
    sys.modules["aiofiles"] = module
    return "stdlib-shim"


def _worker():  # noqa: C901 - mirrors one linear assembly path from bot_app.py
    import asyncio
    import tempfile
    from datetime import datetime
    from types import SimpleNamespace

    payload = json.loads(sys.stdin.read())
    legacy_root = Path(payload["legacy_root"]).resolve()

    sys.dont_write_bytecode = True
    here = Path(__file__).resolve().parent
    forbidden = [Path(p).resolve() for p in payload["forbidden_roots"]]
    kept = []
    for entry in sys.path:
        try:
            resolved = Path(entry or ".").resolve()
        except OSError:  # pragma: no cover - defensive
            continue
        if resolved == here or any(
            resolved == root or root in resolved.parents for root in forbidden
        ):
            continue
        kept.append(entry)
    sys.path[:] = [str(legacy_root), *kept]

    aiofiles_mode = _install_aiofiles_shim()

    import math_tools

    import sayuki_bot
    from sayuki_bot import llm as llm_module
    from sayuki_bot import memory as memory_module
    from sayuki_bot import message_context, prompts
    from sayuki_bot import presence as presence_module
    from sayuki_bot import short_term_memory as short_term_module
    from sayuki_bot.discord_refs import resolve_discord_references
    from sayuki_bot.llm import OpenRouterLLM
    from sayuki_bot.memory import MemoryManager
    from sayuki_bot.presence import PresenceManager
    from sayuki_bot.short_term_memory import ShortTermMemoryManager
    from sayuki_bot.state import BotStats

    from sayuki_bot import search as search_module

    tool_guard = _install_tool_sentinels(llm_module, math_tools, search_module=search_module)
    if payload.get("monotonic_step_seconds"):
        _use_test_monotonic(float(payload["monotonic_step_seconds"]))
    # Every 4.0 module that reads a wall clock for content that can reach the
    # prompt. Only the clock is replaced; the surrounding logic stays original.
    clock_modules = (message_context, memory_module, short_term_module, presence_module)

    loaded = {}
    for name, module in sorted(sys.modules.items()):
        if not (name == "sayuki_bot" or name.startswith("sayuki_bot.")):
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        origin = Path(origin).resolve()
        if legacy_root not in origin.parents:
            raise RuntimeError(f"Module {name} came from outside 4.0: {origin}")
        loaded[name] = {"file": str(origin), "sha256": sha256_file(origin)}

    environment = {
        "python": sys.executable,
        "cwd": os.getcwd(),
        "legacy_root": str(legacy_root),
        "sys_path": list(sys.path),
        "package_file": str(Path(sayuki_bot.__file__).resolve()),
        "dont_write_bytecode": sys.dont_write_bytecode,
        "aiofiles": aiofiles_mode,
        "tool_sentinels": tool_guard["installed"],
        "external_boundaries": tool_guard["boundaries"],
        "frozen_clock_modules": [module.__name__ for module in clock_modules],
        "api_key_env_present": any(
            os.environ.get(name) for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY")
        ),
        "modules": loaded,
    }

    results = []
    for case in payload["cases"]:
        originals = {module: module.datetime for module in clock_modules}
        del tool_guard["calls"][:]
        with tempfile.TemporaryDirectory(prefix="legacy40-") as directory:
            try:
                scenario_now = datetime.fromisoformat(case["now"])
                if scenario_now.tzinfo is None:
                    raise ValueError("case.now requires a timezone offset")

                # Only the wall clock is replaced; 4.0's own "現在時間/發言數"
                # narration and every other native block stay as they are.
                # memory.add_permanent_memory stamps fact ids from datetime.now(),
                # so freezing it there is what makes a scenario replayable.
                class _FrozenDatetime(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return (
                            scenario_now.astimezone(tz) if tz else scenario_now.replace(tzinfo=None)
                        )

                for module in clock_modules:
                    module.datetime = _FrozenDatetime
                results.append(
                    asyncio.run(
                        _assemble_case(
                            case,
                            payload,
                            Path(directory),
                            message_context=message_context,
                            prompts=prompts,
                            resolve_discord_references=resolve_discord_references,
                            OpenRouterLLM=OpenRouterLLM,
                            MemoryManager=MemoryManager,
                            PresenceManager=PresenceManager,
                            ShortTermMemoryManager=ShortTermMemoryManager,
                            BotStats=BotStats,
                            SimpleNamespace=SimpleNamespace,
                            tool_guard=tool_guard,
                        )
                    )
                )
            except Exception as exc:  # one unusable case must not hide the rest
                results.append(
                    {
                        "case_id": case.get("id"),
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            except ToolExecutionAttempted as exc:
                results.append(
                    {
                        "case_id": case.get("id"),
                        "status": "error",
                        "error": f"ToolExecutionAttempted: {exc}",
                        "tool_guard": {"calls": list(tool_guard["calls"])},
                    }
                )
            finally:
                for module, original in originals.items():
                    module.datetime = original
    json.dump({"environment": environment, "results": results}, sys.stdout, ensure_ascii=False)


async def _assemble_case(  # noqa: C901 - one linear path, kept in bot_app.py order
    case,
    payload,
    work_dir,
    *,
    message_context,
    prompts,
    resolve_discord_references,
    OpenRouterLLM,
    MemoryManager,
    PresenceManager,
    ShortTermMemoryManager,
    BotStats,
    SimpleNamespace,
    tool_guard,
    generate=True,
    scene_out=None,
):
    from datetime import datetime as real_datetime

    bot_user_id = int(case.get("bot", {}).get("id", 99))
    bot_name = case.get("bot", {}).get("name", "紗月")
    guild_id = int(case.get("guild_id", 30))
    channel_id = int(case.get("channel_id", 20))

    members = {bot_user_id: SimpleNamespace(id=bot_user_id, display_name=bot_name, bot=True)}
    for person in case.get("participants", []):
        members[int(person["id"])] = SimpleNamespace(
            id=int(person["id"]), display_name=person["name"], bot=bool(person.get("bot", False))
        )

    guild = SimpleNamespace(id=guild_id, name=case.get("guild_name", "模擬伺服器"))
    channel = SimpleNamespace(
        id=channel_id, name=case.get("channel_name", f"模擬頻道-{channel_id}")
    )
    guild.get_member = members.get
    guild.get_channel_or_thread = lambda identifier: channel if identifier == channel_id else None

    built = {}

    def make_message(raw):
        identifier = int(raw["id"])
        author_id = int(raw["author_id"])
        if author_id not in members:
            members[author_id] = SimpleNamespace(
                id=author_id, display_name=raw.get("author_name", "未知"), bot=False
            )
        author = members[author_id]
        if raw.get("author_name"):
            author.display_name = raw["author_name"]
        created = real_datetime.fromisoformat(raw["time"])
        if created.tzinfo is None:
            raise ValueError(f"message {identifier} time requires a timezone offset")
        attachments = [
            SimpleNamespace(
                filename=item.get("filename", ""),
                url=item.get("url", ""),
                content_type=item.get("content_type"),
            )
            for item in raw.get("attachments", [])
        ]
        text = raw["text"]
        reference = None
        if raw.get("reply_to"):
            target = built.get(int(raw["reply_to"]))
            reference = SimpleNamespace(message_id=int(raw["reply_to"]), resolved=target)
        message = SimpleNamespace(
            id=identifier,
            guild=guild,
            channel=channel,
            author=author,
            content=text,
            clean_content=text,
            created_at=created,
            attachments=attachments,
            mentions=[members[int(uid)] for uid in raw.get("mentions", [])],
            reference=reference,
            components=[],
            embeds=[],
            poll=None,
            thread=None,
        )
        built[identifier] = message
        return message

    history_messages = [make_message(raw) for raw in case.get("history", [])]
    current_message = make_message(case["current"])
    # bot_app.py: the triggering message is already inside the channel cache, so it
    # appears both in the history block and as the user message.
    cached_msgs = [*history_messages, current_message]
    history_msgs = cached_msgs[-int(payload["history_limit"]) :]

    is_mentioned, is_keyword, is_reply = message_context.get_attention_flags(
        current_message, bot_user_id
    )
    declared = case.get("trigger", "mention")
    is_proactive = declared == "proactive"
    if is_proactive:
        attention_reason = "主動查看，沒有人提及你"
    elif is_mentioned:
        attention_reason = "目前對話者直接@了你"
    elif is_reply:
        attention_reason = "目前對話者回覆了你的訊息"
    elif is_keyword:
        attention_reason = "目前對話者提到你的名字或關鍵字"
    else:
        attention_reason = ""
    trigger_check = {
        "declared": declared,
        "mentioned": is_mentioned,
        "keyword": is_keyword,
        "reply": is_reply,
        "proactive": is_proactive,
        "attention_reason": attention_reason,
        "agrees": bool(
            is_proactive
            or (declared == "mention" and is_mentioned)
            or (declared == "name" and is_keyword)
            or (declared == "reply" and is_reply)
        ),
    }

    fake_bot = SimpleNamespace(
        user=members[bot_user_id],
        get_channel=lambda identifier: channel if identifier == channel_id else None,
    )
    discord_refs = await resolve_discord_references(
        fake_bot,
        current_message,
        current_message.content,
        component_context_cache={},
        component_context_cache_times={},
        component_cache_ttl_seconds=30,
        component_cache_max_items=500,
    )

    # Relevant-user selection, copied from create_bot._collect_relevant_memory_user_ids.
    relevant_ids = {str(current_message.author.id)}
    for message in [*history_msgs, *discord_refs.reply_targets.values()]:
        author_id = getattr(getattr(message, "author", None), "id", None)
        if author_id and author_id != bot_user_id:
            relevant_ids.add(str(author_id))
        for mentioned in getattr(message, "mentions", []):
            if mentioned.id != bot_user_id:
                relevant_ids.add(str(mentioned.id))
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None) if reference else None
        resolved_author_id = getattr(getattr(resolved, "author", None), "id", None)
        if resolved_author_id and resolved_author_id != bot_user_id:
            relevant_ids.add(str(resolved_author_id))

    memory_manager = MemoryManager(
        db_file=str(work_dir / "memory.json"),
        permanent_db_file=str(work_dir / "permanent_memory.json"),
        server_memory_file=str(work_dir / "server_memory.json"),
    )
    await memory_manager.init_db()
    # Every memory must name a source that exists in the case. A missing source is
    # an unusable case, never a reason to borrow somebody else's message.
    declared_sources = {
        int(raw["id"]): raw
        for raw in [*case.get("history", []), *case.get("stored_events", []), case["current"]]
    }
    unmapped = []
    for item in case.get("memories", []):
        subject = str(item["subject_id"])
        name = item.get("subject_name")
        kind = item.get("kind", "profile")
        key, value = item["key"], item["value"]
        source = declared_sources.get(int(item["source_id"]))
        if source is None:
            raise ValueError(
                f"memory {item['key']!r} names source_id {item['source_id']} "
                "which the case never declares"
            )
        source_date = source["time"][:10]
        # Keep third-party statements attributable, as 4.0's own tool path does.
        attribution = source.get("author_name") if str(source["author_id"]) != subject else None
        if kind == "event":
            await memory_manager.add_important_event(
                subject,
                f"{key}：{value}" if key else value,
                item.get("event_type", "事實/動態"),
                source_date,
                name,
                attribution,
            )
        elif kind == "profile" and key == "hobby":
            await memory_manager.add_hobby(subject, value, name)
        elif kind == "profile" and key in {"nickname", "location", "mbti"}:
            await memory_manager.set_profile_field(subject, f"basic_info.{key}", value, name)
        elif kind == "profile" and key in {"relationship", "recent_events", "name"}:
            await memory_manager.set_profile_field(subject, key, value, name)
        elif kind == "profile":
            # No equivalent structured field in 4.0; keep it visible as an event
            # rather than inventing a profile shape the original never had.
            await memory_manager.add_important_event(
                subject, f"{key}：{value}", "偏好/畫像", source_date, name, attribution
            )
            unmapped.append({"key": key, "reason": "4.0 profile has no free-form key/value field"})
        elif kind == "self":
            await memory_manager.add_permanent_memory(f"{key}：{value}" if key else value)
        elif kind == "culture":
            await memory_manager.add_server_memory(guild_id, f"{key}：{value}", "文化", source_date)
        else:
            unmapped.append({"key": key, "reason": f"unsupported kind {kind}"})
    for item in self_memory_entries(case):
        # 4.0 keeps permanent memory as a list of separate facts; each synthetic
        # entry stays its own fact so nothing silently overwrites anything else.
        await memory_manager.add_permanent_memory(item["value"])

    memory_context = memory_manager.get_relevant_memory_context(relevant_ids)
    permanent_memory = memory_manager.get_permanent_memory()
    server_memory = memory_manager.get_server_memory(guild_id)

    presence_manager = PresenceManager()
    presence_context = presence_manager.build_context(guild_id, relevant_ids)

    short_memory = ShortTermMemoryManager(
        db_file=str(work_dir / "short_term_memory.json"),
        pending_file=work_dir / "short_memory_pending.jsonl",
    )
    await short_memory.init_db()
    short_term_context = short_memory.build_context(channel_id, current_message.author.id)

    vl_description_cache = {
        int(raw["id"]): raw["image_description"]
        for raw in [*case.get("history", []), case["current"]]
        if raw.get("image_description")
    }

    stats = BotStats(
        total_messages=int(case.get("stats", {}).get("total_messages", 0)),
        today_messages=int(case.get("stats", {}).get("today_messages", 0)),
    )

    started = _monotonic()
    chat_history = await message_context.build_chat_history(
        history_msgs,
        cached_msgs,
        current_message,
        bot_user_id,
        is_proactive,
        vl_description_cache,
        {},
        {},
        30,
        500,
    )
    time_context = message_context.build_time_context(stats)
    sys_info = message_context.build_system_context(
        current_message.author.display_name,
        current_message.author.id,
        attention_reason,
        memory_context,
        permanent_memory,
        server_memory,
        presence_context,
        short_term_context,
        chat_history,
        stats,
        is_proactive,
    )
    system_prompt = prompts.load_system_prompt(Path(payload["persona_path"]))

    msg_content = current_message.content
    current_attachment = message_context.get_attachment_info(current_message)
    if current_attachment:
        msg_content = f"{msg_content} {current_attachment}"
    reference_context = discord_refs.context
    if reference_context:
        msg_content = f"{msg_content}\n\n【Discord標記解析】\n{reference_context}"
    msg_list = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": sys_info},
        {"role": "user", "content": msg_content},
    ]
    assembly_ms = (_monotonic() - started) * 1000

    if scene_out is not None:
        # Live objects the E layer needs; the P layer ignores them.
        scene_out.update(
            {
                "memory_manager": memory_manager,
                "stats": stats,
                "current_message": current_message,
                "channel": channel,
                "guild": guild,
                "members": members,
                "bot_user_id": bot_user_id,
                "history_msgs": history_msgs,
                "attention_reason": attention_reason,
                "is_proactive": is_proactive,
                "reply_targets": {
                    f"msg_{str(message.id)[-4:]}": message for message in history_msgs
                },
            }
        )
    if not generate:
        return {
            "case_id": case.get("id"),
            "status": "ok",
            "version": "4.0",
            "messages": msg_list,
            "trigger_check": trigger_check,
        }

    requests = []
    llm = OpenRouterLLM(
        api_key="offline-placeholder-not-used",
        model=payload["model"],
        small_model=payload["model"],
        vl_model=payload["model"],
        use_reasoning_effort=bool(payload["use_reasoning_effort"]),
        reasoning_effort=payload["reasoning_effort"],
    )
    # The original constructor always builds a real SDK client. Close it before
    # swapping in the fake transport so the P layer leaves no open connector.
    real_client = llm.client
    stop_after_first = payload.get("stop_after_first_request", True)
    llm.client = _FakeChatClient(
        requests, case.get("fake_reply", "（離線假模型回覆）"), stop_after_first
    )
    stopped_at_first_request = False
    generation_started = _monotonic()
    try:
        # max_search stays at the production default: the stop signal, not a
        # lowered loop count, is what keeps the tool loop from running.
        await llm.generate_async(list(msg_list), max_search=2, call_type="main")
    except FirstRequestCaptured:
        stopped_at_first_request = True
    finally:
        generation_ms = (_monotonic() - generation_started) * 1000
        await real_client.close()
    if stop_after_first and not stopped_at_first_request:
        raise RuntimeError("4.0 generation continued past the first request")
    if tool_guard["calls"]:
        raise ToolExecutionAttempted(", ".join(tool_guard["calls"]))

    return {
        "case_id": case.get("id"),
        "status": "ok",
        "version": "4.0",
        "trigger_check": trigger_check,
        "persona": {
            "source": str(payload["persona_path"]),
            "source_sha256": sha256_file(payload["persona_path"]),
            "block_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
            "chars": len(system_prompt),
        },
        "messages": msg_list,
        "regions": {
            "persona": len(system_prompt),
            "system_info_total": len(sys_info),
            "time_context": len(time_context),
            "memory_context": len(memory_context),
            "permanent_memory": len(permanent_memory),
            "server_memory": len(server_memory),
            "presence_context": len(presence_context),
            "short_term_context": len(short_term_context),
            "chat_history": len(chat_history),
            "current_message": len(msg_content),
            "discord_references": len(reference_context),
        },
        # 4.0 prints the triggering message inside its history block, so the
        # entry count includes it; prior_available counts only earlier messages.
        "history_block_entries": len(history_msgs),
        "history_includes_current": any(m.id == current_message.id for m in history_msgs),
        "prior_messages_available": len(cached_msgs) - 1,
        "history_window_limit": int(payload["history_limit"]),
        "unmapped_memories": unmapped,
        "request": requests[0] if requests else None,
        "request_count": len(requests),
        "fake_reply": case.get("fake_reply"),
        "stopped_at": "first request; no reply parsed, no tool loop entered",
        "tool_guard": {
            "installed": list(tool_guard["installed"]),
            "calls": list(tool_guard["calls"]),
        },
        "usage": {"status": "unknown", "reason": "offline fake transport returns no usage"},
        "timing_ms": {
            "assembly": round(assembly_ms, 3),
            "fake_generation": round(generation_ms, 3),
        },
        "calls": [
            {
                "function": "prompts.load_system_prompt",
                "module_file": str(Path(prompts.__file__).resolve()),
            },
            {
                "function": "message_context.build_chat_history",
                "module_file": str(Path(message_context.__file__).resolve()),
            },
            {
                "function": "message_context.build_system_context",
                "module_file": str(Path(message_context.__file__).resolve()),
            },
            {
                "function": "message_context.build_time_context",
                "module_file": str(Path(message_context.__file__).resolve()),
            },
            {
                "function": "message_context.get_attention_flags",
                "module_file": str(Path(message_context.__file__).resolve()),
            },
            {
                "function": "message_context.get_attachment_info",
                "module_file": str(Path(message_context.__file__).resolve()),
            },
            {"function": "discord_refs.resolve_discord_references"},
            {"function": "memory.MemoryManager.get_relevant_memory_context"},
            {"function": "presence.PresenceManager.build_context"},
            {"function": "short_term_memory.ShortTermMemoryManager.build_context"},
            {"function": "llm.OpenRouterLLM.generate_async"},
        ],
    }


def _monotonic():
    return _MONOTONIC_SOURCE()


def _default_monotonic():
    from time import perf_counter

    return perf_counter()


_MONOTONIC_SOURCE = _default_monotonic


def _use_test_monotonic(step_seconds):
    """Swap in a controllable monotonic source so tests can assert that elapsed
    time comes from a clock of its own, independent of the frozen scenario clock."""
    global _MONOTONIC_SOURCE
    state = {"value": 0.0}

    def ticking():
        current = state["value"]
        state["value"] += step_seconds
        return current

    _MONOTONIC_SOURCE = ticking


class _FakeChatClient:
    """Records the real request arguments; performs no network call."""

    def __init__(self, sink, reply=None, stop=True):
        self.chat = type("_Chat", (), {})()
        self.chat.completions = _FakeCompletions(sink, reply, stop)


class _FakeCompletions:
    def __init__(self, sink, reply=None, stop=True):
        self.sink, self.reply, self.stop = sink, reply, stop

    async def create(self, **kwargs):
        # The request is fully built by the version's own code by the time this
        # runs; record it and stop before any reply can drive the tool loop.
        self.sink.append(request_fingerprint(kwargs))
        if self.stop:
            raise FirstRequestCaptured("P layer stops after the first request")
        # Regression-only path: let the original loop continue so the tool
        # sentinels can prove they would catch a real tool call. Never used by
        # the normal comparison run.
        from types import SimpleNamespace

        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=self.reply), finish_reason="stop")
            ],
            usage=None,
        )


def self_memory_entries(case) -> list[dict]:
    """Normalise `self_memory` so each entry keeps a stable, distinct identity.

    A plain string gets an index-derived key. 4.6's MemoryService updates by key,
    so entries sharing one key would overwrite each other and the two versions
    would end up with different facts; the index keeps them separate and stable
    across repeats and batch order.
    """
    entries = []
    for index, item in enumerate(case.get("self_memory", []), start=1):
        if isinstance(item, dict):
            entries.append({"key": item["key"], "value": item["value"]})
        else:
            entries.append({"key": f"自身記憶{index}", "value": item})
    keys = [entry["key"] for entry in entries]
    if len(keys) != len(set(keys)):
        raise ValueError("self_memory keys must be distinct")
    return entries


def request_fingerprint(kwargs) -> dict:
    """What the version's own code actually asked the provider for."""
    messages = kwargs.get("messages", [])
    return {
        "model": kwargs.get("model"),
        "max_tokens": kwargs.get("max_tokens"),
        "temperature": kwargs.get("temperature"),
        "extra_body": kwargs.get("extra_body"),
        "message_roles": [message.get("role") for message in messages],
        "message_chars": [len(message.get("content") or "") for message in messages],
        "messages_sha256": hashlib.sha256(
            json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


# ------------------------------------------------------------ 4.0 E layer


def run_flow_cases(
    cases,
    *,
    legacy_root=None,
    persona_path,
    model,
    history_limit=25,
    use_reasoning_effort=False,
    reasoning_effort="medium",
    pacing="simulated",
    python=None,
    timeout=WORKER_TIMEOUT_SECONDS,
    on_record=None,
    on_progress=None,
    on_model_request=None,
    live=False,
    stop_event=None,
):
    """Replay whole turns through 4.0's real Scheduler in the isolated child.

    ``on_record`` receives each case the moment the child reports it, so a caller
    can persist evidence before the batch finishes. ``on_progress`` receives the
    partial snapshots the child streams *during* a case — delivered segments and
    the events so far — so a turn stopped mid-flight still has its evidence on
    this side of the pipe when the child is force-stopped.
    """
    legacy_root = Path(legacy_root or default_legacy_root()).resolve()
    if not (legacy_root / "sayuki_bot" / "scheduler.py").is_file():
        raise ValueError(f"No 4.0 source at {legacy_root}")
    payload = {
        "legacy_root": str(legacy_root),
        "forbidden_roots": [str(Path(__file__).resolve().parents[2])],
        "persona_path": str(Path(persona_path).resolve()),
        "model": model,
        "history_limit": history_limit,
        "use_reasoning_effort": use_reasoning_effort,
        "reasoning_effort": reasoning_effort,
        "stop_after_first_request": False,  # the E layer must finish the turn
        "pacing": pacing,
        # Live mode routes every completion back through the parent proxy.
        "live": bool(live or on_model_request is not None),
        "cases": list(cases),
    }
    return _stream_flow_worker(
        payload,
        legacy_root,
        python=python,
        timeout=timeout,
        on_record=on_record,
        on_progress=on_progress,
        on_model_request=on_model_request,
        stop_event=stop_event,
    )


# Process groups are a POSIX facility; this project runs on macOS/Linux and
# falls back to killing the direct child anywhere else.
_GROUP_SIGNALS = hasattr(os, "killpg") and hasattr(os, "setsid")


def _serialisable_request(kwargs) -> dict:
    """Request arguments without the message bodies.

    The parent needs the messages to perform the call, so they travel separately;
    this keeps the recorded copy free of persona text.
    """
    return {key: value for key, value in kwargs.items() if key != "messages"}


def _close_pipes(pipes, *, busy):
    """Close the worker pipes without ever waiting on a blocked I/O thread.

    ``close()`` takes the buffer lock, so a reader still parked inside ``read()``
    would hold the caller there. Once the group is really dead the reader sees EOF
    and this is immediate; if a thread is somehow still running, the close is
    handed to a daemon so the deadline stays bounded either way.
    """

    def shut(pipe):
        try:
            pipe.close()
        except (ValueError, OSError):
            pass

    for pipe in pipes:
        if pipe is None or pipe.closed:
            continue
        if busy:
            threading.Thread(target=shut, args=(pipe,), daemon=True).start()
        else:
            shut(pipe)


class FlowWorkerTimeout(RuntimeError):
    """The 4.0 flow worker exceeded the deadline for the whole exchange.

    Carries whatever the child had already delivered, so an aborted batch can be
    read back from the records that were handed over before the deadline.
    """

    def __init__(self, message, *, environment=None, results=None):
        super().__init__(message)
        self.environment = environment or {}
        self.results = list(results or [])


def _stream_flow_worker(
    payload,
    legacy_root,
    *,
    python=None,
    timeout=None,
    on_record=None,
    on_progress=None,
    on_model_request=None,
    stop_event=None,
    worker_script=None,
):
    """Run the child and hand back each case as soon as it finishes.

    Waiting for the whole subprocess to return one JSON blob means a timeout or a
    crash loses every case it had already completed, so the child streams one
    record per line and the parent forwards each one straight away.

    One monotonic deadline covers the entire exchange — writing the payload,
    waiting for each line, and the final exit wait. Reading a line does **not**
    extend it, so a child that stalls, emits half a line, or drips output forever
    is still bounded. Blocking reads and writes run on daemon threads because the
    deadline has to be enforced from the outside; a silent child would otherwise
    leave `readline()` parked with no timeout to reach.
    """
    import queue
    import tempfile
    from time import monotonic

    deadline = None if timeout is None else monotonic() + timeout

    def remaining():
        if deadline is None:
            return None
        return deadline - monotonic()

    # The live-model proxy keeps the API key in this process: the child asks for a
    # completion over the pipe instead of holding a key or reaching the network.
    keep_stdin_open = on_model_request is not None
    environment, results = {}, []
    lines: queue.Queue = queue.Queue()
    end_of_stream = object()
    stopped = object()

    def pump(stream):
        try:
            for line in stream:
                lines.put(line)
        except (ValueError, OSError):  # pipe closed while we were killing the child
            pass
        finally:
            lines.put(end_of_stream)

    stdin_lock = threading.Lock()
    # Events a stop must set. Every bounded wait in this function registers here
    # so the stop wakes it directly rather than relying on the wait's own bound.
    wakeups: set = set()

    def push(stream, data):
        try:
            with stdin_lock:
                stream.write(data)
                stream.flush()
                if not keep_stdin_open:
                    stream.close()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def answer(message, limit=None):
        """Send one reply line back to the child (live-model proxy channel).

        The write is bounded too: a child that stops reading would otherwise fill
        the pipe and park this thread with no deadline to reach. ``wakeups``
        holds the events a stop has to set, so a write that is blocked on a full
        pipe returns as soon as the caller cancels instead of sitting out its
        share of the deadline.
        """
        done = threading.Event()
        wakeups.add(done)

        def write():
            try:
                with stdin_lock:
                    process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                    process.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass
            finally:
                done.set()

        try:
            if limit is None:
                write()
                return True
            worker = threading.Thread(target=write, daemon=True)
            worker.start()
            written = done.wait(timeout=max(0.0, limit))
            if stop_event is not None and stop_event.is_set():
                # The stop released this wait; the reply did not get through.
                return False
            return written
        finally:
            wakeups.discard(done)

    with tempfile.TemporaryFile("w+", encoding="utf-8") as errors:
        environment_vars = child_environment(legacy_root)
        if worker_script is not None:
            # Custom receive-path workers need no credential-bearing variables.
            environment_vars = {
                key: value
                for key, value in environment_vars.items()
                if not any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
            }
        process = subprocess.Popen(
            [
                python or sys.executable,
                "-s",
                str(worker_script or Path(__file__).resolve()),
                "--worker-flow",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            env=environment_vars,
            cwd=str(legacy_root.parent),
            # Its own process group, so a timeout can reap anything the worker
            # started as well: killing only the direct child leaves a grandchild
            # holding the pipe open, and the reader would never see EOF.
            start_new_session=True,
        )
        # start_new_session makes the child a session and group leader, so the
        # group id is its pid by definition. Remembering it here is what lets the
        # cleanup work after the direct child has already exited: getpgid() would
        # then have nothing to look up, and only this group is ever signalled.
        group = process.pid if _GROUP_SIGNALS else None
        owned = {"group": True}
        if stop_event is not None and hasattr(stop_event, "own_group"):
            stop_event.own_group(group)
        reader = threading.Thread(target=pump, args=(process.stdout,), daemon=True)
        writer = threading.Thread(
            target=push,
            args=(process.stdin, json.dumps(payload, ensure_ascii=False) + "\n"),
            daemon=True,
        )
        reader.start()
        writer.start()

        def terminate():
            """Clean up the whole group this call created.

            The direct child exiting proves nothing: a shell can start a
            background descendant, exit 0, and leave that descendant holding the
            inherited stdout. Returning early on ``poll()`` therefore skipped the
            group kill and left the close waiting for a reader that could not see
            EOF. Only the group created here is signalled, and a group that has
            already gone is a normal outcome, not an error.
            """
            if group is not None and owned["group"]:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # every member already exited
                except (PermissionError, OSError):  # pragma: no cover - defensive
                    pass
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:  # pragma: no cover - already reaped
                    pass
                process.wait()

        def wake():
            """Run when the caller stops this run, from whatever thread stopped it.

            Polling a flag cannot reach a wait that is already blocked, so the
            stop reaches each one directly: the queue gets a sentinel, every
            bounded write is released, the model proxy is told to drop its
            in-flight call, and the group is reaped so the pipes close and the
            reader sees EOF. None of this moves the deadline; it only unblocks
            waits that the deadline already bounds.
            """
            abandon = getattr(on_model_request, "abandon", None)
            if abandon:
                _safely(abandon)
            _safely(lambda: lines.put(stopped))
            for event in list(wakeups):
                _safely(event.set)
            with terminate_lock:
                _safely(terminate)

        terminate_lock = threading.Lock()
        if stop_event is not None and hasattr(stop_event, "add_waker"):
            stop_event.add_waker(wake)

        def confirm_gone(limit=5.0):
            """Poll the group this run created until nothing in it is left.

            ``terminate`` signalling the group is not the same as the group being
            gone: a descendant is only really reaped once the kernel stops
            answering for it. Returns True when nothing answers any more.
            """
            if group is None:
                return process.poll() is not None
            from time import monotonic

            deadline = monotonic() + max(0.0, limit)
            while True:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    return True
                except (PermissionError, OSError):  # pragma: no cover - defensive
                    return True
                if monotonic() >= deadline:
                    return False
                _safely(lambda: os.killpg(group, signal.SIGKILL))
                time.sleep(0.02)

        def released():
            """Tell the caller this group no longer needs confirming."""
            if stop_event is not None and hasattr(stop_event, "release_group"):
                stop_event.release_group(group)
            owned["group"] = False

        def halt(stage):
            """The caller cancelled: clean up this run's own group, then stop.

            The cleanup is confirmed, not assumed. A stop that could not prove
            the group had gone says so in its own message instead of reporting a
            timeout as a successful cleanup.
            """
            terminate()
            reader.join(timeout=0.2)
            writer.join(timeout=0.2)
            cleaned = confirm_gone()
            if cleaned:
                released()
            raise FlowWorkerStopped(
                f"the caller stopped the 4.0 worker while {stage}; "
                f"{len(results)} case(s) had already been delivered; "
                + (
                    "the worker's process group is confirmed stopped"
                    if cleaned
                    else f"its process group {group} could NOT be confirmed stopped"
                )
            )

        def expire(stage):
            terminate()
            # The reader and writer are daemons; they end when the pipes close.
            # Never block the deadline on them.
            reader.join(timeout=0.2)
            writer.join(timeout=0.2)
            raise FlowWorkerTimeout(
                f"4.0 flow worker exceeded {timeout}s while {stage}; "
                f"{len(results)} case(s) had already been delivered",
                environment=environment,
                results=results,
            )

        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    halt("streaming results")
                left = remaining()
                if left is not None and left <= 0:
                    expire("streaming results")
                try:
                    item = lines.get(timeout=left)
                except queue.Empty:
                    expire("streaming results")
                if item is stopped:
                    halt("streaming results")
                if item is end_of_stream:
                    if stop_event is not None and stop_event.is_set():
                        # The pipes closed because the stop reaped the group, not
                        # because the child finished its work.
                        halt("streaming results")
                    break
                item = item.strip()
                if not item:
                    continue
                record = json.loads(item)
                if record.get("kind") == "environment":
                    environment = record["environment"]
                    continue
                if record.get("kind") == "progress":
                    # Partial evidence the child streams as it goes, so a turn
                    # that is cancelled mid-flight still has its delivered
                    # segments and events on this side of the pipe.
                    if on_progress:
                        on_progress(record)
                    continue
                if record.get("kind") == "model_request":
                    if stop_event is not None and stop_event.is_set():
                        halt("serving a model request")
                    # Answer in the parent, where the key lives, then let the
                    # child's own Scheduler carry on with the real reply. The
                    # callback and the write-back both run inside the remaining
                    # deadline: neither may extend it.
                    left = remaining()
                    if left is not None and left <= 0:
                        expire("serving a model request")
                    # The parent enforces the limit rather than trusting the
                    # callback to respect it; an abandoned callback is told to
                    # cancel its own in-flight work so nothing keeps issuing
                    # completions after the deadline.
                    holder = {}

                    def invoke(entry=record, budget=left):
                        try:
                            holder["reply"] = on_model_request(entry, budget)
                        except BaseException as exc:  # carried back to this loop
                            holder["error"] = exc

                    call = threading.Thread(target=invoke, daemon=True)
                    call.start()
                    # wake() abandons the in-flight completion, so a stop ends
                    # this join at once instead of waiting out the deadline.
                    call.join(timeout=left)
                    if stop_event is not None and stop_event.is_set():
                        halt("serving a model request")
                    if call.is_alive():
                        abandon = getattr(on_model_request, "abandon", None)
                        if abandon:
                            abandon()
                        expire("serving a model request")
                    if "error" in holder:
                        # A cancellation inside the proxy must reach the caller,
                        # not die in the worker thread; clean up first.
                        terminate()
                        raise holder["error"]
                    reply = holder.get(
                        "reply", {"kind": "model_stopped", "reason": "no reply from the proxy"}
                    )
                    left = remaining()
                    if left is not None and left <= 0:
                        expire("serving a model request")
                    if not answer(reply, left):
                        if stop_event is not None and stop_event.is_set():
                            halt("writing a model reply back to the worker")
                        expire("writing a model reply back to the worker")
                    continue
                results.append(record["result"])
                if on_record:
                    on_record(record["result"])
            left = remaining()
            if left is not None and left <= 0:
                expire("waiting for the worker to exit")
            if stop_event is not None and stop_event.is_set():
                halt("waiting for the worker to exit")
            try:
                process.wait(timeout=left)
                # Reaped cleanly: stop treating the group id as ours so a later
                # failure cannot signal a recycled group.
                released()
            except subprocess.TimeoutExpired:
                expire("waiting for the worker to exit")
            if stop_event is not None and stop_event.is_set():
                halt("waiting for the worker to exit")
            writer.join(timeout=max(0.0, remaining() or 1))
        except BaseException:
            terminate()
            raise
        finally:
            if stop_event is not None and hasattr(stop_event, "discard_waker"):
                stop_event.discard_waker(wake)
            reader.join(timeout=0.2)
            writer.join(timeout=0.2)
            _close_pipes(
                (process.stdout, process.stdin), busy=reader.is_alive() or writer.is_alive()
            )
        if process.returncode != 0:
            errors.seek(0)
            raise RuntimeError(
                f"4.0 flow worker failed (exit {process.returncode}): {errors.read()[-4000:]}"
            )
    return {"environment": environment, "results": results}


def _install_opencc_shim():
    """Allow legacy 4.0 imports without adding OpenCC to 4.6 dependencies.

    4.6 deliberately has no automatic script conversion. When OpenCC is absent,
    4.0 gets an identity shim; reports must retain that environment limitation.
    """
    import importlib.util

    if importlib.util.find_spec("opencc") is not None:
        return "real"

    import types

    class _Identity:
        def __init__(self, *args, **kwargs):
            pass

        def convert(self, text):
            return text

    module = types.ModuleType("opencc")
    module.OpenCC = _Identity
    module.__sayuki_shim__ = True
    sys.modules["opencc"] = module
    return "identity-shim"


def _flow_worker():  # noqa: C901 - one linear harness around the real Scheduler
    import asyncio
    import tempfile
    from contextlib import asynccontextmanager
    from datetime import datetime
    from time import perf_counter
    from types import SimpleNamespace

    # One JSON line, so stdin stays usable as the live-model reply channel.
    payload = json.loads(sys.stdin.readline())
    legacy_root = Path(payload["legacy_root"]).resolve()
    sys.dont_write_bytecode = True
    here = Path(__file__).resolve().parent
    forbidden = [Path(p).resolve() for p in payload["forbidden_roots"]]
    kept = []
    for entry in sys.path:
        try:
            resolved = Path(entry or ".").resolve()
        except OSError:  # pragma: no cover - defensive
            continue
        if resolved == here or any(
            resolved == root or root in resolved.parents for root in forbidden
        ):
            continue
        kept.append(entry)
    sys.path[:] = [str(legacy_root), *kept]

    aiofiles_mode = _install_aiofiles_shim()
    opencc_mode = _install_opencc_shim()

    import math_tools

    import sayuki_bot
    from sayuki_bot import llm as llm_module
    from sayuki_bot import memory as memory_module
    from sayuki_bot import message_context, prompts
    from sayuki_bot import presence as presence_module
    from sayuki_bot import scheduler as scheduler_module
    from sayuki_bot import short_term_memory as short_term_module
    from sayuki_bot.discord_refs import resolve_discord_references
    from sayuki_bot.llm import OpenRouterLLM
    from sayuki_bot.memory import MemoryManager
    from sayuki_bot.models import Request
    from sayuki_bot.presence import PresenceManager
    from sayuki_bot.scheduler import Scheduler
    from sayuki_bot.short_term_memory import ShortTermMemoryManager
    from sayuki_bot.state import BotStats

    from sayuki_bot import search as search_module

    tool_guard = _install_tool_sentinels(
        llm_module, math_tools, scheduler_module=scheduler_module, search_module=search_module
    )
    clock_modules = (message_context, memory_module, short_term_module, presence_module)

    loaded = {}
    for name, module in sorted(sys.modules.items()):
        if not (name == "sayuki_bot" or name.startswith("sayuki_bot.")):
            continue
        origin = getattr(module, "__file__", None)
        if not origin:
            continue
        origin = Path(origin).resolve()
        if legacy_root not in origin.parents:
            raise RuntimeError(f"Module {name} came from outside 4.0: {origin}")
        loaded[name] = {"file": str(origin), "sha256": sha256_file(origin)}

    environment = {
        "python": sys.executable,
        "cwd": os.getcwd(),
        "legacy_root": str(legacy_root),
        "sys_path": list(sys.path),
        "package_file": str(Path(sayuki_bot.__file__).resolve()),
        "dont_write_bytecode": sys.dont_write_bytecode,
        "aiofiles": aiofiles_mode,
        "opencc": opencc_mode,
        "api_key_env_present": any(
            os.environ.get(name) for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY")
        ),
        "tool_sentinels": tool_guard["installed"],
        "external_boundaries": tool_guard["boundaries"],
        "frozen_clock_modules": [module.__name__ for module in clock_modules],
        "layer": "E",
        "modules": loaded,
    }

    def emit(record):
        # One JSON line per record, flushed immediately, so the parent can save
        # each finished case while the subprocess is still working.
        json.dump(record, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        sys.stdout.flush()

    emit({"kind": "environment", "environment": environment})

    results = []
    for case in payload["cases"]:
        originals = {module: module.datetime for module in clock_modules}
        del tool_guard["calls"][:]
        capture = {}
        with tempfile.TemporaryDirectory(prefix="legacy40flow-") as directory:
            try:
                scenario_now = datetime.fromisoformat(case["now"])
                if scenario_now.tzinfo is None:
                    raise ValueError("case.now requires a timezone offset")

                class _FrozenDatetime(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return (
                            scenario_now.astimezone(tz) if tz else scenario_now.replace(tzinfo=None)
                        )

                for module in clock_modules:
                    module.datetime = _FrozenDatetime
                results.append(
                    asyncio.run(
                        _replay_case(
                            case,
                            payload,
                            Path(directory),
                            capture=capture,
                            emit_line=emit,
                            message_context=message_context,
                            prompts=prompts,
                            resolve_discord_references=resolve_discord_references,
                            OpenRouterLLM=OpenRouterLLM,
                            MemoryManager=MemoryManager,
                            PresenceManager=PresenceManager,
                            ShortTermMemoryManager=ShortTermMemoryManager,
                            BotStats=BotStats,
                            SimpleNamespace=SimpleNamespace,
                            tool_guard=tool_guard,
                            Scheduler=Scheduler,
                            Request=Request,
                            scheduler_module=scheduler_module,
                            asynccontextmanager=asynccontextmanager,
                            perf_counter=perf_counter,
                            asyncio=asyncio,
                        )
                    )
                )
            except (ToolExecutionAttempted, ScriptExhausted, LiveStopped) as exc:
                # Known harness-control failures keep their partial evidence so an
                # aborted case is still reviewable; cancellation is not caught.
                # A refused completion is a batch stop, not a harness error.
                results.append(
                    {
                        "case_id": case.get("id"),
                        "harness_status": "stopped" if isinstance(exc, LiveStopped) else "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "tool_guard": {"calls": list(tool_guard["calls"])},
                        "timeline": capture.get("timeline", []),
                        "requests": capture.get("requests", []),
                        "sent_segments": capture.get("sent_segments", []),
                        "assembly_ms": capture.get("assembly_ms"),
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "case_id": case.get("id"),
                        "harness_status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "timeline": capture.get("timeline", []),
                        "requests": capture.get("requests", []),
                        "sent_segments": capture.get("sent_segments", []),
                    }
                )
            finally:
                for module, original in originals.items():
                    module.datetime = original
        emit({"kind": "case", "result": results[-1]})


# The child streams a snapshot after each of these, so a cancelled turn keeps
# what it had already delivered, asked for and learned.
PROGRESS_KINDS = frozenset({"visible", "tool_result", "model_response", "model_stopped", "action"})


class _PacingClock:
    """Records the wait 4.0 asked for instead of sleeping through it.

    4.0's pacing runs entirely through ``_sleep_or_interrupted``, which awaits
    ``asyncio.wait_for(interrupt_event.wait(), timeout=seconds)``. Timing out is
    the normal, un-interrupted path, so raising TimeoutError immediately keeps the
    original semantics while the requested wait is still counted in full. Each
    wait is kept as a locatable event so "before the first visible chunk" can be
    separated from the whole turn.
    """

    def __init__(self, real, mode, mark=None):
        self._real, self.mode, self.requested_ms = real, mode, 0.0
        self._mark = mark

    def __getattr__(self, name):
        return getattr(self._real, name)

    def _record(self, timeout, actual):
        self.requested_ms += max(0.0, timeout) * 1000
        if self._mark:
            self._mark(
                "pacing",
                requested_ms=round(max(0.0, timeout) * 1000, 3),
                actual_ms=round(max(0.0, actual) * 1000, 3),
                mode=self.mode,
            )

    async def wait_for(self, awaitable, timeout=None, **kwargs):
        if timeout is None:
            return await self._real.wait_for(awaitable, timeout=timeout, **kwargs)
        if self.mode == "real":
            from time import perf_counter as _now

            started = _now()
            try:
                return await self._real.wait_for(awaitable, timeout=timeout, **kwargs)
            finally:
                self._record(timeout, _now() - started)
        self._record(timeout, 0.0)
        close = getattr(awaitable, "close", None)
        if close:
            close()
        raise self._real.TimeoutError()


async def _replay_case(  # noqa: C901 - one linear harness around the real Scheduler
    case,
    payload,
    work_dir,
    *,
    Scheduler,
    Request,
    scheduler_module,
    asynccontextmanager,
    perf_counter,
    asyncio,
    capture=None,
    emit_line=None,
    **assemble_kwargs,
):
    """Run one whole turn through 4.0's real Scheduler, offline."""
    scene = {}
    assembly_started = perf_counter()
    assembled = await _assemble_case(
        case, payload, work_dir, generate=False, scene_out=scene, **assemble_kwargs
    )
    assembly_ms = round((perf_counter() - assembly_started) * 1000, 3)
    SimpleNamespace = assemble_kwargs["SimpleNamespace"]
    OpenRouterLLM = assemble_kwargs["OpenRouterLLM"]
    tool_guard = assemble_kwargs["tool_guard"]

    # The published offsets start when the turn is enqueued, the same point 4.6
    # uses, so scene-building cost cannot leak into first_visible/total.
    clock = {"origin": None}
    events = []
    if capture is not None:
        # Live references, so a case that aborts still leaves its evidence behind.
        capture["timeline"] = events

    def mark(kind, at=None, **detail):
        moment = perf_counter() if at is None else at
        base = clock["origin"] if clock["origin"] is not None else moment
        events.append({"t_ms": round((moment - base) * 1000, 3), "kind": kind, **detail})
        if kind in PROGRESS_KINDS and emit_line is not None:
            # Stream what has happened so far. The parent cannot ask a killed
            # child for its evidence afterwards, so the child hands it over as it
            # goes. Only these few boundaries emit, and each already follows real
            # I/O, so the published offsets are not paying for this line.
            emit_line(
                {
                    "kind": "progress",
                    "case_id": case.get("id"),
                    "version": "4.0",
                    "harness_status": "running",
                    "sent_segments": list((capture or {}).get("sent_segments", [])),
                    "timeline": list(events),
                    "requests": list((capture or {}).get("requests", [])),
                    "assembly_ms": (capture or {}).get("assembly_ms"),
                }
            )

    scripted = list(case.get("scripted", {}).get("v40") or [])
    if not scripted and not payload.get("live"):
        raise ValueError(f"case {case['id']} has no scripted v40 replies")
    requests = []
    if capture is not None:
        capture["requests"] = requests
        capture["assembly_ms"] = assembly_ms

    live = bool(payload.get("live"))

    class _Completions:
        async def create(self, **kwargs):
            round_number = len(requests) + 1
            fingerprint = request_fingerprint(kwargs)
            mark("model_request", round=round_number, messages=len(kwargs["messages"]))
            if live:
                # In live mode the parent may refuse this round, so the request is
                # only counted once it has actually been sent; the parent's ledger
                # is what records a refusal.
                # The key never enters this process; the parent performs the call
                # and sends one reply line back on stdin.
                started = perf_counter()
                emit_line(
                    {
                        "kind": "model_request",
                        "case_id": case.get("id"),
                        "version": "4.0",
                        "round": round_number,
                        "fingerprint": fingerprint,
                        "request": _serialisable_request(kwargs),
                        # Needed to perform the call; the parent keeps these in
                        # memory only and records the fingerprint, never the text.
                        "messages": kwargs.get("messages", []),
                    }
                )
                reply = json.loads(sys.stdin.readline() or "{}")
                elapsed = round((perf_counter() - started) * 1000, 3)
                if reply.get("kind") != "model_response":
                    mark(
                        "model_stopped",
                        round=round_number,
                        reason=reply.get("reason", "no reply"),
                        duration_ms=elapsed,
                    )
                    raise LiveStopped(reply.get("reason", "the parent refused this completion"))
                # Prefer the parent's post-injection fingerprint; it describes
                # the request that actually went out, routing included.
                requests.append(reply.get("fingerprint") or fingerprint)
                text = reply.get("text") or ""
                mark(
                    "model_response",
                    round=round_number,
                    chars=len(text),
                    duration_ms=elapsed,
                    finish_reason=reply.get("finish_reason"),
                    usage_status=reply.get("usage_status", "unknown"),
                    # The model's own text, separate from what was delivered.
                    raw_text=text,
                )
                return SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=text),
                            finish_reason=reply.get("finish_reason") or "stop",
                        )
                    ],
                    usage=None,
                )
            requests.append(fingerprint)
            if not scripted:
                raise ScriptExhausted(
                    f"case {case['id']} ran out of scripted replies at round {round_number}"
                )
            text = scripted.pop(0)
            mark("model_response", round=round_number, chars=len(text), duration_ms=0.0)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")
                ],
                usage=None,
            )

    llm = OpenRouterLLM(
        api_key="offline-placeholder-not-used",
        model=payload["model"],
        small_model=payload["model"],
        vl_model=payload["model"],
        use_reasoning_effort=bool(payload["use_reasoning_effort"]),
        reasoning_effort=payload["reasoning_effort"],
    )
    real_client = llm.client
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    await real_client.close()

    # Record what actually reaches the channel, through the version's own send path.
    channel = scene["channel"]
    sent_segments = []
    if capture is not None:
        capture["sent_segments"] = sent_segments

    @asynccontextmanager
    async def typing():
        yield

    async def send(**kwargs):
        content = kwargs.get("content", "") or ""
        if content:
            sent_segments.append(content)
            mark("visible", chars=len(content))
        return SimpleNamespace(id=900000 + len(sent_segments))

    channel.typing = typing
    channel.send = send
    channel.name = case.get("channel_name", f"模擬頻道-{channel.id}")
    for message in [*scene["history_msgs"], scene["current_message"]]:

        async def reply(_message=message, **kwargs):
            content = kwargs.get("content", "") or ""
            if content:
                sent_segments.append(content)
                mark("visible", chars=len(content), reply_to=_message.id)
            return SimpleNamespace(id=900500 + len(sent_segments))

        message.reply = reply

    failures = case.get("tool_failures", {})
    actions = []

    async def change_presence(**kwargs):
        if "STATUS" in failures:
            raise TimeoutError(failures["STATUS"])
        actions.append({"tool": "STATUS", "detail": str(kwargs)})

    bot = SimpleNamespace(
        user=SimpleNamespace(id=scene["bot_user_id"]),
        change_presence=change_presence,
    )
    stats = scene["stats"]
    scheduler = Scheduler(llm, scene["memory_manager"], stats, 5, bot=bot)

    # Time each query-tool report at its real boundaries. The start event has to
    # be written before the call runs, so whether a tool will fire is decided with
    # 4.0's own tag regexes rather than by inspecting the result afterwards.
    for attribute, tool_name, pattern_name in (
        ("_build_image_tool_report", "VIEW_IMAGE", "VIEW_IMAGE_RE"),
        ("_build_roles_tool_report", "CHECK_ROLES", "CHECK_ROLES_RE"),
        ("_build_user_stats_tool_report", "USER_STATS", "USER_STATS_RE"),
        ("_build_memory_lookup_tool_report", "LOOKUP_MEMORY", "LOOKUP_MEMORY_RE"),
        ("_build_presence_tool_report", "CHECK_PRESENCE", "CHECK_PRESENCE_RE"),
        ("_build_read_web_tool_report", "READ_WEB", "READ_WEB_RE"),
    ):
        original = getattr(scheduler, attribute)
        pattern = getattr(scheduler_module, pattern_name)

        async def timed(
            raw_response, *args, _original=original, _name=tool_name, _pattern=pattern, **kwargs
        ):
            will_run = bool(_pattern.search(raw_response))
            if will_run:
                mark("tool_call", tool=_name)
            started = perf_counter()
            probe = await _original(raw_response, *args, **kwargs)
            if will_run:
                mark(
                    "tool_result",
                    tool=_name,
                    status="ok" if probe else "empty",
                    duration_ms=round((perf_counter() - started) * 1000, 3),
                    chars=len(probe or ""),
                    # Bounded preview so a hit and a miss are distinguishable on
                    # both sides without carrying the whole report.
                    preview=(probe or "")[:200],
                )
            return probe

        setattr(scheduler, attribute, timed)

    original_status = scheduler._apply_status_tags

    async def status_tags(raw_response, action_events=None):
        collected = [] if action_events is None else action_events
        before = len(collected)
        await original_status(raw_response, collected)
        for entry in collected[before:]:
            # 4.0 records its own verdict; it has no separate "unknown" state.
            mark("action", tool=entry["tool"], status="ok" if entry["success"] else "failed")
            actions.append(entry)

    scheduler._apply_status_tags = status_tags

    original_handle = scheduler.handle_request
    finished = asyncio.Event()
    # process_queue runs the turn in its own task and swallows Exception, so a
    # harness failure has to be carried out explicitly or it would be lost and
    # the case would be recorded as a clean run.
    failure = {}

    async def handle_request(req):
        mark("dequeue")
        try:
            return await original_handle(req)
        except BaseException as exc:
            failure["error"] = exc
            raise
        finally:
            finished.set()

    scheduler.handle_request = handle_request

    pacing = _PacingClock(scheduler_module.asyncio, payload.get("pacing", "simulated"), mark)
    scheduler_module.asyncio = pacing
    try:
        request = Request(
            messages=list(assembled["messages"]),
            interaction_obj=scene["current_message"],
            is_proactive=scene["is_proactive"],
            reply_targets=dict(scene["reply_targets"]),
            target_user_id=scene["current_message"].author.id,
            target_user_name=scene["current_message"].author.display_name,
            target_channel_id=channel.id,
            target_channel_name=channel.name,
            target_guild_id=scene["guild"].id,
            attention_reason=scene["attention_reason"],
            allow_reminders=False,
        )
        clock["origin"] = perf_counter()
        mark("enqueue")
        accepted = await scheduler.add_request(request)
        if not accepted:
            raise RuntimeError("4.0 queue full")
        await asyncio.wait_for(finished.wait(), timeout=60)
        if failure:
            raise failure["error"]
        # 4.0 has no turn-outcome object; delivering nothing is the observable
        # signal, and it is reported separately from the harness status.
        engine_outcome = {
            "status": "completed" if sent_segments else "no-visible-output",
            "sent": len(sent_segments),
        }
        mark("done", outcome=engine_outcome["status"])
    finally:
        scheduler_module.asyncio = pacing._real
        if scheduler.status_clear_task:
            scheduler.status_clear_task.cancel()
        for task in list(scheduler.reminder_tasks.values()):
            task.cancel()

    if tool_guard["calls"]:
        raise ToolExecutionAttempted(", ".join(tool_guard["calls"]))

    return {
        "case_id": case.get("id"),
        # Harness state and turn outcome are different questions.
        "harness_status": "ok",
        "engine_outcome": engine_outcome,
        "version": "4.0",
        "pacing_mode": payload.get("pacing", "simulated"),
        "assembly_ms": assembly_ms,
        "timeline": events,
        "pacing_requested_ms": round(pacing.requested_ms, 3),
        "sent_segments": sent_segments,
        "actions": actions,
        "requests": requests,
        "messages": assembled["messages"],
        # After the turn this also holds the tool reports 4.0 fed back to itself.
        "final_messages": list(request.messages),
        "trigger_check": assembled["trigger_check"],
        "usage": {
            "status": "unknown",
            "reason": "offline scripted transport returns no usage",
        },
        "tool_guard": {"calls": list(tool_guard["calls"])},
    }


if __name__ == "__main__":
    if "--worker-flow" in sys.argv:
        _flow_worker()
    elif "--worker" in sys.argv:
        _worker()
    else:  # pragma: no cover - the worker is spawned by comparison.py
        print("This module is spawned with --worker by evaluation/comparison.py", file=sys.stderr)
        sys.exit(2)
