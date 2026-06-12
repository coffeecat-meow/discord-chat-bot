from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import deque
from datetime import datetime, time, timedelta, timezone
from time import perf_counter

import discord
import opencc

import math_tools

from .conversation_log import ConversationLogger
from .llm import OpenRouterLLM
from .memory import MemoryManager
from .models import Request
from .search import read_web_page
from .state import BotStats
from .tool_tags import MEMORY_TOOL_NAMES, find_balanced_tool_tags, strip_balanced_tool_tags
from .ui import InteractiveAskView


logger = logging.getLogger(__name__)

ACTION_TOOL_NAMES = ("DM_USER", "THREAD", "NICKNAME", "SERVER_EVENT")
SPLIT_TOKEN_RE = re.compile(r"(\[\[SPLIT(?:-WAIT)?\]\])")
TOOL_TAG_RE = re.compile(r"\[\[(?!SPLIT(?:-WAIT)?\]\]|REPLY_TO:).*?\]\]", flags=re.DOTALL)
REPLY_TO_RE = re.compile(r"\[\[REPLY_TO:\s*#?(msg_\d{1,})\s*\]\]")
RESPONSE_CONTROL_RE = re.compile(r"(\[\[SPLIT(?:-WAIT)?\]\]|\[\[REPLY_TO:\s*#?msg_\d{1,}\s*\]\])")
PING_TAG_RE = re.compile(r"\[\[PING:\s*(\d+)\s*\]\]")
CHECK_ROLES_RE = re.compile(r"\[\[CHECK_ROLES:\s*(\d+)\s*\]\]")
USER_STATS_RE = re.compile(r"\[\[USER_STATS:\s*(\d+)\s*\]\]")
LOOKUP_MEMORY_RE = re.compile(r"\[\[LOOKUP_MEMORY:\s*(\d+)\s*\]\]")
READ_WEB_RE = re.compile(r"\[\[READ_WEB:\s*(https?://.*?)\s*\]\]", flags=re.DOTALL)
VIEW_IMAGE_RE = re.compile(r"\[\[VIEW_IMAGE:\s*#?(msg_\d{1,})\s*\]\]")
QUERY_TOOL_RE = re.compile(
    r"\[\[(?:VIEW_IMAGE:\s*#?msg_\d{1,}|CHECK_ROLES:\s*\d+|USER_STATS:\s*\d+|LOOKUP_MEMORY:\s*\d+|READ_WEB:\s*https?://.*?)\s*\]\]",
    flags=re.DOTALL,
)
STATUS_RE = re.compile(r"\[\[STATUS:\s*(.*?)\s*\]\]", flags=re.DOTALL)
MUTE_RE = re.compile(r"\[\[MUTE:\s*(\d+)\s*\|\s*(\d+)\s*\]\]")
POLL_RE = re.compile(r"\[\[POLL:\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\]\]", flags=re.DOTALL)
TW_TZ = timezone(timedelta(hours=8))
MAX_MUTE_SECONDS = 300
STATUS_CLEAR_SECONDS = 30 * 60
SLEEP_STATUS_TEXT = "悄悄去睡覺了 (＿ ＿)zZ"
DEFAULT_POLL_MINUTES = 24 * 60
MAX_POLL_MINUTES = 7 * 24 * 60
MAX_QUERY_TOOL_ROUNDS = 2


def _normalize_memory_date(date_text: str) -> str:
    text = date_text.strip()
    now = datetime.now(TW_TZ)
    relative_days = {
        "今天": 0,
        "今日": 0,
        "昨天": -1,
        "昨日": -1,
        "明天": 1,
        "明日": 1,
        "後天": 2,
        "大後天": 3,
        "前天": -2,
        "大前天": -3,
    }
    if text in relative_days:
        return (now + timedelta(days=relative_days[text])).strftime("%Y-%m-%d")
    return text or now.strftime("%Y-%m-%d")


def _build_opencc_converter():
    try:
        return opencc.OpenCC("s2tw")
    except Exception:
        return opencc.OpenCC("s2tw.json")


def _preview_text(text: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _parse_local_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None

    normalized = text.replace("/", "-")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TW_TZ)
    return parsed


def _strip_hidden_tool_tags(text: str) -> str:
    text = strip_balanced_tool_tags(text, MEMORY_TOOL_NAMES + ACTION_TOOL_NAMES)
    return TOOL_TAG_RE.sub("", text)


