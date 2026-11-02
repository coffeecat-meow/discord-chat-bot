"""Transient, fixed-lifetime panels; one accepted choice per member."""

import asyncio
from copy import deepcopy
import logging
import hashlib
from time import monotonic

import discord

logger = logging.getLogger(__name__)


class ChoiceView(discord.ui.View):
    def __init__(self, bot, options, title, *, context=None, source_text=""):
        super().__init__(timeout=600)
        self.bot, self.title = bot, title
        self.context = deepcopy(context or {})
        self.source_hash = hashlib.sha256(source_text.encode()).hexdigest()
        self.choices = {}
        self.message = None
        self.expires_at = None
        self.expired = False
        self._deadline_handle = None
        for label in options:
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

            async def callback(interaction, choice=label):
                await self.choose(interaction, choice)

            button.callback = callback
            self.add_item(button)

    def bind(self, message):
        self.message = message
        self.expires_at = monotonic() + 600
        self.bot.choice_views.add(self)
        # discord.py refreshes View.timeout on interaction. This independent
        # deadline is fixed to successful panel delivery, not the latest click.
        self._deadline_handle = asyncio.get_running_loop().call_later(
            600, lambda: asyncio.create_task(self.on_timeout())
        )

    def stop(self):
        if self._deadline_handle:
            self._deadline_handle.cancel()
            self._deadline_handle = None
        getattr(self.bot, "choice_views", set()).discard(self)
        super().stop()

    def active(self):
        return (
            not self.expired
            and not self.is_finished()
            and self.expires_at is not None
            and monotonic() < self.expires_at
        )

    async def on_timeout(self):
        if self.expired:
            return
        self.expired = True
        for button in self.children:
            button.disabled = True
        self.stop()
        if self.message:
            try:
                await asyncio.wait_for(self.message.edit(view=self), timeout=3)
            except (discord.HTTPException, asyncio.TimeoutError):
                logger.warning("Could not disable expired panel %s", self.message.id)

    async def choose(self, interaction, choice):
        if not self.active():
            await interaction.response.send_message("這個面板已結束，請重新開一個", ephemeral=True)
            return
        if (
            not interaction.guild
            or interaction.guild.id not in self.bot.runtime_guilds
            or str(interaction.guild.id) != self.context.get("guild_id")
            or str(interaction.channel_id) != self.context.get("channel_id")
            or getattr(interaction.message, "id", None) != self.message.id
        ):
            await interaction.response.send_message("這裡無法使用這個面板", ephemeral=True)
            return
        user_id = interaction.user.id
        if user_id in self.choices:
            notice = (
                "前次是否收下還無法確認，先不要重複送出"
                if self.choices[user_id] == "unknown"
                else "你已經選過或正在送出了喔"
            )
            await interaction.response.send_message(notice, ephemeral=True)
            return
        # No await between checking and claiming: parallel clicks cannot both enter.
        self.choices[user_id] = "pending"
        submitted = False
        try:
            await interaction.response.defer(ephemeral=True)
            if not self.active():
                self.choices.pop(user_id, None)
                await interaction.followup.send("這個面板已結束", ephemeral=True)
                return
            context = deepcopy(self.context)
            context.update(
                panel_message_id=f"msg_{self.message.id}",
                choice=choice,
                clicker={"id": str(user_id), "name": interaction.user.display_name},
            )
            submitted = True
            result = await self.bot.receive_interaction(
                interaction,
                f"在「{self.title}」選擇了：{choice}",
                panel_context=context,
                panel_view=self,
            )
        except BaseException as exc:
            if submitted:
                self.choices[user_id] = "unknown"
            else:
                self.choices.pop(user_id, None)
            if not isinstance(exc, Exception):
                raise
            logger.warning("Panel submission uncertain: %s", type(exc).__name__)
            if submitted:
                await interaction.followup.send(
                    "這次是否收下還無法確認，先不要重複送出", ephemeral=True
                )
            return
        status = result.get("status") if isinstance(result, dict) else "unknown"
        if status == "queued":
            self.choices[user_id] = "accepted"
            notice = "收到你的選擇了"
        elif status in {"sleeping", "queue_full", "rejected", "source_unavailable", "expired"}:
            self.choices.pop(user_id, None)
            notice = {
                "expired": "這個面板已結束，請重新開一個",
                "sleeping": "紗月在睡覺，這次還沒收下；醒來後可以再選",
                "queue_full": "目前忙不過來，這次還沒收下，請稍後再試",
                "rejected": "目前無法讀取這個頻道，這次沒有收下",
                "source_unavailable": "原訊息已變更或無法讀取，請重新開啟面板",
            }[status]
        else:
            self.choices[user_id] = "unknown"
            notice = "這次是否收下還無法確認，先不要重複送出"
        # A followup failure must never undo an already accepted queue entry.
        await interaction.followup.send(notice, ephemeral=True)
