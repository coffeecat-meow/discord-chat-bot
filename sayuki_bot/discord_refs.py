from __future__ import annotations

import re
from dataclasses import dataclass, field

import discord

from .config import TW_TZ


CHANNEL_MENTION_RE = re.compile(r"<#(\d+)>")
MESSAGE_LINK_RE = re.compile(
    r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/(@me|\d+)/(\d+)/(\d+)"
)


@dataclass
class DiscordReferenceInfo:
    context: str = ""
    reply_targets: dict[str, discord.Message] = field(default_factory=dict)
    image_targets: dict[str, list[str]] = field(default_factory=dict)
    image_target_message_ids: dict[str, int] = field(default_factory=dict)


def _message_key(message_id: int) -> str:
    return f"msg_{str(message_id)[-4:]}"


def _attachment_summary(message: discord.Message) -> str:
    images = sum(1 for item in message.attachments if item.content_type and item.content_type.startswith("image/"))
    videos = sum(1 for item in message.attachments if item.content_type and item.content_type.startswith("video/"))
    audios = sum(1 for item in message.attachments if item.content_type and item.content_type.startswith("audio/"))
    others = max(len(message.attachments) - images - videos - audios, 0)

    parts = []
    if images:
        parts.append(f"{images}張圖片")
    if videos:
        parts.append(f"{videos}個影片")
    if audios:
        parts.append(f"{audios}個音訊")
    if others:
        parts.append(f"{others}個其他附件")

    return "、".join(parts) if parts else "無附件"


def _collect_image_targets(message: discord.Message, info: DiscordReferenceInfo) -> None:
    urls = [
        attachment.url
        for attachment in message.attachments
        if attachment.content_type and attachment.content_type.startswith("image/")
    ]
    if urls:
        key = _message_key(message.id)
        info.image_targets[key] = urls
        info.image_target_message_ids[key] = message.id


async def _resolve_channel(bot: discord.Client, guild: discord.Guild | None, channel_id: int):
    if guild:
        channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        if channel:
            return channel

    channel = bot.get_channel(channel_id)
    if channel:
        return channel

    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None


def _format_channel(channel_id: int, channel) -> str:
    if not channel:
        return f"- <#{channel_id}>：Discord頻道標記，ID:{channel_id}，但目前查不到頻道名稱或沒有讀取權限"

    name = getattr(channel, "name", str(channel_id))
    guild_name = getattr(getattr(channel, "guild", None), "name", "")
    topic = getattr(channel, "topic", "") or getattr(channel, "description", "") or ""
    kind = channel.__class__.__name__
    line = f"- <#{channel_id}>：Discord頻道標記，頻道 #{name} (ID:{channel_id})"
    if guild_name:
        line += f"，伺服器：{guild_name}"
    line += f"，類型：{kind}"
    if topic:
        line += f"，說明：{str(topic)[:220]}"
    return line


def _format_message_link(url: str, channel, message: discord.Message) -> str:
    key = _message_key(message.id)
    channel_name = getattr(channel, "name", str(getattr(channel, "id", "")))
    guild_name = getattr(getattr(channel, "guild", None), "name", "")
    created_at = message.created_at.astimezone(TW_TZ).strftime("%Y/%m/%d %H:%M")
    content = (message.clean_content or message.content or "").replace("\n", " ").strip()
    if not content:
        content = "（無文字內容）"

    line = (
        f"- {url}：Discord訊息連結，可在本次回覆中用 [[REPLY_TO: #{key}]] 指定回覆。"
        f" 訊息在 #{channel_name}"
    )
    if guild_name:
        line += f" / {guild_name}"
    line += (
        f"，訊息ID:{message.id}，作者：{message.author.display_name} (ID:{message.author.id})，"
        f"時間：{created_at}，內容：{content[:600]}，附件：{_attachment_summary(message)}"
    )

    if any(item.content_type and item.content_type.startswith("image/") for item in message.attachments):
        line += f"，可用 [[VIEW_IMAGE: #{key}]] 查看圖片"

    return line


async def resolve_discord_references(
    bot: discord.Client,
    source_message: discord.Message,
    text: str,
    *,
    max_message_links: int = 5,
    max_channel_mentions: int = 8,
) -> DiscordReferenceInfo:
    info = DiscordReferenceInfo()
    if not text:
        return info

    guild = getattr(source_message, "guild", None)
    lines: list[str] = []

    channel_ids = []
    seen_channel_ids: set[int] = set()
    for raw_channel_id in CHANNEL_MENTION_RE.findall(text):
        channel_id = int(raw_channel_id)
        if channel_id in seen_channel_ids:
            continue
        seen_channel_ids.add(channel_id)
        channel_ids.append(channel_id)
        if len(channel_ids) >= max_channel_mentions:
            break

    if channel_ids:
        lines.append("【Discord頻道標記解析】")
        for channel_id in channel_ids:
            channel = await _resolve_channel(bot, guild, channel_id)
            lines.append(_format_channel(channel_id, channel))

    link_matches = []
    seen_message_ids: set[int] = set()
    for match in MESSAGE_LINK_RE.finditer(text):
        guild_id_text, channel_id_text, message_id_text = match.groups()
        message_id = int(message_id_text)
        if message_id in seen_message_ids:
            continue
        seen_message_ids.add(message_id)
        link_matches.append((match.group(0), guild_id_text, int(channel_id_text), message_id))
        if len(link_matches) >= max_message_links:
            break

    if link_matches:
        lines.append("【Discord訊息連結解析】")
        for url, guild_id_text, channel_id, message_id in link_matches:
            if guild_id_text.isdigit() and guild and int(guild_id_text) != guild.id:
                lines.append(f"- {url}：Discord訊息連結，位於其他伺服器或不同guild，可能無法讀取")

            channel = await _resolve_channel(bot, guild, channel_id)
            if not channel or not hasattr(channel, "fetch_message"):
                lines.append(f"- {url}：Discord訊息連結，但查不到頻道或沒有讀取權限")
                continue

            try:
                linked_message = await channel.fetch_message(message_id)
            except Exception as exc:
                lines.append(f"- {url}：Discord訊息連結，但讀取訊息失敗：{exc}")
                continue

            key = _message_key(linked_message.id)
            info.reply_targets[key] = linked_message
            _collect_image_targets(linked_message, info)
            lines.append(_format_message_link(url, channel, linked_message))

    info.context = "\n".join(lines).strip()
    return info
