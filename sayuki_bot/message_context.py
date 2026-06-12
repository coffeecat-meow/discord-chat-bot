from __future__ import annotations

from collections import deque
from datetime import datetime

import discord

from .config import TW_TZ
from .state import BotStats


async def get_cached_messages(message: discord.Message, channel_message_cache: dict[str, deque]) -> list[discord.Message]:
    channel_id = str(message.channel.id)

    if channel_id not in channel_message_cache:
        channel_message_cache[channel_id] = deque(maxlen=80)
        async for old_msg in message.channel.history(limit=80, oldest_first=True):
            channel_message_cache[channel_id].append(old_msg)
    else:
        channel_message_cache[channel_id].append(message)

    return list(channel_message_cache[channel_id])


def get_attention_flags(message: discord.Message, bot_user_id: int) -> tuple[bool, bool, bool]:
    is_mentioned = bot_user_id in [user.id for user in message.mentions]
    is_keyword = "紗月" in message.clean_content or "sayuki" in message.clean_content.lower()

    is_reply = False
    if message.reference and getattr(message.reference.resolved, "author", None):
        is_reply = message.reference.resolved.author.id == bot_user_id

    return is_mentioned, is_keyword, is_reply


def get_attachment_info(message: discord.Message) -> str:
    images = []
    videos = []
    audios = []
    others = []

    for attachment in message.attachments:
        if attachment.content_type:
            if attachment.content_type.startswith("image/"):
                images.append(attachment)
            elif attachment.content_type.startswith("video/"):
                videos.append(attachment)
            elif attachment.content_type.startswith("audio/"):
                audios.append(attachment)
            else:
                others.append(attachment)

    parts = []
    if len(images) == 1:
        parts.append("[有1張附件圖片]")
    elif len(images) > 1:
        parts.append(f"[有{len(images)}張附件圖片]")

    if len(videos) == 1:
        parts.append("[有1個附件影片]")
    elif len(videos) > 1:
        parts.append(f"[有{len(videos)}個附件影片]")

    if len(audios) == 1:
        parts.append("[有1個附件音訊]")
    elif len(audios) > 1:
        parts.append(f"[有{len(audios)}個附件音訊]")

    if len(others) == 1:
        parts.append("[有1個附件]")
    elif len(others) > 1:
        parts.append(f"[有{len(others)}個附件]")

    return " ".join(parts)


def should_echo_message(
    message: discord.Message,
    cached_msgs: list[discord.Message],
    bot_user_id: int,
    echoed_messages: dict[str, dict[str, datetime]],
    lookback_seconds: int,
    min_users: int,
) -> bool:
    channel_id = str(message.channel.id)
    msg_content = message.clean_content.strip()
    now_time = datetime.now(TW_TZ)

    if channel_id not in echoed_messages:
        echoed_messages[channel_id] = {}

    echoed_messages[channel_id] = {
        content: timestamp
        for content, timestamp in echoed_messages[channel_id].items()
        if (now_time - timestamp).total_seconds() <= lookback_seconds
    }

    msg_users: dict[str, set[int]] = {}
    for msg in cached_msgs:
        if (now_time - msg.created_at).total_seconds() <= lookback_seconds:
            if msg.author.bot or msg.author.id == bot_user_id:
                continue

            content = msg.clean_content.strip()
            if content and content not in msg_users:
                msg_users[content] = set()
            if content:
                msg_users[content].add(msg.author.id)

    if not msg_content or msg_content not in msg_users:
        return False

    total_users = len(msg_users[msg_content])
    if total_users < min_users:
        return False

    if msg_content in echoed_messages.get(channel_id, {}):
        return False

    echoed_messages[channel_id][msg_content] = now_time
    return True


def should_start_proactive(
    message: discord.Message,
    cached_msgs: list[discord.Message],
    last_time: datetime,
    cooldown_seconds: int,
) -> bool:
    now_time = datetime.now(TW_TZ)
    is_sleep_time = 2 <= now_time.hour < 7
    if is_sleep_time or (now_time - last_time).total_seconds() <= cooldown_seconds:
        return False

    recent_authors = set()
    utc_now = discord.utils.utcnow()

    for msg in reversed(cached_msgs[-15:]):
        if (utc_now - msg.created_at).total_seconds() <= 60:
            if not msg.author.bot:
                recent_authors.add(msg.author.id)
        else:
            break

    return len(recent_authors) >= 4


