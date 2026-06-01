from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

from .config import TW_TZ, Settings, configure_logging, load_settings
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
from .state import BotState


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
    llm_engine = OpenRouterLLM(
        settings.openrouter_api_key,
        settings.openrouter_model,
        settings.openrouter_vl_model,
    )
    memory_manager = MemoryManager(settings.memory_db_file, settings.permanent_memory_db_file)
    scheduler = Scheduler(llm_engine, memory_manager, state.stats, settings.max_queue_size, bot)

    bot.sayuki = SimpleNamespace(
        settings=settings,
        state=state,
        llm_engine=llm_engine,
        memory_manager=memory_manager,
        scheduler=scheduler,
        system_prompt=system_prompt,
        commands_synced=False,
    )

    def _is_admin(user_id: int) -> bool:
        return user_id in settings.admin_user_ids

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
            memory_manager.get_all_memory(),
            memory_manager.get_permanent_memory(),
            chat_history,
            state.stats,
            True,
        )
        msg_list = [{"role": "system", "content": sys_info}]
        note_text = f"\n補充訊息：{note.strip()}" if note and note.strip() else ""
        msg_list.append(
            {
                "role": "user",
                "content": (
                    "【系統通知】請你自然地看一下目前頻道，根據近期群組對話決定是否回應。"
                    "不需要回覆時請輸出 [[$NO_NEED_TO_ANSWER$]]。"
                    f"{note_text}"
                ),
            }
        )

        reply_targets = {f"msg_{str(msg.id)[-4:]}": msg for msg in history_msgs}
        req = Request(msg_list, current_message, is_proactive=True, reply_targets=reply_targets)
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

        await interaction.response.send_message(
            "\n".join(
                [
                    f"佇列：{len(scheduler.queue)} / {settings.max_queue_size}",
                    f"處理中：{'是' if scheduler.processing else '否'}",
                    f"今日發言：{state.stats.today_messages}",
                    f"總發言：{state.stats.total_messages}",
                    f"已同步指令：{'是' if bot.sayuki.commands_synced else '否'}",
                ]
            ),
            ephemeral=True,
        )

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
    @app_commands.describe(user_id="Discord 使用者ID")
    async def sayuki_memory_user(interaction: discord.Interaction, user_id: str):
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("你沒有權限使用這個指令", ephemeral=True)
            return

        if not user_id.isdigit():
            await interaction.response.send_message("user_id 必須是純數字", ephemeral=True)
            return

        memory_text = memory_manager.get_formatted_memory(user_id=user_id)
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

    @bot.event
    async def on_ready():
        logger.info("Bot 就緒: %s", bot.user)
        await memory_manager.init_db()
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

            history_msgs = cached_msgs[-settings.history_limit:]
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
                memory_manager.get_all_memory(),
                memory_manager.get_permanent_memory(),
                chat_history,
                state.stats,
                is_proactive,
            )

            msg_content = message.content
            vl_descriptions = []
            for attachment in message.attachments:
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    async with message.channel.typing():
                        desc = await llm_engine.describe_image_async(attachment.url)
                        vl_descriptions.append(f"圖片內容：{desc}")

            if vl_descriptions:
                full_desc = "\n".join(vl_descriptions)
                msg_content += f"\n\n[系統視覺解析：{full_desc}]"
                state.vl_description_cache[message.id] = full_desc

            current_attachment = get_attachment_info(message)
            if current_attachment and not vl_descriptions:
                msg_content = f"{msg_content} {current_attachment}"

            msg_list = [{"role": "system", "content": sys_info}]
            msg_list.append({"role": "user", "content": msg_content})

            reply_targets = {f"msg_{str(msg.id)[-4:]}": msg for msg in history_msgs}
            req = Request(msg_list, message, is_proactive, reply_targets=reply_targets)
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
