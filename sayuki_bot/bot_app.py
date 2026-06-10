from __future__ import annotations

import logging
import json
import re
import tempfile
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
    bot = commands.Bot(command_prefix="!", intents=intents)

    state = BotState()
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
    memory_manager = MemoryManager(settings.memory_db_file, settings.permanent_memory_db_file)
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
    )

    bot.sayuki = SimpleNamespace(
        settings=settings,
        state=state,
        llm_engine=llm_engine,
        memory_manager=memory_manager,
        user_stats_manager=user_stats_manager,
        conversation_logger=conversation_logger,
        invocation_logger=invocation_logger,
        tool_stats_manager=tool_stats_manager,
        short_memory_manager=short_memory_manager,
        scheduler=scheduler,
        system_prompt=system_prompt,
        commands_synced=False,
    )

    def _is_admin(user_id: int) -> bool:
        return user_id in settings.admin_user_ids

    def _parse_discord_id(value: str) -> str | None:
        value = value.strip()
        mention_match = re.fullmatch(r"<@!?(\d+)>|<@&(\d+)>", value)
        if mention_match:
            return mention_match.group(1) or mention_match.group(2)

        return value if value.isdigit() else None

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
        discord_refs = await resolve_discord_references(bot, current_message, reference_text)
        relevant_memory_user_ids = _collect_relevant_memory_user_ids(
            interaction.user.id,
            history_msgs,
            bot_user_id,
            list(discord_refs.reply_targets.values()),
        )
        memory_context = memory_manager.get_relevant_memory_context(relevant_memory_user_ids)
        await short_memory_manager.digest_pending(
            llm_engine,
            interaction.channel.id,
            getattr(interaction.channel, "name", str(interaction.channel.id)),
            interaction.user.id,
            interaction.user.display_name,
        )
        scheduler.prune_image_cache(state.vl_description_cache, state.vl_description_cache_times)
        chat_history = build_chat_history(
            history_msgs,
            cached_msgs,
            current_message,
            bot_user_id,
            True,
            state.vl_description_cache,
        )
        sys_info = build_system_context(
            bot.sayuki.system_prompt,
            interaction.user.display_name,
            interaction.user.id,
            "系統讓你主動查看目前頻道",
            memory_context,
            memory_manager.get_permanent_memory(),
            short_memory_manager.build_context(interaction.channel.id, interaction.user.id),
            chat_history,
            state.stats,
            True,
        )
        msg_list = [{"role": "system", "content": sys_info}]
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
        req.trigger_message_id = current_message.id
        req.attention_reason = "slash指令主動查看"
        req.original_message = f"管理員觸發主動查看。{note_text.strip()}" if note_text else "管理員觸發主動查看"
        if not await scheduler.add_request(req):
            return False, "佇列已滿，主動查看沒有排進去"

        return True, f"已觸發主動查看，佇列中目前約 {len(scheduler.queue)} 筆"

    @bot.tree.command(name="sayuki_look", description="管理員限定：讓紗月主動查看目前頻道")
    @app_commands.describe(note="可選，補充一句想讓紗月留意的內容")
    async def sayuki_look(interaction: discord.Interaction, note: str | None = None):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, message = await _enqueue_proactive_from_interaction(interaction, note)
        prefix = "成功" if ok else "失敗"
        await interaction.followup.send(f"{prefix}：{message}", ephemeral=True)

    @bot.tree.command(name="sayuki_interrupt", description="管理員限定：清除佇列並打斷紗月尚未輸出的回覆")
    async def sayuki_interrupt(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        cleared = await scheduler.interrupt()
        await interaction.response.send_message(
            f"已打斷目前未輸出的內容，並清除 {cleared} 筆等待佇列。已經送出的訊息不會撤回。",
            ephemeral=True,
        )

    @bot.tree.command(name="sayuki_status", description="管理員限定：查看紗月目前狀態")
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
                    f"已同步指令：{'是' if bot.sayuki.commands_synced else '否'}",
                ]
            ),
            ephemeral=True,
        )

    @bot.tree.command(name="sayuki_tool_stats", description="管理員限定：查看工具使用統計")
    async def sayuki_tool_stats(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        stats_text = tool_stats_manager.format_tool_stats()
        if len(stats_text) > 1900:
            stats_text = stats_text[:1900] + "\n...（過長已截斷）"
        await interaction.response.send_message(f"```text\n{stats_text}\n```", ephemeral=True)

    @bot.tree.command(name="sayuki_debug_last", description="管理員限定：查看最近一次處理debug摘要")
    async def sayuki_debug_last(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        debug_text = scheduler.format_last_debug()
        if len(debug_text) > 1900:
            debug_text = debug_text[:1900] + "\n...（過長已截斷）"
        await interaction.response.send_message(f"```text\n{debug_text}\n```", ephemeral=True)

    @bot.tree.command(name="sayuki_logs", description="管理員限定：查看bot紀錄")
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

    @bot.tree.command(name="sayuki_clear_status", description="管理員限定：清空紗月目前動態狀態")
    async def sayuki_clear_status(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        await scheduler.clear_presence()
        await interaction.response.send_message("已清空動態狀態", ephemeral=True)

    @bot.tree.command(name="sayuki_reload_prompt", description="管理員限定：重新讀取 SYSTEM_PROMPT")
    async def sayuki_reload_prompt(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        bot.sayuki.system_prompt = load_system_prompt(settings.system_prompt_path)
        await interaction.response.send_message(f"已重新載入：{settings.system_prompt_path}", ephemeral=True)

    @bot.tree.command(name="sayuki_memory_user", description="管理員限定：查看指定使用者記憶")
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

    @bot.tree.command(name="sayuki_memory_permanent", description="管理員限定：查看永久記憶")
    async def sayuki_memory_permanent(interaction: discord.Interaction):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        memory_text = memory_manager.get_permanent_memory()
        if len(memory_text) > 1900:
            memory_text = memory_text[:1900] + "\n...（過長已截斷）"
        await interaction.response.send_message(f"```text\n{memory_text}\n```", ephemeral=True)

    @bot.tree.command(name="sayuki_user_stats", description="管理員限定：查看指定使用者互動統計")
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

    @bot.event
    async def on_ready():
        logger.info("Bot 就緒: %s", bot.user)
        await memory_manager.init_db()
        await user_stats_manager.init_db()
        await tool_stats_manager.init_db()
        await short_memory_manager.init_db()
        await _remember_developers()
        scheduler.start_presence_schedule()
        if not bot.sayuki.commands_synced:
            try:
                synced = await bot.tree.sync()
                bot.sayuki.commands_synced = True
                logger.info("已同步 %s 個 slash command", len(synced))
            except Exception as exc:
                logger.error("slash command 同步失敗: %s", exc)

    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return

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

            await user_stats_manager.record_trigger(
                message.author.id,
                message.author.display_name,
                message.channel.id,
                getattr(message.channel, "name", str(message.channel.id)),
                attention_reason,
            )

            history_msgs = cached_msgs[-settings.history_limit:]
            discord_refs = await resolve_discord_references(bot, message, message.content)
            relevant_memory_user_ids = _collect_relevant_memory_user_ids(
                message.author.id,
                history_msgs,
                bot_user_id,
                list(discord_refs.reply_targets.values()),
            )
            memory_context = memory_manager.get_relevant_memory_context(relevant_memory_user_ids)
            await short_memory_manager.digest_pending(
                llm_engine,
                message.channel.id,
                getattr(message.channel, "name", str(message.channel.id)),
                message.author.id,
                message.author.display_name,
            )
            scheduler.prune_image_cache(state.vl_description_cache, state.vl_description_cache_times)
            chat_history = build_chat_history(
                history_msgs,
                cached_msgs,
                message,
                bot_user_id,
                is_proactive,
                state.vl_description_cache,
            )

            sys_info = build_system_context(
                bot.sayuki.system_prompt,
                user_name,
                message.author.id,
                attention_reason,
                memory_context,
                memory_manager.get_permanent_memory(),
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

            msg_list = [{"role": "system", "content": sys_info}]
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