def build_chat_history(
    history_msgs: list[discord.Message],
    cached_msgs: list[discord.Message],
    current_message: discord.Message,
    bot_user_id: int,
    is_proactive: bool,
    vl_description_cache: dict[int, str],
) -> str:
    chat_history = ""

    for msg in history_msgs:
        msg_short_id = str(msg.id)[-4:]
        sender = "你(紗月)" if msg.author.id == bot_user_id else f"{msg.author.display_name} (ID:{msg.author.id})"
        content = msg.clean_content[:150].replace("\n", " ")

        reply_marker = ""
        if msg.reference and msg.reference.message_id:
            ref_id = msg.reference.message_id
            ref_short_id = str(ref_id)[-4:]
            replied_name = "某則較早的訊息"

            for cached_msg in cached_msgs:
                if cached_msg.id == ref_id:
                    replied_name = "你(紗月)" if cached_msg.author.id == bot_user_id else cached_msg.author.display_name
                    break

            reply_marker = f"[回覆 {replied_name} 的 #msg_{ref_short_id}] "

        if msg.id in vl_description_cache:
            attachment_info = f" [附帶圖片，系統回憶內容：{vl_description_cache[msg.id]}]"
        else:
            attachment_info = get_attachment_info(msg)
            if attachment_info:
                if any(
                    attachment.content_type and attachment.content_type.startswith("image/")
                    for attachment in msg.attachments
                ):
                    attachment_info += f" 可用 [[VIEW_IMAGE: #msg_{msg_short_id}]] 查看圖片內容"
                attachment_info = f" {attachment_info}"

        content = f"{reply_marker}{content}{attachment_info}"

        if msg.id == current_message.id:
            marker = "*(無任何人提及你，這是你主動查看)* " if is_proactive else "*(當前訊息)* "
        else:
            marker = ""

        tw_time = msg.created_at.astimezone(TW_TZ)
        chat_history += f"[{tw_time.strftime('%H:%M')}] {sender} (#msg_{msg_short_id}): {marker}{content}\n"

    return chat_history


def build_time_context(stats: BotStats) -> str:
    now_time = datetime.now(TW_TZ)
    time_str = now_time.strftime("%Y/%m/%d %H:%M")
    time_context = f"現在時間是 {time_str}。"

    if 0 <= now_time.hour < 5:
        time_context += " 現在是深夜，你可能會覺得有點睏。"
    elif 11 <= now_time.hour < 13:
        time_context += " 現在是中午午餐時間。"

    time_context += f" 另外，你今天已經在群組說了 {stats.today_messages} 句話，這輩子總共說了 {stats.total_messages} 句話。"
    if stats.today_messages > 50:
        time_context += " (今天似乎聊滿多的，要注意休息喔！)"

    return time_context


def build_system_context(
    user_name: str,
    user_id: int,
    attention_reason: str,
    memory_context: str,
    permanent_memory: str,
    server_memory: str,
    short_term_context: str,
    chat_history: str,
    stats: BotStats,
    is_proactive: bool,
) -> str:
    sys_info = (
        "[系統資訊]\n"
        f"{build_time_context(stats)}\n"
        f"目前對話者: {user_name} (Discord ID:{user_id})\n"
        f"本次觸發原因: {attention_reason}\n"
        "提醒：user_name 是顯示名稱，工具標記需要使用純數字 Discord ID。\n\n"
        "提醒：Discord頻道標記 `<#頻道ID>`、Discord訊息連結會在「Discord標記解析」區塊轉成可讀資訊；"
        "若訊息連結解析成功，可使用該區塊提供的 #msg_xxxx 指定回覆或查看圖片。\n\n"
        "提醒：記憶區只會完整附上目前對話相關的人，其他人只列索引；"
        "如果索引中的某人和當前話題明顯有關，可使用 [[LOOKUP_MEMORY: DiscordID]] 查完整記憶。\n\n"
        f"[相關使用者記憶與索引]\n{memory_context}\n\n"
        f"[你的永久記憶]\n{permanent_memory}\n\n"
        f"[目前伺服器記憶]\n{server_memory}\n\n"
        f"[短期上下文摘要]\n{short_term_context}\n\n"
        f"[近期群組對話]\n{chat_history}"
    )

    if is_proactive:
        sys_info += (
            "\n\n【情境提示】\n"
            "你目前是主動查看頻道，無任何人提及你，只是有多個人在討論。\n"
            "請注意：大部分時候你不需要回覆。若你認為當下話題不需要你參與、不確定該說什麼，"
            "或他們只是在互相閒聊，請務必果斷地只輸出 [[$NO_NEED_TO_ANSWER$]] 以保持安靜。"
        )

    return sys_info
