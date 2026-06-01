from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import deque
from datetime import datetime, time, timedelta, timezone

import discord
import opencc

import math_tools

from .llm import OpenRouterLLM
from .memory import MemoryManager
from .models import Request
from .state import BotStats
from .ui import InteractiveAskView


logger = logging.getLogger(__name__)

SPLIT_TOKEN_RE = re.compile(r"(\[\[SPLIT(?:-WAIT)?\]\])")
TOOL_TAG_RE = re.compile(r"\[\[(?!SPLIT(?:-WAIT)?\]\]|REPLY_TO:).*?\]\]", flags=re.DOTALL)
REPLY_TO_RE = re.compile(r"\[\[REPLY_TO:\s*#?(msg_\d{1,})\s*\]\]")
RESPONSE_CONTROL_RE = re.compile(r"(\[\[SPLIT(?:-WAIT)?\]\]|\[\[REPLY_TO:\s*#?msg_\d{1,}\s*\]\])")
PING_TAG_RE = re.compile(r"\[\[PING:\s*(\d+)\s*\]\]")
CHECK_ROLES_RE = re.compile(r"\[\[CHECK_ROLES:\s*(\d+)\s*\]\]")
STATUS_RE = re.compile(r"\[\[STATUS:\s*(.*?)\s*\]\]", flags=re.DOTALL)
MUTE_RE = re.compile(r"\[\[MUTE:\s*(\d+)\s*\|\s*(\d+)\s*\]\]")
TW_TZ = timezone(timedelta(hours=8))
MAX_MUTE_SECONDS = 300
STATUS_CLEAR_SECONDS = 30 * 60
SLEEP_STATUS_TEXT = "悄悄去睡覺了 (＿ ＿)zZ"


def _build_opencc_converter():
    try:
        return opencc.OpenCC("s2t")
    except Exception:
        return opencc.OpenCC("s2t.json")


