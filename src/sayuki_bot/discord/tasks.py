"""Explicit, bounded work; ordinary chat never selects this entry point."""

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import discord

from sayuki_bot.api import Event, Turn

logger = logging.getLogger(__name__)


def enqueue_task(bot, interaction, prompt):
    if not interaction.guild or interaction.guild.id not in bot.runtime_guilds:
        return {"status": "rejected"}
    prompt = prompt.strip()
    if not 1 <= len(prompt) <= 4000:
        return {"status": "invalid"}
    now = datetime.now(timezone.utc)
    if bot.config.sleep.sleeping(now):
        return {"status": "sleeping"}
    event = Event(
        interaction.id,
        interaction.guild.id,
        interaction.channel_id,
        interaction.user.id,
        interaction.user.display_name,
        prompt,
        now,
    )
    turn = Turn(
        event,
        SimpleNamespace(channel=interaction.channel, author=interaction.user),
        list(bot.history[event.channel_id]),
        reason="task",
        bot_id=bot.user.id,
        completion=asyncio.get_running_loop().create_future(),
    )
    try:
        bot.access.channel(turn, event.channel_id)
    except PermissionError:
        return {"status": "rejected"}
    if not bot.engine.submit(turn):
        return {"status": "queue_full"}
    bot.db.record_event(event)
    return {"status": "queued", "task_id": turn.id, "completion": turn.completion}


def register_tasks(bot):
    group = discord.app_commands.Group(name="task", description="明確交辦較複雜的任務")

    @group.command(name="start", description="交辦任務；結果會在目前頻道回覆")
    @discord.app_commands.describe(prompt="想請紗月處理的事情（最多4000字）")
    async def start(interaction: discord.Interaction, prompt: str):
        await interaction.response.defer(ephemeral=True)
        result = enqueue_task(bot, interaction, prompt)
        if result["status"] != "queued":
            await interaction.followup.send(
                {
                    "rejected": "目前不能在這裡處理任務。",
                    "sleeping": "目前睡眠中，任務未排入；一般聊天可直接 @ 她。",
                    "invalid": "請提供1到4000字的任務內容。",
                    "queue_full": "目前佇列已滿，任務未排入。",
                }[result["status"]],
                ephemeral=True,
            )
            return
        identifier = result["task_id"]
        # Failure to deliver an acknowledgement must not erase the queued task.
        try:
            await interaction.followup.send(
                f"任務已排入：{identifier}；可用 /task cancel 加上此ID取消。", ephemeral=True
            )
        except discord.HTTPException:
            logger.warning("Task acknowledgement unavailable: %s", identifier)
        outcome = await asyncio.shield(result["completion"])
        if outcome["status"] != "completed":
            label = {
                "cancelled": "已取消；已送出的訊息不會撤回。",
                "timed_out": "已達處理時間上限；已完成的操作不會重做。",
                "unknown": "部分操作無法確認結果；沒有自動重做。",
                "failed": "未完成，請查看頻道中的回覆。",
            }.get(outcome["status"], "未完成。")
            try:
                await interaction.followup.send(f"任務 {identifier}：{label}", ephemeral=True)
            except discord.HTTPException:
                logger.warning("Task completion notice unavailable: %s", identifier)

    @group.command(name="cancel", description="取消自己在本頻道交辦的任務；管理員可取消他人的")
    async def cancel(interaction: discord.Interaction, task_id: str):
        allowed = interaction.guild and interaction.guild.id in bot.runtime_guilds
        found = bool(allowed) and bot.engine.cancel_task(
            interaction.guild.id,
            interaction.channel_id,
            interaction.user.id,
            task_id.strip(),
            admin=interaction.user.id in bot.config.admins,
        )
        await interaction.response.send_message(
            "已要求停止；已完成的操作不會撤回。" if found else "找不到你可取消的進行中任務。",
            ephemeral=True,
        )

    bot.tree.add_command(group)