class Scheduler:
    def __init__(
        self,
        llm: OpenRouterLLM,
        memory_mgr: MemoryManager,
        stats: BotStats,
        max_queue_size: int,
        bot=None,
        short_memory_mgr=None,
        user_stats_mgr=None,
        conversation_logger: ConversationLogger | None = None,
        tool_stats_mgr=None,
        image_cache_ttl_seconds: int = 21600,
        image_cache_max_items: int = 500,
    ):
        self.queue = deque()
        self.llm = llm
        self.memory_mgr = memory_mgr
        self.stats = stats
        self.max_queue_size = max_queue_size
        self.bot = bot
        self.short_memory_mgr = short_memory_mgr
        self.user_stats_mgr = user_stats_mgr
        self.conversation_logger = conversation_logger
        self.tool_stats_mgr = tool_stats_mgr
        self.image_cache_ttl_seconds = image_cache_ttl_seconds
        self.image_cache_max_items = image_cache_max_items
        self.last_debug_record: dict | None = None
        self.processing = False
        self.lock = asyncio.Lock()
        self.opencc_converter = _build_opencc_converter()
        self.status_clear_task: asyncio.Task | None = None
        self.presence_schedule_task: asyncio.Task | None = None
        self.interrupt_generation = 0
        self.interrupt_event = asyncio.Event()

    def prune_image_cache(self, cache: dict[int, str], cache_times: dict[int, datetime]) -> None:
        if not cache:
            cache_times.clear()
            return

        now = datetime.now(TW_TZ)
        if self.image_cache_ttl_seconds > 0:
            for message_id, cached_at in list(cache_times.items()):
                if message_id in cache and (now - cached_at).total_seconds() > self.image_cache_ttl_seconds:
                    cache.pop(message_id, None)
                    cache_times.pop(message_id, None)

        for message_id in list(cache.keys()):
            cache_times.setdefault(message_id, now)

        if self.image_cache_max_items > 0 and len(cache) > self.image_cache_max_items:
            overflow = len(cache) - self.image_cache_max_items
            oldest_ids = sorted(cache, key=lambda message_id: cache_times.get(message_id, now))[:overflow]
            for message_id in oldest_ids:
                cache.pop(message_id, None)
                cache_times.pop(message_id, None)

    def _new_debug_record(self, req: Request, interaction_obj) -> dict:
        channel = getattr(interaction_obj, "channel", None)
        return {
            "time": datetime.now(TW_TZ).isoformat(timespec="seconds"),
            "mode": "proactive" if req.is_proactive else "triggered",
            "attention_reason": req.attention_reason,
            "channel_id": req.target_channel_id or getattr(channel, "id", None),
            "channel_name": req.target_channel_name or getattr(channel, "name", ""),
            "user_id": req.target_user_id,
            "user_name": req.target_user_name,
            "message_id": req.trigger_message_id,
            "original_message": _preview_text(req.original_message, 500),
            "initial_response": "",
            "tool_rounds": [],
            "no_need": False,
            "sent": False,
            "replied_to_message": False,
            "final_response": "",
        }

    def _store_debug_record(self, debug_record: dict) -> None:
        self.last_debug_record = debug_record

    def format_last_debug(self) -> str:
        if not self.last_debug_record:
            return "目前沒有debug紀錄。"

        record = self.last_debug_record
        lines = [
            f"時間：{record.get('time', '')}",
            f"模式：{record.get('mode', '')}",
            f"觸發原因：{record.get('attention_reason', '') or '未記錄'}",
            f"頻道：{record.get('channel_name', '')} ({record.get('channel_id', '')})",
            f"使用者：{record.get('user_name', '')} ({record.get('user_id', '')})",
            f"訊息ID：{record.get('message_id', '')}",
            f"是否回覆：{'是' if record.get('sent') else '否'}",
            f"是否NO_NEED：{'是' if record.get('no_need') else '否'}",
            f"指定回覆：{'是' if record.get('replied_to_message') else '否'}",
            f"原始訊息：{record.get('original_message', '')}",
            f"初次模型回覆：{record.get('initial_response', '')}",
        ]

        tool_rounds = record.get("tool_rounds", [])
        if tool_rounds:
            lines.append("工具：")
            for round_info in tool_rounds:
                tools = ", ".join(round_info.get("tools", [])) or "無"
                reports = " / ".join(round_info.get("reports", [])) or "無結果"
                lines.append(f"- 第{round_info.get('round', '?')}輪：{tools}")
                lines.append(f"  結果：{reports}")
        else:
            lines.append("工具：未使用")

        lines.append(f"最後回覆：{record.get('final_response', '')}")
        return "\n".join(lines)

    async def _write_conversation_log(
        self,
        req: Request,
        debug_record: dict,
        responded: bool,
        bot_response: str = "",
        replied_to_message: bool = False,
    ) -> None:
        if not self.conversation_logger:
            return

        tools = [
            tool
            for round_info in debug_record.get("tool_rounds", [])
            for tool in round_info.get("tools", [])
        ]
        channel = getattr(req.interaction_obj, "channel", None)
        await self.conversation_logger.write(
            {
                "log_type": "conversation",
                "time": datetime.now(TW_TZ).isoformat(timespec="seconds"),
                "mode": "proactive" if req.is_proactive else "triggered",
                "attention_reason": req.attention_reason,
                "channel_id": req.target_channel_id or getattr(channel, "id", None),
                "channel_name": req.target_channel_name or getattr(channel, "name", ""),
                "user_id": req.target_user_id,
                "user_name": req.target_user_name,
                "message_id": req.trigger_message_id,
                "user_message": _preview_text(req.original_message, 1200),
                "responded": responded,
                "replied_to_message": replied_to_message,
                "bot_response": _preview_text(bot_response, 4000),
                "tool_rounds": len(debug_record.get("tool_rounds", [])),
                "tools": tools,
            }
        )

    async def interrupt(self) -> int:
        async with self.lock:
            cleared = len(self.queue)
            self.queue.clear()
            self.interrupt_generation += 1
            self.interrupt_event.set()

        logger.info("管理員打斷輸出，已清除 %s 筆佇列", cleared)
        return cleared

    def start_presence_schedule(self) -> None:
        if not self.bot or self.presence_schedule_task:
            return

        self.presence_schedule_task = asyncio.create_task(self._presence_schedule_loop())

    async def _presence_schedule_loop(self) -> None:
        while True:
            now = datetime.now(TW_TZ)
            sleep_at = datetime.combine(now.date(), time(0, 30), tzinfo=TW_TZ)
            wake_at = datetime.combine(now.date(), time(8, 0), tzinfo=TW_TZ)

            if sleep_at <= now < wake_at:
                try:
                    await self._set_presence(SLEEP_STATUS_TEXT, discord.Status.idle, schedule_clear=False)
                except Exception as exc:
                    logger.error("自動狀態更新失敗: %s", exc)

                next_at = wake_at
                mode = "wake"
            elif now < sleep_at:
                next_at = sleep_at
                mode = "sleep"
            else:
                next_at = sleep_at + timedelta(days=1)
                mode = "sleep"

            await asyncio.sleep(max((next_at - now).total_seconds(), 0))

            try:
                if mode == "sleep":
                    await self._set_presence(SLEEP_STATUS_TEXT, discord.Status.idle, schedule_clear=False)
                else:
                    await self._clear_presence(discord.Status.online)
            except Exception as exc:
                logger.error("自動狀態更新失敗: %s", exc)

    async def _set_presence(
        self,
        text: str,
        status: discord.Status = discord.Status.online,
        schedule_clear: bool = True,
    ) -> None:
        if not self.bot:
            return

        if self.status_clear_task:
            self.status_clear_task.cancel()
            self.status_clear_task = None

        activity = discord.Game(name=text[:128]) if text else None
        await self.bot.change_presence(status=status, activity=activity)

        if schedule_clear:
            self.status_clear_task = asyncio.create_task(self._clear_presence_later())

    async def _clear_presence_later(self) -> None:
        try:
            await asyncio.sleep(STATUS_CLEAR_SECONDS)
            await self._clear_presence(discord.Status.online)
        except asyncio.CancelledError:
            return

    async def _clear_presence(self, status: discord.Status = discord.Status.online) -> None:
        if not self.bot:
            return

        if self.status_clear_task:
            self.status_clear_task.cancel()
            self.status_clear_task = None

        await self.bot.change_presence(status=status, activity=None)

    async def clear_presence(self) -> None:
        await self._clear_presence(discord.Status.online)

    def _split_response_parts(self, text: str) -> list[tuple[str, str | None]]:
        tokens = SPLIT_TOKEN_RE.split(text)
        parts: list[tuple[str, str | None]] = []
        pending_split: str | None = None

        for token in tokens:
            if not token:
                continue

            if SPLIT_TOKEN_RE.fullmatch(token):
                pending_split = token
                continue

            if token.strip():
                parts.append((token.strip(), pending_split))
            pending_split = None

        return parts

    def _response_items(self, text: str) -> list[tuple[str, str | None, str | None]]:
        tokens = RESPONSE_CONTROL_RE.split(text)
        items: list[tuple[str, str | None, str | None]] = []
        current_text: list[str] = []
        current_target: str | None = None
        pending_split: str | None = None

        def _flush() -> None:
            nonlocal pending_split
            content = "".join(current_text).strip()
            current_text.clear()
            if content:
                items.append((content, pending_split, current_target))
                pending_split = None

        for token in tokens:
            if not token:
                continue

            if SPLIT_TOKEN_RE.fullmatch(token):
                _flush()
                pending_split = token
                continue

            reply_match = REPLY_TO_RE.fullmatch(token)
            if reply_match:
                _flush()
                current_target = reply_match.group(1)
                continue

            current_text.append(token)

        _flush()
        return items

    def _typing_seconds(self, text: str) -> float:
        length = len(text.strip())
        if length <= 0:
            return 0.0

        return min(length * 0.1, 2.0)

    async def _sleep_or_interrupted(self, seconds: float, generation: int | None = None) -> bool:
        if generation is not None and generation != self.interrupt_generation:
            return True

        try:
            await asyncio.wait_for(self.interrupt_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return generation is not None and generation != self.interrupt_generation

        return generation is not None and generation != self.interrupt_generation

    async def _simulate_split_delay(
        self,
        channel,
        text: str,
        split_token: str | None,
        generation: int | None = None,
    ) -> bool:
        if not split_token:
            return True

        if split_token == "[[SPLIT-WAIT]]":
            async with channel.typing():
                if await self._sleep_or_interrupted(random.uniform(0.8, 2.0), generation):
                    return False
            if await self._sleep_or_interrupted(random.uniform(3.0, 8.0), generation):
                return False

        typing_seconds = self._typing_seconds(text)
        if typing_seconds <= 0:
            return True

        async with channel.typing():
            if await self._sleep_or_interrupted(typing_seconds, generation):
                return False

        return True

    def _guild_from_interaction(self, interaction_obj):
        return getattr(interaction_obj, "guild", None)

    async def _find_member(self, guild, user_id: int):
        if not guild:
            return None

        member = guild.get_member(user_id)
        if member:
            return member

        try:
            return await guild.fetch_member(user_id)
        except Exception:
            return None

    def _format_ping(self, guild, target_id: str) -> str:
        if guild:
            role = guild.get_role(int(target_id))
            if role:
                return f"<@&{target_id}>"

        return f"<@{target_id}>"

    def _apply_ping_tags(self, text: str, interaction_obj) -> str:
        guild = self._guild_from_interaction(interaction_obj)
        used_targets: set[str] = set()

        def _replace(match: re.Match[str]) -> str:
            target_id = match.group(1)
            if target_id in used_targets:
                return ""

            used_targets.add(target_id)
            return self._format_ping(guild, target_id)

        return PING_TAG_RE.sub(_replace, text)

    async def _apply_status_tags(self, raw_response: str) -> None:
        for status_text in STATUS_RE.findall(raw_response):
            clean_status = status_text.strip()
            if not clean_status:
                continue

            started = perf_counter()
            success = False
            try:
                await self._set_presence(clean_status)
                success = True
                logger.info("更新動態狀態: %s", clean_status)
            except Exception as exc:
                logger.error("更新動態狀態失敗: %s", exc)
            finally:
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("STATUS", (perf_counter() - started) * 1000, success)

    async def _apply_mute_tags(self, raw_response: str, interaction_obj) -> None:
        guild = self._guild_from_interaction(interaction_obj)
        if not guild:
            return

        for user_id_text, seconds_text in MUTE_RE.findall(raw_response):
            started = perf_counter()
            success = False
            try:
                seconds = max(1, min(int(seconds_text), MAX_MUTE_SECONDS))
                member = await self._find_member(guild, int(user_id_text))
                if not member:
                    logger.warning("禁言失敗，找不到成員: %s", user_id_text)
                    continue

                until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                await member.timeout(until, reason="Sayuki 任性禁言")
                success = True
                logger.info("已禁言 %s %s 秒", user_id_text, seconds)
            except Exception as exc:
                logger.error("禁言失敗: %s", exc)
            finally:
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("MUTE", (perf_counter() - started) * 1000, success)

    async def _send_tool_text_to_channel(self, channel, text: str, interaction_obj) -> bool:
        clean_text = self._apply_ping_tags(text, interaction_obj)
        clean_text = _strip_hidden_tool_tags(clean_text)
        clean_text = re.sub(r"<think>.*?(?:</think>|$)", "", clean_text, flags=re.DOTALL).strip()
        clean_text = self.opencc_converter.convert(clean_text)
        if not clean_text:
            return False

        sent_any = False
        for content, split_token, _ in self._response_items(clean_text):
            chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)] if content else [""]
            for index, chunk in enumerate(chunks):
                if not chunk:
                    continue
                await self._simulate_split_delay(channel, chunk, split_token if index == 0 else None)
                await channel.send(chunk)
                sent_any = True
        return sent_any

    async def _apply_dm_tags(self, raw_response: str, interaction_obj) -> None:
        tags = find_balanced_tool_tags(raw_response, ACTION_TOOL_NAMES)
        for tag in tags:
            if tag.name != "DM_USER":
                continue

            parts = [part.strip() for part in tag.body.split("|", 1)]
            if len(parts) != 2 or not parts[0].isdigit():
                continue

            started = perf_counter()
            success = False
            try:
                user = self.bot.get_user(int(parts[0])) if self.bot else None
                if not user and self.bot:
                    user = await self.bot.fetch_user(int(parts[0]))
                if not user:
                    continue

                dm_channel = user.dm_channel or await user.create_dm()
                success = await self._send_tool_text_to_channel(dm_channel, parts[1], interaction_obj)
            except Exception as exc:
                logger.error("私訊使用者失敗: %s", exc)
            finally:
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("DM_USER", (perf_counter() - started) * 1000, success)

    async def _apply_thread_tags(self, raw_response: str, interaction_obj) -> None:
        tags = find_balanced_tool_tags(raw_response, ACTION_TOOL_NAMES)
        for tag in tags:
            if tag.name != "THREAD":
                continue

            parts = [part.strip() for part in tag.body.split("|", 1)]
            if not parts or not parts[0]:
                continue

            title = parts[0][:100]
            first_message = parts[1] if len(parts) > 1 else ""
            started = perf_counter()
            success = False
            try:
                channel = getattr(interaction_obj, "channel", None)
                if not channel:
                    continue

                source_message = interaction_obj if isinstance(interaction_obj, discord.Message) else getattr(interaction_obj, "message", None)
                if source_message and hasattr(source_message, "create_thread"):
                    thread = await source_message.create_thread(name=title, auto_archive_duration=1440)
                else:
                    thread = await channel.create_thread(name=title, auto_archive_duration=1440)

                if first_message.strip():
                    await self._send_tool_text_to_channel(thread, first_message, interaction_obj)
                success = True
            except Exception as exc:
                logger.error("建立討論串失敗: %s", exc)
            finally:
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("THREAD", (perf_counter() - started) * 1000, success)

    async def _apply_nickname_tags(self, raw_response: str, interaction_obj) -> None:
        guild = self._guild_from_interaction(interaction_obj)
        if not guild:
            return

        tags = find_balanced_tool_tags(raw_response, ACTION_TOOL_NAMES)
        for tag in tags:
            if tag.name != "NICKNAME":
                continue

            parts = [part.strip() for part in tag.body.split("|", 1)]
            if len(parts) != 2 or not parts[0].isdigit():
                continue

            started = perf_counter()
            success = False
            try:
                member = await self._find_member(guild, int(parts[0]))
                if not member:
                    logger.warning("更改暱稱失敗，找不到成員: %s", parts[0])
                    continue

                nickname = self.opencc_converter.convert(parts[1]).strip()
                await member.edit(
                    nick=nickname[:32] if nickname else None,
                    reason="Sayuki 更改伺服器暱稱",
                )
                success = True
                logger.info("已更改 %s 的伺服器暱稱", parts[0])
            except Exception as exc:
                logger.error("更改伺服器暱稱失敗: %s", exc)
            finally:
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("NICKNAME", (perf_counter() - started) * 1000, success)

    def _parse_event_end_time(self, start_time: datetime, end_text: str) -> datetime | None:
        text = end_text.strip()
        if not text:
            return None

        try:
            minutes = int(text)
        except ValueError:
            return _parse_local_datetime(text)

        return start_time + timedelta(minutes=max(1, minutes))

    async def _apply_server_event_tags(self, raw_response: str, interaction_obj) -> None:
        guild = self._guild_from_interaction(interaction_obj)
        if not guild:
            return

        tags = find_balanced_tool_tags(raw_response, ACTION_TOOL_NAMES)
        for tag in tags:
            if tag.name != "SERVER_EVENT":
                continue

            parts = [part.strip() for part in tag.body.split("|", 4)]
            if len(parts) != 5:
                continue

            name, start_text, end_text, location, description = parts
            if not name or not location:
                continue

            start_time = _parse_local_datetime(start_text)
            if not start_time:
                logger.warning("建立伺服器活動失敗，開始時間格式錯誤: %s", start_text)
                continue

            end_time = self._parse_event_end_time(start_time, end_text)
            if not end_time:
                logger.warning("建立伺服器活動失敗，結束時間格式錯誤: %s", end_text)
                continue
            if end_time <= start_time:
                logger.warning("建立伺服器活動失敗，結束時間不可早於開始時間")
                continue

            started = perf_counter()
            success = False
            try:
                await guild.create_scheduled_event(
                    name=self.opencc_converter.convert(name)[:100],
                    start_time=start_time,
                    end_time=end_time,
                    entity_type=discord.EntityType.external,
                    privacy_level=discord.PrivacyLevel.guild_only,
                    location=self.opencc_converter.convert(location)[:100],
                    description=self.opencc_converter.convert(description)[:1000],
                    reason="Sayuki 建立伺服器活動",
                )
                success = True
                logger.info("已建立伺服器活動: %s", name)
            except Exception as exc:
                logger.error("建立伺服器活動失敗: %s", exc)
            finally:
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("SERVER_EVENT", (perf_counter() - started) * 1000, success)

    async def _build_image_tool_report(
        self,
        raw_response: str,
        req: Request,
        tool_events: list[str] | None = None,
    ) -> str:
        matches = VIEW_IMAGE_RE.findall(raw_response)
        if not matches:
            return ""

        self.prune_image_cache(req.image_description_cache, req.image_description_cache_times)
        image_reports = []
        seen_targets: set[str] = set()
        now = datetime.now(TW_TZ)

        for target_key in matches:
            target_started = perf_counter()
            if target_key in seen_targets:
                continue
            seen_targets.add(target_key)

            message_id = req.image_target_message_ids.get(target_key)
            if message_id and message_id in req.image_description_cache:
                cached_desc = req.image_description_cache[message_id]
                req.image_description_cache_times.setdefault(message_id, now)
                if tool_events is not None:
                    tool_events.append(f"VIEW_IMAGE #{target_key} cache-hit")
                image_reports.append(f"#{target_key}:\n{cached_desc}\n（以上為快取的圖片解析結果）")
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool(
                        "VIEW_IMAGE",
                        (perf_counter() - target_started) * 1000,
                        True,
                    )
                continue

            image_urls = req.image_targets.get(target_key)
            if not image_urls:
                if tool_events is not None:
                    tool_events.append(f"VIEW_IMAGE #{target_key} not-found")
                image_reports.append(f"#{target_key}: 找不到這則近期訊息的圖片，可能已超出可查看範圍。")
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool(
                        "VIEW_IMAGE",
                        (perf_counter() - target_started) * 1000,
                        False,
                    )
                continue

            lines = []
            target_success = True
            for index, image_url in enumerate(image_urls[:4], start=1):
                vl_started = perf_counter()
                desc = await self.llm.describe_image_async(image_url)
                vl_success = not desc.startswith("圖片解析失敗")
                target_success = target_success and vl_success
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool(
                        "VIEW_IMAGE_VL",
                        (perf_counter() - vl_started) * 1000,
                        vl_success,
                    )
                lines.append(f"- 圖片{index}: {desc}")
            if tool_events is not None:
                tool_events.append(f"VIEW_IMAGE #{target_key} vl-call x{min(len(image_urls), 4)}")

            if len(image_urls) > 4:
                lines.append(f"- 另有{len(image_urls) - 4}張圖片未解析。")

            full_desc = "\n".join(lines)
            if message_id:
                req.image_description_cache[message_id] = full_desc
                req.image_description_cache_times[message_id] = now
                self.prune_image_cache(req.image_description_cache, req.image_description_cache_times)
            image_reports.append(f"#{target_key}:\n{full_desc}")
            if self.tool_stats_mgr:
                await self.tool_stats_mgr.record_tool(
                    "VIEW_IMAGE",
                    (perf_counter() - target_started) * 1000,
                    target_success,
                )

        return "【系統圖片解析結果】\n" + "\n\n".join(image_reports)

    async def _build_roles_tool_report(
        self,
        raw_response: str,
        interaction_obj,
        tool_events: list[str] | None = None,
    ) -> str:
        matches = CHECK_ROLES_RE.findall(raw_response)
        if not matches:
            return ""

        guild = self._guild_from_interaction(interaction_obj)
        role_reports = []
        seen_users: set[str] = set()

        for user_id_text in matches:
            started = perf_counter()
            success = False
            if user_id_text in seen_users:
                continue
            seen_users.add(user_id_text)
            if tool_events is not None:
                tool_events.append(f"CHECK_ROLES {user_id_text}")

            member = await self._find_member(guild, int(user_id_text))
            if not member:
                role_reports.append(f"用戶ID {user_id_text}: 查不到此成員或不在目前伺服器")
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("CHECK_ROLES", (perf_counter() - started) * 1000, False)
                continue

            roles = [role for role in member.roles if role.name != "@everyone"]
            if not roles:
                role_reports.append(f"{member.display_name} ({user_id_text}): 沒有身分組")
                success = True
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("CHECK_ROLES", (perf_counter() - started) * 1000, success)
                continue

            role_lines = "\n".join(f"- {role.name}: {role.id}" for role in roles)
            role_reports.append(f"{member.display_name} ({user_id_text}) 的身分組:\n{role_lines}")
            success = True
            if self.tool_stats_mgr:
                await self.tool_stats_mgr.record_tool("CHECK_ROLES", (perf_counter() - started) * 1000, success)

        return (
            "【系統身分組查詢結果】\n"
            + "\n\n".join(role_reports)
            + "\n若要@身份組，可使用[[PING: 身分組id]]。"
        )

    async def _build_user_stats_tool_report(
        self,
        raw_response: str,
        tool_events: list[str] | None = None,
    ) -> str:
        matches = USER_STATS_RE.findall(raw_response)
        if not matches or not self.user_stats_mgr:
            return ""

        seen_users: set[str] = set()
        reports = []
        for user_id in matches:
            started = perf_counter()
            if user_id in seen_users:
                continue
            seen_users.add(user_id)
            if tool_events is not None:
                tool_events.append(f"USER_STATS {user_id}")
            report = self.user_stats_mgr.format_user_stats(user_id)
            reports.append(report)
            if self.tool_stats_mgr:
                await self.tool_stats_mgr.record_tool(
                    "USER_STATS",
                    (perf_counter() - started) * 1000,
                    not report.startswith("沒有找到"),
                )

        return "【系統使用者統計查詢結果】\n" + "\n\n".join(reports)

    async def _build_memory_lookup_tool_report(
        self,
        raw_response: str,
        tool_events: list[str] | None = None,
    ) -> str:
        matches = LOOKUP_MEMORY_RE.findall(raw_response)
        if not matches:
            return ""

        seen_users: set[str] = set()
        reports = []
        for user_id in matches:
            started = perf_counter()
            if user_id in seen_users:
                continue
            seen_users.add(user_id)
            if tool_events is not None:
                tool_events.append(f"LOOKUP_MEMORY {user_id}")

            memory_text = self.memory_mgr.get_formatted_memory(user_id=user_id)
            if memory_text == "無":
                reports.append(f"用戶ID {user_id}: 找不到完整記憶。")
                success = False
            else:
                reports.append(f"用戶ID {user_id} 的完整記憶:\n{memory_text}")
                success = True

            if self.tool_stats_mgr:
                await self.tool_stats_mgr.record_tool(
                    "LOOKUP_MEMORY",
                    (perf_counter() - started) * 1000,
                    success,
                )

        return "【系統完整記憶查詢結果】\n" + "\n\n".join(reports)

    async def _build_read_web_tool_report(
        self,
        raw_response: str,
        tool_events: list[str] | None = None,
    ) -> str:
        matches = READ_WEB_RE.findall(raw_response)
        if not matches:
            return ""

        seen_urls: set[str] = set()
        reports = []
        for raw_url in matches:
            started = perf_counter()
            url = raw_url.strip()
            if url in seen_urls:
                continue
            seen_urls.add(url)
            if tool_events is not None:
                tool_events.append(f"READ_WEB {url}")

            result = await read_web_page(url)
            reports.append(
                f"網址：{result.url}\n"
                f"原始文字量：{result.scanned_characters}字元\n\n"
                f"{result.text}"
            )
            if self.tool_stats_mgr:
                await self.tool_stats_mgr.record_tool(
                    "READ_WEB",
                    (perf_counter() - started) * 1000,
                    result.success,
                )

        return "【系統網頁讀取結果】\n" + "\n\n".join(reports)

    async def _run_query_tools_once(
        self,
        raw_response: str,
        req: Request,
        interaction_obj,
        debug_record: dict | None = None,
        round_number: int = 1,
    ) -> bool:
        if not QUERY_TOOL_RE.search(raw_response):
            return False

        async def _collect_reports() -> list[str]:
            tool_events: list[str] = []
            reports = [
                await self._build_image_tool_report(raw_response, req, tool_events),
                await self._build_roles_tool_report(raw_response, interaction_obj, tool_events),
                await self._build_user_stats_tool_report(raw_response, tool_events),
                await self._build_memory_lookup_tool_report(raw_response, tool_events),
                await self._build_read_web_tool_report(raw_response, tool_events),
            ]
            filtered_reports = [report for report in reports if report]
            if debug_record is not None and filtered_reports:
                debug_record["tool_rounds"].append(
                    {
                        "round": round_number,
                        "tools": tool_events,
                        "reports": [_preview_text(report, 160) for report in filtered_reports],
                    }
                )
            return filtered_reports

        if req.is_proactive:
            reports = await _collect_reports()
        else:
            async with interaction_obj.channel.typing():
                reports = await _collect_reports()

        if not reports:
            return False

        content_to_keep = QUERY_TOOL_RE.sub("", raw_response).strip()
        if content_to_keep:
            req.messages.append({"role": "assistant", "content": content_to_keep})

        req.messages.append(
            {
                "role": "user",
                "content": (
                    "\n\n".join(reports)
                    + "\n\n工具結果已提供。請直接根據結果自然地回覆使用者；"
                    "不要重複呼叫同一個查詢工具，也不要說出你用了後台工具。"
                ),
            }
        )
        return True

    def _parse_bool(self, value: str) -> bool:
        return value.strip().lower() in {"true", "yes", "y", "1", "複選", "多選", "是", "可", "可以"}

    def _build_poll(self, title: str, minutes_text: str, multiple_text: str, options_text: str):
        poll_cls = getattr(discord, "Poll", None)
        if not poll_cls:
            return None, "目前 discord.py 版本不支援 Discord Poll。"

        title = title.strip()[:300]
        if not title:
            return None, "投票標題不可為空。"

        options = [option.strip()[:55] for option in re.split(r"[,，、;；]", options_text) if option.strip()]
        if len(options) < 2:
            return None, "投票至少需要2個選項。"
        if len(options) > 10:
            options = options[:10]

        try:
            minutes = int(minutes_text.strip()) if minutes_text.strip() else DEFAULT_POLL_MINUTES
        except ValueError:
            minutes = DEFAULT_POLL_MINUTES
        minutes = max(1, min(minutes, MAX_POLL_MINUTES))

        try:
            poll = poll_cls(
                question=title,
                duration=timedelta(minutes=minutes),
                multiple=self._parse_bool(multiple_text),
            )
            for option in options:
                poll.add_answer(text=option)
            return poll, ""
        except Exception as exc:
            return None, f"建立投票失敗：{exc}"

    async def add_request(self, req: Request) -> bool:
        async with self.lock:
            if len(self.queue) >= self.max_queue_size:
                return False
            self.queue.append(req)

        if not self.processing:
            asyncio.create_task(self.process_queue())

        return True

    async def process_queue(self) -> None:
        async with self.lock:
            if self.processing:
                return
            self.processing = True

        try:
            while True:
                async with self.lock:
                    if not self.queue:
                        self.processing = False
                        return
                    req = self.queue.popleft()

                try:
                    await self.handle_request(req)
                except Exception as exc:
                    logger.error("處理錯誤: %s", exc)
        finally:
            self.processing = False

    async def send_response(
        self,
        interaction_obj,
        text: str,
        is_proactive: bool = False,
        reply_targets: dict[str, object] | None = None,
        embed=None,
        view=None,
        files=None,
        polls=None,
        generation: int | None = None,
    ) -> None:
        safe_content = text if text else ""
        target_channel = interaction_obj.channel
        reply_targets = reply_targets or {}
        polls = polls or []
        chunk_items: list[tuple[str, str | None, str | None]] = []
        response_items = self._response_items(safe_content) if safe_content else []
        if not response_items:
            response_items = [("", None, None)]

        for content, split_token, reply_target_key in response_items:
            chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)] if content else [""]
            for index, chunk in enumerate(chunks):
                chunk_items.append((chunk, split_token if index == 0 else None, reply_target_key))

        sent_any = False
        try:
            for index, (chunk, split_token, reply_target_key) in enumerate(chunk_items):
                if generation is not None and generation != self.interrupt_generation:
                    return

                is_last = index == len(chunk_items) - 1
                kwargs = {}
                if chunk:
                    kwargs["content"] = chunk
                if is_last:
                    if embed:
                        kwargs["embed"] = embed
                    if view:
                        kwargs["view"] = view
                    if files:
                        kwargs["files"] = files

                if not kwargs:
                    continue

                if not await self._simulate_split_delay(target_channel, chunk, split_token, generation):
                    return
                if generation is not None and generation != self.interrupt_generation:
                    return

                reply_target = reply_targets.get(reply_target_key or "")
                if reply_target:
                    await reply_target.reply(**kwargs, mention_author=False)
                elif isinstance(interaction_obj, discord.Interaction):
                    if not interaction_obj.response.is_done():
                        await interaction_obj.response.send_message(**kwargs)
                    else:
                        await interaction_obj.followup.send(**kwargs)
                else:
                    await target_channel.send(**kwargs)
                sent_any = True

            for poll in polls:
                if generation is not None and generation != self.interrupt_generation:
                    return
                started = perf_counter()
                try:
                    await target_channel.send(poll=poll)
                    sent_any = True
                    if self.tool_stats_mgr:
                        await self.tool_stats_mgr.record_tool("POLL", (perf_counter() - started) * 1000, True)
                except Exception as exc:
                    logger.error("建立投票失敗: %s", exc)
                    if self.tool_stats_mgr:
                        await self.tool_stats_mgr.record_tool("POLL", (perf_counter() - started) * 1000, False)
                    await target_channel.send(f"（系統提示：建立投票失敗 {exc}）")
                    sent_any = True
        finally:
            if sent_any:
                self.stats.record_sent_message()
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_bot_message()

    async def _bg_reminder_task(
        self,
        wait_minutes: float,
        content: str,
        user_name: str,
        target_uid: str,
        original_message: str,
        interaction_obj,
        context_messages: list,
    ) -> None:
        await asyncio.sleep(wait_minutes * 60)

        remind_context = (
            f"\n\n【定時提醒時間到】\n"
            f"- 使用者「{user_name}」設定提醒時的話：{original_message}\n"
            f"- 提醒內容：{content}\n"
            f"請根據以上資訊與下方的歷史紀錄，嚴格遵守你的 <rules>，用語氣自然地告訴使用者這件事。"
        )

        new_messages = context_messages.copy()
        new_messages.append({"role": "system", "content": remind_context})

        new_req = Request(new_messages, interaction_obj, is_proactive=True, prefix_text=f"<@{target_uid}>")
        new_req.target_user_id = int(target_uid) if target_uid.isdigit() else None
        new_req.target_user_name = user_name
        new_req.target_channel_id = getattr(interaction_obj.channel, "id", None)
        new_req.target_channel_name = getattr(
            interaction_obj.channel,
            "name",
            str(getattr(interaction_obj.channel, "id", "")),
        )
        new_req.target_guild_id = getattr(getattr(interaction_obj, "guild", None), "id", None)
        new_req.target_guild_name = getattr(getattr(interaction_obj, "guild", None), "name", "")
        new_req.trigger_message_id = getattr(interaction_obj, "id", None)
        new_req.attention_reason = "定時提醒"
        new_req.original_message = f"定時提醒：{content}"
        await self.add_request(new_req)

    async def handle_request(self, req: Request) -> None:
        interaction_obj = req.interaction_obj
        generation = self.interrupt_generation
        self.interrupt_event.clear()

        if req.is_proactive:
            raw_response = await self.llm.generate_async(req.messages, call_type="main_initial")
        else:
            async with interaction_obj.channel.typing():
                raw_response = await self.llm.generate_async(req.messages, call_type="main_initial")

        if generation != self.interrupt_generation:
            return

        if not req.original_message:
            req.original_message = req.messages[-1]["content"] if req.messages else ""
        debug_record = self._new_debug_record(req, interaction_obj)
        debug_record["initial_response"] = _preview_text(raw_response, 500)

        if "[[$NO_NEED_TO_ANSWER$]]" in raw_response:
            debug_record["no_need"] = True
            self._store_debug_record(debug_record)
            await self._write_conversation_log(req, debug_record, responded=False)
            if self.user_stats_mgr and req.target_user_id:
                await self.user_stats_mgr.record_no_response(req.target_user_id, req.target_user_name)
            return

        for round_number in range(1, MAX_QUERY_TOOL_ROUNDS + 1):
            used_query_tool = await self._run_query_tools_once(
                raw_response,
                req,
                interaction_obj,
                debug_record,
                round_number,
            )
            if not used_query_tool:
                break

            if generation != self.interrupt_generation:
                return

            if req.is_proactive:
                raw_response = await self.llm.generate_async(
                    req.messages,
                    max_search=1,
                    call_type=f"query_tool_followup_round_{round_number}",
                )
            else:
                async with interaction_obj.channel.typing():
                    raw_response = await self.llm.generate_async(
                        req.messages,
                        max_search=1,
                        call_type=f"query_tool_followup_round_{round_number}",
                    )

        if generation != self.interrupt_generation:
            return

        if "[[$NO_NEED_TO_ANSWER$]]" in raw_response:
            debug_record["no_need"] = True
            debug_record["final_response"] = _preview_text(raw_response, 500)
            self._store_debug_record(debug_record)
            await self._write_conversation_log(req, debug_record, responded=False)
            if self.user_stats_mgr and req.target_user_id:
                await self.user_stats_mgr.record_no_response(req.target_user_id, req.target_user_name)
            return

        replied_to_message = bool(REPLY_TO_RE.search(raw_response))
        if self.tool_stats_mgr:
            if replied_to_message:
                await self.tool_stats_mgr.record_tool("REPLY_TO", 0.0, True)
            for split_token in SPLIT_TOKEN_RE.findall(raw_response):
                tool_name = "SPLIT_WAIT" if split_token == "[[SPLIT-WAIT]]" else "SPLIT"
                await self.tool_stats_mgr.record_tool(tool_name, 0.0, True)

        user_name = interaction_obj.author.display_name if not req.is_interaction else interaction_obj.user.display_name
        req.user_name = user_name

        await self._apply_memory_tags(raw_response, req, interaction_obj)
        await self._apply_reactions(raw_response, interaction_obj)
        await self._apply_status_tags(raw_response)
        await self._apply_mute_tags(raw_response, interaction_obj)
        await self._apply_dm_tags(raw_response, interaction_obj)
        await self._apply_thread_tags(raw_response, interaction_obj)
        await self._apply_nickname_tags(raw_response, interaction_obj)
        await self._apply_server_event_tags(raw_response, interaction_obj)
        await self._schedule_reminder(raw_response, req, interaction_obj)

        ping_count = len(set(PING_TAG_RE.findall(raw_response)))
        if ping_count and self.tool_stats_mgr:
            for _ in range(ping_count):
                await self.tool_stats_mgr.record_tool("PING", 0.0, True)

        files_to_send = []
        embed_to_send = None

        for index, func_str in enumerate(re.findall(r"\[\[MATH_PLOT:\s*(.*?)\s*\]\]", raw_response, flags=re.DOTALL)):
            started = perf_counter()
            try:
                img_buf = await math_tools.MathToolkit.plot(func_str.strip())
                files_to_send.append(discord.File(img_buf, filename=f"plot_{index}.png"))
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("MATH_PLOT", (perf_counter() - started) * 1000, True)
            except Exception as exc:
                logger.error("畫圖失敗: %s", exc)
                raw_response += f"\n*(系統提示：繪圖失敗 {exc})*"
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("MATH_PLOT", (perf_counter() - started) * 1000, False)

        for index, latex_str in enumerate(re.findall(r"\[\[MATH_LATEX:\s*(.*?)\s*\]\]", raw_response, flags=re.DOTALL)):
            started = perf_counter()
            try:
                img_buf = await math_tools.MathToolkit.render_latex(latex_str.strip())
                files_to_send.append(discord.File(img_buf, filename=f"latex_{index}.png"))
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("MATH_LATEX", (perf_counter() - started) * 1000, True)
            except Exception as exc:
                logger.error("LaTeX渲染失敗: %s", exc)
                raw_response += f"\n*(系統提示：LaTeX渲染失敗 {exc})*"
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("MATH_LATEX", (perf_counter() - started) * 1000, False)

        view_to_send = None
        polls_to_send = []
        poll_errors = []
        for title, minutes_text, multiple_text, options_text in POLL_RE.findall(raw_response):
            started = perf_counter()
            poll, error = self._build_poll(title, minutes_text, multiple_text, options_text)
            if poll:
                polls_to_send.append(poll)
            elif error:
                poll_errors.append(error)
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("POLL", (perf_counter() - started) * 1000, False)

        if poll_errors:
            raw_response += "\n" + "\n".join(f"*(系統提示：{error})*" for error in poll_errors)

        ui_match = re.search(r"\[\[BUTTON_UI:\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\]\]", raw_response, flags=re.DOTALL)
        if ui_match:
            ui_title = self.opencc_converter.convert(ui_match.group(1).strip())
            ui_desc = self.opencc_converter.convert(ui_match.group(2).strip())
            ui_btns = [
                self.opencc_converter.convert(button.strip())
                for button in ui_match.group(3).split(",")
                if button.strip()
            ]

            if ui_btns:
                embed_to_send = discord.Embed(title=f"{ui_title}", description=ui_desc, color=0x2b2d31)
                view_to_send = InteractiveAskView(ui_title, ui_desc, ui_btns, self, interaction_obj, req.messages)
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("BUTTON_UI", 0.0, True)

        clean_text = self._apply_ping_tags(raw_response, interaction_obj)
        clean_text = _strip_hidden_tool_tags(clean_text)
        clean_text = re.sub(r"<think>.*?(?:</think>|$)", "", clean_text, flags=re.DOTALL).strip()
        clean_text = self.opencc_converter.convert(clean_text)

        if req.prefix_text:
            clean_text = f"{req.prefix_text} {clean_text}"

        if not clean_text and not embed_to_send and not files_to_send and not polls_to_send:
            debug_record["final_response"] = ""
            self._store_debug_record(debug_record)
            await self._write_conversation_log(req, debug_record, responded=False)
            return

        await self.send_response(
            interaction_obj,
            clean_text,
            req.is_proactive,
            reply_targets=req.reply_targets,
            embed=embed_to_send,
            view=view_to_send,
            files=files_to_send,
            polls=polls_to_send,
            generation=generation,
        )

        debug_record["sent"] = True
        debug_record["replied_to_message"] = replied_to_message
        debug_record["final_response"] = _preview_text(clean_text, 800)
        self._store_debug_record(debug_record)
        await self._write_conversation_log(
            req,
            debug_record,
            responded=True,
            bot_response=clean_text,
            replied_to_message=replied_to_message,
        )

        if self.user_stats_mgr and req.target_user_id:
            await self.user_stats_mgr.record_bot_response(
                req.target_user_id,
                req.target_user_name,
                req.target_channel_id or getattr(interaction_obj.channel, "id", 0),
                req.target_channel_name or getattr(interaction_obj.channel, "name", str(getattr(interaction_obj.channel, "id", ""))),
                replied_to_message,
            )

        if self.short_memory_mgr and clean_text and not req.is_proactive:
            target_user = interaction_obj.user if req.is_interaction else interaction_obj.author
            await self.short_memory_mgr.record_user_interaction(
                target_user.id,
                target_user.display_name,
                req.target_channel_id or getattr(interaction_obj.channel, "id", 0),
                req.target_channel_name or getattr(interaction_obj.channel, "name", str(getattr(interaction_obj.channel, "id", ""))),
                req.original_message,
                clean_text,
            )

    async def _apply_memory_tags(self, raw_response: str, req: Request, interaction_obj) -> None:
        memory_tags = find_balanced_tool_tags(raw_response, MEMORY_TOOL_NAMES)
        memory_tool_count = len(memory_tags)
        field_paths = {
            "name": "name",
            "nickname": "basic_info.nickname",
            "location": "basic_info.location",
            "mbti": "basic_info.mbti",
            "relationship": "relationship",
            "recent_events": "recent_events",
        }

        for tag in memory_tags:
            if tag.name == "MEM_SET":
                parts = [part.strip() for part in tag.body.split("|", 2)]
                if len(parts) == 3 and parts[0].isdigit() and parts[1] in field_paths:
                    await self.memory_mgr.set_profile_field(parts[0], field_paths[parts[1]], parts[2])

            elif tag.name == "MEM_HOBBY":
                parts = [part.strip() for part in tag.body.split("|", 1)]
                if len(parts) == 2 and parts[0].isdigit():
                    await self.memory_mgr.add_hobby(parts[0], parts[1])

            elif tag.name == "MEM_GOSSIP":
                parts = [part.strip() for part in tag.body.split("|", 2)]
                if len(parts) == 3 and parts[0].isdigit():
                    await self.memory_mgr.add_gossip(parts[0], parts[1], parts[2])

            elif tag.name == "MEM_EVENT":
                parts = [part.strip() for part in tag.body.split("|", 2)]
                if len(parts) == 3 and parts[0].isdigit():
                    await self.memory_mgr.add_important_event(parts[0], parts[2], parts[1])

            elif tag.name == "MEM_EVENT_FOR":
                parts = [part.strip() for part in tag.body.split("|", 4)]
                if len(parts) == 5 and parts[0].isdigit():
                    await self.memory_mgr.add_important_event(
                        parts[0],
                        parts[4],
                        parts[2],
                        _normalize_memory_date(parts[1]),
                        source=parts[3],
                        recorded_at=datetime.now(TW_TZ).strftime("%Y-%m-%d"),
                    )

            elif tag.name == "MEMORY":
                parts = [part.strip() for part in tag.body.split("|", 1)]
                if len(parts) == 2 and parts[0].isdigit():
                    await self.memory_mgr.add_memory(parts[0], parts[1], None)

            elif tag.name == "EDIT_MEMORY":
                parts = [part.strip() for part in tag.body.split("|", 1)]
                if len(parts) == 2 and parts[0].isdigit():
                    await self.memory_mgr.update_memory(parts[0], None, "", parts[1])

            elif tag.name == "DELETE_MEMORY":
                parts = [part.strip() for part in tag.body.split("|", 1)]
                if len(parts) == 2 and parts[0].isdigit():
                    await self.memory_mgr.delete_memory(parts[0], None, parts[1])

            elif tag.name == "PERMANENT_MEMORY":
                if tag.body:
                    await self.memory_mgr.add_permanent_memory(tag.body)

            elif tag.name == "EDIT_PERMANENT_MEMORY":
                parts = [part.strip() for part in tag.body.split(":", 1)]
                if len(parts) == 2:
                    await self.memory_mgr.update_permanent_memory(parts[0], parts[1])

            elif tag.name == "DELETE_PERMANENT_MEMORY":
                if tag.body:
                    await self.memory_mgr.delete_permanent_memory(tag.body)

            elif tag.name == "SERVER_MEMORY":
                parts = [part.strip() for part in tag.body.split("|", 1)]
                if len(parts) == 2:
                    guild = self._guild_from_interaction(interaction_obj)
                    guild_id = req.target_guild_id or getattr(guild, "id", None)
                    guild_name = req.target_guild_name or getattr(guild, "name", "")
                    await self.memory_mgr.add_server_memory(guild_id, guild_name, parts[0], parts[1])

            elif tag.name == "DELETE_SERVER_MEMORY":
                if tag.body:
                    guild = self._guild_from_interaction(interaction_obj)
                    guild_id = req.target_guild_id or getattr(guild, "id", None)
                    await self.memory_mgr.delete_server_memory(guild_id, tag.body)

        if memory_tool_count and self.tool_stats_mgr:
            for _ in range(memory_tool_count):
                await self.tool_stats_mgr.record_tool("MEMORY", 0.0, True)

    async def _apply_reactions(self, raw_response: str, interaction_obj) -> None:
        reactions = re.findall(r"\[\[REACT:\s*(.*?)\s*\]\]", raw_response, flags=re.DOTALL)
        for emoji in reactions:
            started = perf_counter()
            success = False
            try:
                target_msg = interaction_obj if isinstance(interaction_obj, discord.Message) else interaction_obj.message
                await target_msg.add_reaction(emoji.strip())
                success = True
            except Exception:
                pass
            finally:
                if self.tool_stats_mgr:
                    await self.tool_stats_mgr.record_tool("REACT", (perf_counter() - started) * 1000, success)

    async def _schedule_reminder(self, raw_response: str, req: Request, interaction_obj) -> None:
        remind_match = re.search(r"\[\[REMIND:\s*(\d+\.?\d*)\s*\|\s*(.*?)\s*\]\]", raw_response, flags=re.DOTALL)
        if not remind_match:
            return

        started = perf_counter()
        success = False
        try:
            minutes = float(remind_match.group(1))
            remind_content = remind_match.group(2).strip()
            target_uid = str(interaction_obj.author.id) if not req.is_interaction else str(interaction_obj.user.id)
            asyncio.create_task(
                self._bg_reminder_task(
                    minutes,
                    remind_content,
                    req.user_name,
                    target_uid,
                    req.original_message,
                    interaction_obj,
                    req.messages,
                )
            )
            success = True
            logger.info("設定提醒：%s 分鐘後 - %s", minutes, remind_content)
        except Exception as exc:
            logger.error("提醒設定失敗: %s", exc)
        finally:
            if self.tool_stats_mgr:
                await self.tool_stats_mgr.record_tool("REMIND", (perf_counter() - started) * 1000, success)