class Scheduler:
    def __init__(
        self,
        llm: OpenRouterLLM,
        memory_mgr: MemoryManager,
        stats: BotStats,
        max_queue_size: int,
        bot=None,
    ):
        self.queue = deque()
        self.llm = llm
        self.memory_mgr = memory_mgr
        self.stats = stats
        self.max_queue_size = max_queue_size
        self.bot = bot
        self.processing = False
        self.lock = asyncio.Lock()
        self.opencc_converter = _build_opencc_converter()
        self.status_clear_task: asyncio.Task | None = None
        self.presence_schedule_task: asyncio.Task | None = None
        self.interrupt_generation = 0
        self.interrupt_event = asyncio.Event()

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
        if length <= 2:
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

            try:
                await self._set_presence(clean_status)
                logger.info("更新動態狀態: %s", clean_status)
            except Exception as exc:
                logger.error("更新動態狀態失敗: %s", exc)

    async def _apply_mute_tags(self, raw_response: str, interaction_obj) -> None:
        guild = self._guild_from_interaction(interaction_obj)
        if not guild:
            return

        for user_id_text, seconds_text in MUTE_RE.findall(raw_response):
            try:
                seconds = max(1, min(int(seconds_text), MAX_MUTE_SECONDS))
                member = await self._find_member(guild, int(user_id_text))
                if not member:
                    logger.warning("禁言失敗，找不到成員: %s", user_id_text)
                    continue

                until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                await member.timeout(until, reason="Sayuki 任性禁言")
                logger.info("已禁言 %s %s 秒", user_id_text, seconds)
            except Exception as exc:
                logger.error("禁言失敗: %s", exc)

    async def _check_roles_once(self, raw_response: str, req: Request, interaction_obj) -> str:
        matches = CHECK_ROLES_RE.findall(raw_response)
        if not matches:
            return raw_response

        guild = self._guild_from_interaction(interaction_obj)
        role_reports = []

        for user_id_text in matches:
            member = await self._find_member(guild, int(user_id_text))
            if not member:
                role_reports.append(f"用戶ID {user_id_text}: 查不到此成員或不在目前伺服器")
                continue

            roles = [role for role in member.roles if role.name != "@everyone"]
            if not roles:
                role_reports.append(f"{member.display_name} ({user_id_text}): 沒有身分組")
                continue

            role_lines = "\n".join(f"- {role.name}: {role.id}" for role in roles)
            role_reports.append(f"{member.display_name} ({user_id_text}) 的身分組:\n{role_lines}")

        content_to_keep = CHECK_ROLES_RE.sub("", raw_response).strip()
        if content_to_keep:
            req.messages.append({"role": "assistant", "content": content_to_keep})

        req.messages.append(
            {
                "role": "user",
                "content": (
                    "【系統身分組查詢結果】\n"
                    + "\n\n".join(role_reports)
                    + "\n請根據結果自然地回覆使用者。若要@身份組，可使用[[PING: 身分組id]]。"
                ),
            }
        )

        if req.is_proactive:
            return await self.llm.generate_async(req.messages, max_search=1)

        async with interaction_obj.channel.typing():
            return await self.llm.generate_async(req.messages, max_search=1)

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
        generation: int | None = None,
    ) -> None:
        self.stats.record_sent_message()

        safe_content = text if text else ""
        target_channel = interaction_obj.channel
        reply_targets = reply_targets or {}
        chunk_items: list[tuple[str, str | None, str | None]] = []
        response_items = self._response_items(safe_content) if safe_content else []
        if not response_items:
            response_items = [("", None, None)]

        for content, split_token, reply_target_key in response_items:
            chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)] if content else [""]
            for index, chunk in enumerate(chunks):
                chunk_items.append((chunk, split_token if index == 0 else None, reply_target_key))

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
        await self.add_request(new_req)

    async def handle_request(self, req: Request) -> None:
        interaction_obj = req.interaction_obj
        generation = self.interrupt_generation
        self.interrupt_event.clear()

        if req.is_proactive:
            raw_response = await self.llm.generate_async(req.messages)
        else:
            async with interaction_obj.channel.typing():
                raw_response = await self.llm.generate_async(req.messages)

        if generation != self.interrupt_generation:
            return

        if "[[$NO_NEED_TO_ANSWER$]]" in raw_response:
            return

        raw_response = await self._check_roles_once(raw_response, req, interaction_obj)

        if generation != self.interrupt_generation:
            return

        if "[[$NO_NEED_TO_ANSWER$]]" in raw_response:
            return

        user_name = interaction_obj.author.display_name if not req.is_interaction else interaction_obj.user.display_name
        req.user_name = user_name
        req.original_message = req.messages[-1]["content"] if req.messages else ""

        await self._apply_memory_tags(raw_response)
        await self._apply_reactions(raw_response, interaction_obj)
        await self._apply_status_tags(raw_response)
        await self._apply_mute_tags(raw_response, interaction_obj)
        await self._schedule_reminder(raw_response, req, interaction_obj)

        files_to_send = []
        embed_to_send = None

        for index, func_str in enumerate(re.findall(r"\[\[MATH_PLOT:\s*(.*?)\s*\]\]", raw_response, flags=re.DOTALL)):
            try:
                img_buf = await math_tools.MathToolkit.plot(func_str.strip())
                files_to_send.append(discord.File(img_buf, filename=f"plot_{index}.png"))
            except Exception as exc:
                logger.error("畫圖失敗: %s", exc)
                raw_response += f"\n*(系統提示：繪圖失敗 {exc})*"

        for index, latex_str in enumerate(re.findall(r"\[\[MATH_LATEX:\s*(.*?)\s*\]\]", raw_response, flags=re.DOTALL)):
            try:
                img_buf = await math_tools.MathToolkit.render_latex(latex_str.strip())
                files_to_send.append(discord.File(img_buf, filename=f"latex_{index}.png"))
            except Exception as exc:
                logger.error("LaTeX渲染失敗: %s", exc)
                raw_response += f"\n*(系統提示：LaTeX渲染失敗 {exc})*"

        view_to_send = None
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

        clean_text = self._apply_ping_tags(raw_response, interaction_obj)
        clean_text = TOOL_TAG_RE.sub("", clean_text)
        clean_text = re.sub(r"<think>.*?(?:</think>|$)", "", clean_text, flags=re.DOTALL).strip()
        clean_text = self.opencc_converter.convert(clean_text)

        if req.prefix_text:
            clean_text = f"{req.prefix_text} {clean_text}"

        if not clean_text and not embed_to_send and not files_to_send:
            return

        await self.send_response(
            interaction_obj,
            clean_text,
            req.is_proactive,
            reply_targets=req.reply_targets,
            embed=embed_to_send,
            view=view_to_send,
            files=files_to_send,
            generation=generation,
        )

    async def _apply_memory_tags(self, raw_response: str) -> None:
        for target_id, field, value in re.findall(
            r"\[\[MEM_SET:\s*(\d+)\s*\|\s*(name|nickname|location|mbti|relationship|recent_events)\s*\|\s*(.*?)\s*\]\]",
            raw_response,
            flags=re.DOTALL,
        ):
            field_paths = {
                "name": "name",
                "nickname": "basic_info.nickname",
                "location": "basic_info.location",
                "mbti": "basic_info.mbti",
                "relationship": "relationship",
                "recent_events": "recent_events",
            }
            await self.memory_mgr.set_profile_field(target_id, field_paths[field], value)

        for target_id, hobby in re.findall(r"\[\[MEM_HOBBY:\s*(\d+)\s*\|\s*(.*?)\s*\]\]", raw_response, flags=re.DOTALL):
            await self.memory_mgr.add_hobby(target_id, hobby)

        for target_id, source, content in re.findall(
            r"\[\[MEM_GOSSIP:\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\]\]",
            raw_response,
            flags=re.DOTALL,
        ):
            await self.memory_mgr.add_gossip(target_id, source, content)

        for target_id, event_type, event in re.findall(
            r"\[\[MEM_EVENT:\s*(\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\]\]",
            raw_response,
            flags=re.DOTALL,
        ):
            await self.memory_mgr.add_important_event(target_id, event, event_type)

        for match in re.findall(r"\[\[MEMORY:\s*(.*?)\]\]", raw_response, flags=re.DOTALL):
            parts = match.split("|", 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                target_id = parts[0].strip()
                content = parts[1].strip()
                if target_id.isdigit():
                    await self.memory_mgr.add_memory(target_id, content, None)

        for match in re.findall(r"\[\[EDIT_MEMORY:\s*(.*?)\]\]", raw_response, flags=re.DOTALL):
            parts = match.split("|", 1)
            if len(parts) == 2:
                target_id = parts[0].strip()
                new_content = parts[1].strip()
                if target_id.isdigit():
                    await self.memory_mgr.update_memory(target_id, None, "", new_content)

        for match in re.findall(r"\[\[DELETE_MEMORY:\s*(.*?)\]\]", raw_response, flags=re.DOTALL):
            parts = match.split("|", 1)
            if len(parts) == 2:
                target_id = parts[0].strip()
                path = parts[1].strip()
                if target_id.isdigit():
                    await self.memory_mgr.delete_memory(target_id, None, path)

        for match in re.findall(r"\[\[PERMANENT_MEMORY:\s*(.*?)\]\]", raw_response, flags=re.DOTALL):
            content = match.strip()
            if content:
                await self.memory_mgr.add_permanent_memory(content)

        for match in re.findall(r"\[\[EDIT_PERMANENT_MEMORY:\s*(.*?)\]\]", raw_response, flags=re.DOTALL):
            parts = match.split(":", 1)
            if len(parts) == 2:
                fact_id = parts[0].strip()
                new_content = parts[1].strip()
                await self.memory_mgr.update_permanent_memory(fact_id, new_content)

        for match in re.findall(r"\[\[DELETE_PERMANENT_MEMORY:\s*(.*?)\]\]", raw_response, flags=re.DOTALL):
            fact_id = match.strip()
            if fact_id:
                await self.memory_mgr.delete_permanent_memory(fact_id)

    async def _apply_reactions(self, raw_response: str, interaction_obj) -> None:
        reactions = re.findall(r"\[\[REACT:\s*(.*?)\s*\]\]", raw_response, flags=re.DOTALL)
        for emoji in reactions:
            try:
                target_msg = interaction_obj if isinstance(interaction_obj, discord.Message) else interaction_obj.message
                await target_msg.add_reaction(emoji.strip())
            except Exception:
                pass

    async def _schedule_reminder(self, raw_response: str, req: Request, interaction_obj) -> None:
        remind_match = re.search(r"\[\[REMIND:\s*(\d+\.?\d*)\s*\|\s*(.*?)\s*\]\]", raw_response, flags=re.DOTALL)
        if not remind_match:
            return

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
            logger.info("設定提醒：%s 分鐘後 - %s", minutes, remind_content)
        except Exception as exc:
            logger.error("提醒設定失敗: %s", exc)
