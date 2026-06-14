from __future__ import annotations

import asyncio
import logging
import json
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

from .config import TW_TZ, Settings, configure_logging, load_settings
from .conversation_log import ConversationLogger
from .discord_refs import resolve_discord_references
from .llm import OpenRouterLLM
from .memory import MemoryManager
from .message_context import (
    build_chat_history,
    build_system_context,
    get_attachment_info,
    get_attention_flags,
    get_cached_messages,
    should_echo_message,
    should_start_proactive,
)
from .models import Request
from .presence import PresenceManager
from .prompts import load_system_prompt
from .scheduler import Scheduler
from .short_term_memory import ShortTermMemoryManager
from .state import BotState
from .tool_stats import ToolStatsManager
from .user_stats import UserStatsManager


logger = logging.getLogger(__name__)


def create_bot(settings: Settings | None = None) -> commands.Bot:
    configure_logging()
    settings = settings or load_settings()

    if not settings.openrouter_api_key:
        logger.error("請在 .env 中設定 OPENROUTER_API_KEY")
        raise SystemExit(1)

    system_prompt = load_system_prompt(settings.system_prompt_path)

    intents = discord.Intents.default()
    intents.message_content = True
    intents.presences = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    state = BotState()
    presence_manager = PresenceManager(
        settings.presence_ttl_seconds,
        settings.presence_max_context_users,
        settings.presence_max_age_seconds,
    )
    tool_stats_manager = ToolStatsManager(settings.tool_stats_file)
    invocation_logger = ConversationLogger(settings.invocation_log_file)
    llm_engine = OpenRouterLLM(
        settings.openrouter_api_key,
        settings.openrouter_model,
        settings.openrouter_small_model,
        settings.openrouter_vl_model,
        tool_stats_manager,
        invocation_logger,
        settings.openrouter_use_reasoning_effort,
        settings.openrouter_reasoning_effort,
    )
    memory_manager = MemoryManager(
        settings.memory_db_file,
        settings.permanent_memory_db_file,
        settings.server_memory_file,
    )
    user_stats_manager = UserStatsManager(settings.user_stats_file)
    conversation_logger = ConversationLogger(settings.conversation_log_file)
    short_memory_manager = ShortTermMemoryManager(
        settings.short_memory_file,
        settings.short_memory_pending_file,
        settings.short_memory_ttl_seconds,
        settings.short_memory_trigger_messages,
        settings.short_memory_min_interval_seconds,
        settings.short_memory_max_context_chars,
        invocation_logger,
    )
    scheduler = Scheduler(
        llm_engine,
        memory_manager,
        state.stats,
        settings.max_queue_size,
        bot,
        short_memory_manager,
        user_stats_manager,
        conversation_logger,
        tool_stats_manager,
        settings.image_cache_ttl_seconds,
        settings.image_cache_max_items,
        settings.reminders_file,
        presence_manager,
    )

    bot.sayuki = SimpleNamespace(
        settings=settings,
        state=state,
        llm_engine=llm_engine,
        memory_manager=memory_manager,
        presence_manager=presence_manager,
        user_stats_manager=user_stats_manager,
        conversation_logger=conversation_logger,
        invocation_logger=invocation_logger,
        tool_stats_manager=tool_stats_manager,
        short_memory_manager=short_memory_manager,
        scheduler=scheduler,
        system_prompt=system_prompt,
        commands_synced=False,
        presence_cleanup_task=None,
    )

    def _is_admin(user_id: int) -> bool:
        return user_id in settings.admin_user_ids

    def _parse_discord_id(value: str) -> str | None:
        value = value.strip()
        mention_match = re.fullmatch(r"<@!?(\d+)>|<@&(\d+)>", value)
        if mention_match:
            return mention_match.group(1) or mention_match.group(2)

        return value if value.isdigit() else None

    async def _bot_member_for_guild(guild: discord.Guild):
        member = guild.get_member(bot.user.id) if bot.user else None
        if member:
            return member

        try:
            return await guild.fetch_member(bot.user.id)
        except Exception:
            return getattr(guild, "me", None)

    def _permission_line(label: str, value) -> str:
        if value is None:
            status = "未知"
        else:
            status = "OK" if value else "缺少"
        return f"{label}：{status}"

    @asynccontextmanager
    async def _typing_for_trigger(message: discord.Message, is_proactive: bool):
        if is_proactive:
            yield
            return

        async with message.channel.typing():
            yield

    def _log_time(event: dict) -> str:
        for key in ("time", "started_at", "recorded_at", "finished_at"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value
        return ""

    async def _read_log_entries(kind: str, day: str | None, limit: int) -> list[dict]:
        safe_limit = max(1, min(limit, 100))
        entries: list[dict] = []

        if kind in {"all", "conversation"}:
            entries.extend(await conversation_logger.read_recent(safe_limit, day, None))
        if kind in {"all", "llm"}:
            entries.extend(await invocation_logger.read_recent(safe_limit, day, "llm_call"))
        if kind in {"all", "short_memory"}:
            entries.extend(await invocation_logger.read_recent(safe_limit, day, "short_memory"))
            pending_logger = ConversationLogger(settings.short_memory_pending_file)
            entries.extend(await pending_logger.read_recent(safe_limit, day, "short_memory_pending"))

        entries.sort(key=_log_time)
        return entries[-safe_limit:]

    async def _send_log_entries(
        interaction: discord.Interaction,
        entries: list[dict],
        kind: str,
        day: str | None,
    ) -> None:
        if not entries:
            await interaction.followup.send("找不到符合條件的紀錄。", ephemeral=True)
            return

        content = "\n".join(json.dumps(entry, ensure_ascii=False, indent=2) for entry in entries)
        title = f"kind={kind} day={day or 'all'} count={len(entries)}"
        if len(content) <= 1700:
            await interaction.followup.send(f"{title}\n```json\n{content}\n```", ephemeral=True)
            return

        output_path = Path(tempfile.gettempdir()) / f"sayuki_logs_{kind}_{day or 'all'}.json"
        output_path.write_text(content + "\n", encoding="utf-8")
        await interaction.followup.send(
            f"{title}\n紀錄太長，已用檔案附上。",
            file=discord.File(output_path, filename=output_path.name),
            ephemeral=True,
        )

    def _collect_image_targets(messages: list[discord.Message]) -> dict[str, list[str]]:
        targets: dict[str, list[str]] = {}
        for msg in messages:
            urls = [
                attachment.url
                for attachment in msg.attachments
                if attachment.content_type and attachment.content_type.startswith("image/")
            ]
            if urls:
                targets[f"msg_{str(msg.id)[-4:]}"] = urls

        return targets

    def _collect_image_target_message_ids(messages: list[discord.Message]) -> dict[str, int]:
        targets: dict[str, int] = {}
        for msg in messages:
            if any(
                attachment.content_type and attachment.content_type.startswith("image/")
                for attachment in msg.attachments
            ):
                targets[f"msg_{str(msg.id)[-4:]}"] = msg.id

        return targets

    def _collect_relevant_memory_user_ids(
        primary_user_id: int | str,
        messages: list[discord.Message],
        bot_user_id: int,
        extra_messages: list[discord.Message] | None = None,
    ) -> set[str]:
        user_ids = {str(primary_user_id)}
        for msg in [*messages, *(extra_messages or [])]:
            author_id = getattr(getattr(msg, "author", None), "id", None)
            if author_id and author_id != bot_user_id:
                user_ids.add(str(author_id))

            for mentioned in getattr(msg, "mentions", []):
                if mentioned.id != bot_user_id:
                    user_ids.add(str(mentioned.id))

            reference = getattr(msg, "reference", None)
            resolved = getattr(reference, "resolved", None) if reference else None
            resolved_author_id = getattr(getattr(resolved, "author", None), "id", None)
            if resolved_author_id and resolved_author_id != bot_user_id:
                user_ids.add(str(resolved_author_id))

        return user_ids

    async def _remember_developers() -> None:
        for user_id in settings.developer_user_ids:
            await memory_manager.add_memory(
                str(user_id),
                '{"important_events":[{"date":"系統設定","event":"這位user是你的開發者","type":"身份"}]}',
            )

    async def _presence_cleanup_loop() -> None:
        while True:
            await asyncio.sleep(settings.presence_cleanup_interval_seconds)
            removed = presence_manager.cleanup_expired()
            if removed:
                logger.info("已清理 %s 筆過期Discord狀態快取", removed)

    async def _enqueue_proactive_from_interaction(
        interaction: discord.Interaction,
        note: str | None = None,
    ) -> tuple[bool, str]:
        if not interaction.channel or not hasattr(interaction.channel, "history"):
            return False, "這裡不能讀取頻道歷史，沒辦法主動查看"

        bot_user_id = bot.user.id
        history_msgs = [
            msg
            async for msg in interaction.channel.history(limit=settings.history_limit, oldest_first=True)
        ]
        history_msgs = [msg for msg in history_msgs if not msg.author.bot or msg.author.id == bot_user_id]
        if not history_msgs:
            return False, "目前讀不到近期訊息"

        current_message = history_msgs[-1]
        cached_msgs = history_msgs
        reference_text = "\n".join(msg.content for msg in history_msgs[-settings.history_limit:])
        discord_refs = await resolve_discord_references(
            bot,
            current_message,
            reference_text,
            component_context_cache=state.discord_component_context_cache,
            component_context_cache_times=state.discord_component_context_cache_times,
            component_cache_ttl_seconds=settings.discord_component_cache_ttl_seconds,
            component_cache_max_items=settings.discord_component_cache_max_items,
        )
        relevant_memory_user_ids = _collect_relevant_memory_user_ids(
            interaction.user.id,
            history_msgs,
            bot_user_id,
            list(discord_refs.reply_targets.values()),
        )
        memory_context = memory_manager.get_relevant_memory_context(relevant_memory_user_ids)
        presence_context = presence_manager.build_context(
            getattr(interaction.guild, "id", None),
            relevant_memory_user_ids,
        )
        await short_memory_manager.digest_pending(
            llm_engine,
            interaction.channel.id,
            getattr(interaction.channel, "name", str(interaction.channel.id)),
            interaction.user.id,
            interaction.user.display_name,
        )
        scheduler.prune_image_cache(state.vl_description_cache, state.vl_description_cache_times)
        chat_history = await build_chat_history(
            history_msgs,
            cached_msgs,
            current_message,
            bot_user_id,
            True,
            state.vl_description_cache,
            state.discord_component_context_cache,
            state.discord_component_context_cache_times,
            settings.discord_component_cache_ttl_seconds,
            settings.discord_component_cache_max_items,
        )
        sys_info = build_system_context(
            interaction.user.display_name,
            interaction.user.id,
            "系統讓你主動查看目前頻道",
            memory_context,
            memory_manager.get_permanent_memory(),
            memory_manager.get_server_memory(getattr(interaction.guild, "id", None)),
            presence_context,
            short_memory_manager.build_context(interaction.channel.id, interaction.user.id),
            chat_history,
            state.stats,
            True,
        )
        msg_list = [
            {"role": "system", "content": bot.sayuki.system_prompt},
            {"role": "system", "content": sys_info},
        ]
        note_text = f"\n補充訊息：{note.strip()}" if note and note.strip() else ""
        reference_text = f"\n\n【Discord標記解析】\n{discord_refs.context}" if discord_refs.context else ""
        msg_list.append(
            {
                "role": "user",
                "content": (
                    "【系統通知】請你自然地看一下目前頻道，根據近期群組對話決定是否回應。"
                    "不需要回覆時請輸出 [[$NO_NEED_TO_ANSWER$]]。"
                    f"{note_text}"
                    f"{reference_text}"
                ),
            }
        )

        reply_targets = {f"msg_{str(msg.id)[-4:]}": msg for msg in history_msgs}
        reply_targets.update(discord_refs.reply_targets)
        image_targets = _collect_image_targets(history_msgs)
        image_targets.update(discord_refs.image_targets)
        image_target_message_ids = _collect_image_target_message_ids(history_msgs)
        image_target_message_ids.update(discord_refs.image_target_message_ids)
        req = Request(
            msg_list,
            current_message,
            is_proactive=True,
            reply_targets=reply_targets,
            image_targets=image_targets,
            image_target_message_ids=image_target_message_ids,
            image_description_cache=state.vl_description_cache,
            image_description_cache_times=state.vl_description_cache_times,
        )
        req.target_user_id = interaction.user.id
        req.target_user_name = interaction.user.display_name
        req.target_channel_id = interaction.channel.id
        req.target_channel_name = getattr(interaction.channel, "name", str(interaction.channel.id))
        req.target_guild_id = getattr(interaction.guild, "id", None)
        req.target_guild_name = getattr(interaction.guild, "name", "")
        req.presence_context = presence_context
        req.trigger_message_id = current_message.id
        req.attention_reason = "slash指令主動查看"
        req.original_message = f"管理員觸發主動查看。{note_text.strip()}" if note_text else "管理員觸發主動查看"
        if not await scheduler.add_request(req):
            return False, "佇列已滿，主動查看沒有排進去"

        return True, f"已觸發主動查看，佇列中目前約 {len(scheduler.queue)} 筆"

    sayuki_group = app_commands.Group(name="sayuki", description="管理員限定：紗月管理指令")

    @sayuki_group.command(name="look", description="讓紗月主動查看目前頻道")
    @app_commands.describe(note="可選，補充一句想讓紗月留意的內容")
    async def sayuki_look(interaction: discord.Interaction, note: str | None = None):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, message = await _enqueue_proactive_from_interaction(interaction, note)
        prefix = "成功" if ok else "失敗"
        await interaction.followup.send(f"{prefix}：{message}", ephemeral=True)

    @sayuki_group.command(name="interrupt", description="清除佇列並打斷紗月尚未輸出的回覆")
    async def sayuki_interrupt(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        cleared = await scheduler.interrupt()
        await interaction.response.send_message(
            f"已打斷目前未輸出的內容，並清除 {cleared} 筆等待佇列。已經送出的訊息不會撤回。",
            ephemeral=True,
        )

    @sayuki_group.command(name="status", description="查看紗月目前狀態")
    async def sayuki_status(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        bot_message_counts = tool_stats_manager.get_bot_message_counts()
        await interaction.response.send_message(
            "\n".join(
                [
                    f"佇列：{len(scheduler.queue)} / {settings.max_queue_size}",
                    f"處理中：{'是' if scheduler.processing else '否'}",
                    f"今日回覆：{bot_message_counts['today']}（本次啟動 {state.stats.today_messages}）",
                    f"本月回覆：{bot_message_counts['month']}",
                    f"永久回覆：{bot_message_counts['total']}",
                    f"圖片快取：{len(state.vl_description_cache)} / {settings.image_cache_max_items}",
                    (
                        "Discord元件快取："
                        f"{len(state.discord_component_context_cache)} / "
                        f"{settings.discord_component_cache_max_items}"
                    ),
                    f"已同步指令：{'是' if bot.sayuki.commands_synced else '否'}",
                ]
            ),
            ephemeral=True,
        )

    @sayuki_group.command(name="permissions", description="診斷bot目前頻道/伺服器權限")
    async def sayuki_permissions(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        if not interaction.guild or not interaction.channel:
            await interaction.response.send_message("這個指令只能在Discord伺服器頻道內使用", ephemeral=True)
            return

        member = await _bot_member_for_guild(interaction.guild)
        if not member:
            await interaction.response.send_message("找不到bot自己的伺服器成員資料", ephemeral=True)
            return

        guild_perms = member.guild_permissions
        channel_perms = (
            interaction.channel.permissions_for(member)
            if hasattr(interaction.channel, "permissions_for")
            else guild_perms
        )
        send_polls = getattr(channel_perms, "send_polls", None)
        lines = [
            f"伺服器：{interaction.guild.name} ({interaction.guild.id})",
            f"頻道：#{getattr(interaction.channel, 'name', interaction.channel.id)} ({interaction.channel.id})",
            f"Bot成員：{member.display_name} ({member.id})",
            f"最高身分組：{member.top_role.name} (position:{member.top_role.position})",
            f"Presence快取筆數：{len(presence_manager.records)}",
            f"Discord元件快取筆數：{len(state.discord_component_context_cache)}",
            "",
            _permission_line("Administrator", guild_perms.administrator),
            _permission_line("Send Messages", channel_perms.send_messages),
            _permission_line("Read Message History", channel_perms.read_message_history),
            _permission_line("Add Reactions", channel_perms.add_reactions),
            _permission_line("Embed Links", channel_perms.embed_links),
            _permission_line("Attach Files", channel_perms.attach_files),
            _permission_line("Create Public Threads", getattr(channel_perms, "create_public_threads", None)),
            _permission_line("Send Messages in Threads", getattr(channel_perms, "send_messages_in_threads", None)),
            _permission_line("Send Polls", send_polls),
            _permission_line("Moderate Members / Timeout", guild_perms.moderate_members),
            _permission_line("Manage Nicknames", guild_perms.manage_nicknames),
            _permission_line("Manage Events", getattr(guild_perms, "manage_events", None)),
            "",
            "提示：禁言/改暱稱還會受身分組階級影響，bot最高身分組必須高於目標成員。",
        ]
        await interaction.response.send_message(f"```text\n{chr(10).join(lines)}\n```", ephemeral=True)

    @sayuki_group.command(name="tool_stats", description="查看工具使用統計")
    async def sayuki_tool_stats(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        stats_text = tool_stats_manager.format_tool_stats()
        if len(stats_text) > 1900:
            stats_text = stats_text[:1900] + "\n...（過長已截斷）"
        await interaction.response.send_message(f"```text\n{stats_text}\n```", ephemeral=True)

    @sayuki_group.command(name="debug_last", description="查看最近一次處理debug摘要")
    async def sayuki_debug_last(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        debug_text = scheduler.format_last_debug()
        if len(debug_text) > 1900:
            debug_text = debug_text[:1900] + "\n...（過長已截斷）"
        await interaction.response.send_message(f"```text\n{debug_text}\n```", ephemeral=True)

    @sayuki_group.command(name="logs", description="查看bot紀錄")
    @app_commands.describe(
        kind="紀錄分類：all/conversation/llm/short_memory",
        day="可選，YYYY-MM-DD，例如 2026-06-10",
        limit="最多顯示幾筆，1到100",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="全部", value="all"),
            app_commands.Choice(name="呼叫log", value="conversation"),
            app_commands.Choice(name="LLM調用log", value="llm"),
            app_commands.Choice(name="短期記憶log", value="short_memory"),
        ]
    )
    async def sayuki_logs(
        interaction: discord.Interaction,
        kind: str = "all",
        day: str | None = None,
        limit: int = 20,
    ):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        day = day.strip() if day else None
        if day and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            await interaction.response.send_message("day格式請用 YYYY-MM-DD，例如 2026-06-10", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        entries = await _read_log_entries(kind, day, limit)
        await _send_log_entries(interaction, entries, kind, day)

    @sayuki_group.command(name="reminders", description="查看待觸發提醒")
    @app_commands.describe(limit="最多顯示幾筆，1到50")
    async def sayuki_reminders(interaction: discord.Interaction, limit: int = 20):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        reminders_text = await scheduler.format_pending_reminders(limit)
        if len(reminders_text) > 1900:
            reminders_text = reminders_text[:1900] + "\n...（過長已截斷）"
        await interaction.response.send_message(f"```text\n{reminders_text}\n```", ephemeral=True)

    @sayuki_group.command(name="cancel_reminder", description="取消待觸發提醒")
    @app_commands.describe(reminder_id="提醒ID，可只貼前幾碼")
    async def sayuki_cancel_reminder(interaction: discord.Interaction, reminder_id: str):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        ok = await scheduler.cancel_reminder(reminder_id)
        message = "已取消提醒" if ok else "找不到符合的提醒ID"
        await interaction.response.send_message(message, ephemeral=True)

    @sayuki_group.command(name="clear_status", description="清空紗月目前動態狀態")
    async def sayuki_clear_status(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        await scheduler.clear_presence()
        await interaction.response.send_message("已清空動態狀態", ephemeral=True)

    @sayuki_group.command(name="reload_prompt", description="重新讀取SYSTEM_PROMPT")
    async def sayuki_reload_prompt(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        bot.sayuki.system_prompt = load_system_prompt(settings.system_prompt_path)
        await interaction.response.send_message(f"已重新載入：{settings.system_prompt_path}", ephemeral=True)

    @sayuki_group.command(name="memory_user", description="查看指定使用者記憶")
    @app_commands.describe(user_id="Discord 使用者ID，或直接貼使用者mention")
    async def sayuki_memory_user(interaction: discord.Interaction, user_id: str):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        parsed_user_id = _parse_discord_id(user_id)
        if not parsed_user_id:
            await interaction.response.send_message("user_id 必須是純數字，或像 <@123456789> 這樣的 mention", ephemeral=True)
            return

        memory_text = memory_manager.get_formatted_memory(user_id=parsed_user_id)
        if len(memory_text) > 1900:
            memory_text = memory_text[:1900] + "\n...（過長已截斷）"
        await interaction.response.send_message(f"```json\n{memory_text}\n```", ephemeral=True)

    @sayuki_group.command(name="memory_permanent", description="查看永久記憶")
    async def sayuki_memory_permanent(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        memory_text = memory_manager.get_permanent_memory()
        if len(memory_text) > 1900:
            memory_text = memory_text[:1900] + "\n...（過長已截斷）"
        await interaction.response.send_message(f"```text\n{memory_text}\n```", ephemeral=True)

    @sayuki_group.command(name="memory_server", description="查看目前伺服器記憶")
    async def sayuki_memory_server(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        if not interaction.guild:
            await interaction.response.send_message("這個指令只能在Discord伺服器內使用", ephemeral=True)
            return

        memory_text = memory_manager.get_server_memory(interaction.guild.id)
        content = f"伺服器：{interaction.guild.name} ({interaction.guild.id})\n\n{memory_text}"
        if len(content) <= 1900:
            await interaction.response.send_message(f"```text\n{content}\n```", ephemeral=True)
            return

        output_path = Path(tempfile.gettempdir()) / f"sayuki_server_memory_{interaction.guild.id}.txt"
        output_path.write_text(content + "\n", encoding="utf-8")
        await interaction.response.send_message(
            "伺服器記憶太長，已用檔案附上。",
            file=discord.File(output_path, filename=output_path.name),
            ephemeral=True,
        )

    @sayuki_group.command(name="user_stats", description="查看指定使用者互動統計")
    @app_commands.describe(user_id="Discord 使用者ID，或直接貼使用者mention")
    async def sayuki_user_stats(interaction: discord.Interaction, user_id: str):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        parsed_user_id = _parse_discord_id(user_id)
        if not parsed_user_id:
            await interaction.response.send_message("user_id 必須是純數字，或像 <@123456789> 這樣的 mention", ephemeral=True)
            return

        stats_text = user_stats_manager.format_user_stats(parsed_user_id)
        if len(stats_text) > 1900:
            stats_text = stats_text[:1900] + "\n...（過長已截斷）"
        await interaction.response.send_message(f"```text\n{stats_text}\n```", ephemeral=True)

    bot.tree.add_command(sayuki_group)

    @bot.event
    async def on_ready():
        logger.info("Bot 就緒: %s", bot.user)
        presence_manager.prime_from_guilds(list(bot.guilds))
        removed = presence_manager.cleanup_expired()
        if removed:
            logger.info("已清理 %s 筆過期Discord狀態快取", removed)
        if settings.presence_cleanup_interval_seconds > 0 and not bot.sayuki.presence_cleanup_task:
            bot.sayuki.presence_cleanup_task = asyncio.create_task(_presence_cleanup_loop())
        await memory_manager.init_db()
        await user_stats_manager.init_db()
        await tool_stats_manager.init_db()
        await short_memory_manager.init_db()
        await _remember_developers()
        scheduler.start_presence_schedule()
        await scheduler.init_reminders()
        if not bot.sayuki.commands_synced:
            try:
                if settings.command_sync_guild_ids:
                    total_synced = 0
                    for guild_id in settings.command_sync_guild_ids:
                        guild_obj = discord.Object(id=guild_id)
                        bot.tree.copy_global_to(guild=guild_obj)
                        synced = await bot.tree.sync(guild=guild_obj)
                        total_synced += len(synced)
                        logger.info("已同步 %s 個 guild slash command 到 %s", len(synced), guild_id)
                    logger.info("已同步 %s 個 guild slash command", total_synced)
                else:
                    synced = await bot.tree.sync()
                    logger.info("已同步 %s 個 slash command", len(synced))
                bot.sayuki.commands_synced = True
            except Exception as exc:
                logger.error("slash command 同步失敗: %s", exc)

    @bot.event
    async def on_presence_update(before: discord.Member, after: discord.Member):
        presence_manager.update_member(after)

    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return

        if isinstance(message.author, discord.Member):
            presence_manager.update_member(message.author)

        bot_user_id = bot.user.id
        cached_msgs = await get_cached_messages(message, state.channel_message_cache)
        await user_stats_manager.record_seen_message(message)
        await short_memory_manager.record_channel_message(message, bot_user_id)
        is_mentioned, is_keyword, is_reply = get_attention_flags(message, bot_user_id)

        should_echo = should_echo_message(
            message,
            cached_msgs,
            bot_user_id,
            state.echoed_messages,
            settings.echo_lookback_seconds,
            settings.echo_min_users,
        )
        if should_echo:
            await message.channel.send(message.clean_content.strip())

        is_proactive = False
        if not (is_mentioned or is_keyword or is_reply) and not message.content.startswith("!") and not should_echo:
            last_time = state.proactive_cooldowns.get(
                message.channel.id,
                datetime(1970, 1, 1, tzinfo=TW_TZ),
            )
            if should_start_proactive(message, cached_msgs, last_time, settings.proactive_cooldown_seconds):
                is_proactive = True
                state.proactive_cooldowns[message.channel.id] = datetime.now(TW_TZ)

        if is_mentioned or is_keyword or is_reply or is_proactive:
            user_name = message.author.display_name
            if is_proactive:
                attention_reason = "主動查看，沒有人提及你"
            elif is_mentioned:
                attention_reason = "目前對話者直接@了你"
            elif is_reply:
                attention_reason = "目前對話者回覆了你的訊息"
            else:
                attention_reason = "目前對話者提到你的名字或關鍵字"

            async with _typing_for_trigger(message, is_proactive):
                await user_stats_manager.record_trigger(
                    message.author.id,
                    message.author.display_name,
                    message.channel.id,
                    getattr(message.channel, "name", str(message.channel.id)),
                    attention_reason,
                )

                history_msgs = cached_msgs[-settings.history_limit:]
                discord_refs = await resolve_discord_references(
                    bot,
                    message,
                    message.content,
                    component_context_cache=state.discord_component_context_cache,
                    component_context_cache_times=state.discord_component_context_cache_times,
                    component_cache_ttl_seconds=settings.discord_component_cache_ttl_seconds,
                    component_cache_max_items=settings.discord_component_cache_max_items,
                )
                relevant_memory_user_ids = _collect_relevant_memory_user_ids(
                    message.author.id,
                    history_msgs,
                    bot_user_id,
                    list(discord_refs.reply_targets.values()),
                )
                memory_context = memory_manager.get_relevant_memory_context(relevant_memory_user_ids)
                presence_context = presence_manager.build_context(
                    getattr(message.guild, "id", None),
                    relevant_memory_user_ids,
                )
                await short_memory_manager.digest_pending(
                    llm_engine,
                    message.channel.id,
                    getattr(message.channel, "name", str(message.channel.id)),
                    message.author.id,
                    message.author.display_name,
                )
                scheduler.prune_image_cache(state.vl_description_cache, state.vl_description_cache_times)
                chat_history = await build_chat_history(
                    history_msgs,
                    cached_msgs,
                    message,
                    bot_user_id,
                    is_proactive,
                    state.vl_description_cache,
                    state.discord_component_context_cache,
                    state.discord_component_context_cache_times,
                    settings.discord_component_cache_ttl_seconds,
                    settings.discord_component_cache_max_items,
                )

                sys_info = build_system_context(
                    user_name,
                    message.author.id,
                    attention_reason,
                    memory_context,
                    memory_manager.get_permanent_memory(),
                    memory_manager.get_server_memory(getattr(message.guild, "id", None)),
                    presence_context,
                    short_memory_manager.build_context(message.channel.id, message.author.id),
                    chat_history,
                    state.stats,
                    is_proactive,
                )

                msg_content = message.content
                current_attachment = get_attachment_info(message)
                if current_attachment:
                    msg_content = f"{msg_content} {current_attachment}"
                log_message = msg_content
                if discord_refs.context:
                    msg_content = f"{msg_content}\n\n【Discord標記解析】\n{discord_refs.context}"

                msg_list = [
                    {"role": "system", "content": bot.sayuki.system_prompt},
                    {"role": "system", "content": sys_info},
                ]
                msg_list.append({"role": "user", "content": msg_content})

                reply_targets = {f"msg_{str(msg.id)[-4:]}": msg for msg in history_msgs}
                reply_targets.update(discord_refs.reply_targets)
                image_targets = _collect_image_targets(history_msgs)
                image_targets.update(discord_refs.image_targets)
                image_target_message_ids = _collect_image_target_message_ids(history_msgs)
                image_target_message_ids.update(discord_refs.image_target_message_ids)
                req = Request(
                    msg_list,
                    message,
                    is_proactive,
                    reply_targets=reply_targets,
                    image_targets=image_targets,
                    image_target_message_ids=image_target_message_ids,
                    image_description_cache=state.vl_description_cache,
                    image_description_cache_times=state.vl_description_cache_times,
                )
                req.target_user_id = message.author.id
                req.target_user_name = message.author.display_name
                req.target_channel_id = message.channel.id
                req.target_channel_name = getattr(message.channel, "name", str(message.channel.id))
                req.target_guild_id = getattr(message.guild, "id", None)
                req.target_guild_name = getattr(message.guild, "name", "")
                req.presence_context = presence_context
                req.trigger_message_id = message.id
                req.attention_reason = attention_reason
                req.original_message = log_message
                if not await scheduler.add_request(req):
                    try:
                        await message.reply("腦袋快過載了，請等我一下下...", mention_author=False)
                    except Exception:
                        await message.channel.send("（系統忙碌中）")

        await bot.process_commands(message)

    return bot


def run() -> None:
    settings = load_settings()
    bot = create_bot(settings)

    if not settings.token:
        print("錯誤: 缺少 BOT_TOKEN")
        return

    bot.run(settings.token)
