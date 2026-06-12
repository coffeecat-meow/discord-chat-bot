from __future__ import annotations

from datetime import datetime

import discord

from .config import TW_TZ
from .models import Request


class InteractiveAskView(discord.ui.View):
    def __init__(
        self,
        title: str,
        description: str,
        options: list,
        scheduler,
        interaction_obj,
        context_messages: list,
    ):
        super().__init__(timeout=None)
        self.title = title
        self.description = description
        self.scheduler = scheduler
        self.original_interaction = interaction_obj
        self.context_messages = context_messages
        self.created_time = datetime.now(TW_TZ)

        if isinstance(interaction_obj, discord.Interaction):
            self.original_user_id = interaction_obj.user.id
        else:
            self.original_user_id = interaction_obj.author.id

        for option in options[:25]:
            button = discord.ui.Button(label=option.strip(), style=discord.ButtonStyle.primary)
            button.callback = self.make_callback(option.strip())
            self.add_item(button)

    def make_callback(self, option: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.original_user_id:
                await interaction.response.send_message("這不是你的選擇喔～", ephemeral=True)
                return

            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)

            now = datetime.now(TW_TZ)
            delta = now - self.created_time
            mins = int(delta.total_seconds() // 60)

            ui_context = (
                f"\n\n【互動反饋】使用者點擊了「你」在 {self.created_time.strftime('%H:%M')} 發送的面板。\n"
                f"- 面板標題：{self.title}\n"
                f"- 面板描述：{self.description}\n"
                f"- 思考時間：使用者考慮了約 {mins} 分鐘後做出決定。\n"
                f"- 使用者選擇：{option}\n"
                f"請根據以上資訊與下方的歷史紀錄，延續紗月的人設給予回覆。"
            )

            new_messages = self.context_messages.copy()
            new_messages.append({"role": "system", "content": ui_context})
            new_messages.append({"role": "user", "content": f"（點擊了按鈕：{option}）"})

            new_req = Request(new_messages, interaction, is_proactive=True)
            new_req.target_user_id = interaction.user.id
            new_req.target_user_name = interaction.user.display_name
            new_req.target_channel_id = interaction.channel.id if interaction.channel else None
            new_req.target_channel_name = getattr(interaction.channel, "name", str(getattr(interaction.channel, "id", "")))
            new_req.target_guild_id = interaction.guild.id if interaction.guild else None
            new_req.target_guild_name = interaction.guild.name if interaction.guild else ""
            new_req.trigger_message_id = interaction.message.id if interaction.message else None
            new_req.attention_reason = "互動按鈕回覆"
            new_req.original_message = f"互動按鈕選擇：{option}"
            await self.scheduler.add_request(new_req)

        return callback
