from __future__ import annotations

import inspect
from collections import deque
from datetime import datetime

import discord

from .config import TW_TZ
from .state import BotStats


MAX_POLL_VOTERS_PER_ANSWER = 25
MAX_EMBED_FIELDS = 6
MAX_COMPONENT_LABELS = 16
MAX_DISCORD_COMPONENT_CONTEXT_CHARS = 2200


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


def _clean_inline(value, limit: int = 160) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _call_if_noarg(value):
    if not callable(value):
        return value

    try:
        return value()
    except TypeError:
        return value


def _user_label(user) -> str:
    name = (
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", None)
        or str(user)
    )
    return _clean_inline(name, 40)


def _message_author_label(message: discord.Message, bot_user_id: int | None = None) -> str:
    if bot_user_id and message.author.id == bot_user_id:
        return "你(紗月)"
    return f"{message.author.display_name} (ID:{message.author.id})"


def _poll_text(value) -> str:
    if value is None:
        return ""

    text = getattr(value, "text", None)
    emoji = getattr(value, "emoji", None)
    media = getattr(value, "media", None)
    if text is None and media is not None:
        text = getattr(media, "text", None)
        emoji = emoji or getattr(media, "emoji", None)

    if text is None:
        text = value if isinstance(value, str) else str(value)

    if emoji:
        return f"{emoji} {_clean_inline(text, 120)}".strip()
    return _clean_inline(text, 120)


def _poll_answers(poll) -> list:
    answers = getattr(poll, "answers", [])
    answers = _call_if_noarg(answers)
    if isinstance(answers, dict):
        return list(answers.values())
    return list(answers or [])


def _poll_answer_count(answer) -> int:
    for attr in ("vote_count", "votes", "count"):
        value = getattr(answer, attr, None)
        value = _call_if_noarg(value)
        if isinstance(value, int):
            return value
    return 0


async def _collect_poll_voters(answer) -> tuple[list[str], bool]:
    voters_source = getattr(answer, "voters", None) or getattr(answer, "fetch_voters", None)
    if not voters_source:
        return [], False

    try:
        if callable(voters_source):
            try:
                voters_result = voters_source(limit=MAX_POLL_VOTERS_PER_ANSWER)
            except TypeError:
                voters_result = voters_source()
        else:
            voters_result = voters_source
    except Exception:
        return [], True

    try:
        if inspect.isawaitable(voters_result):
            voters_result = await voters_result

        names = []
        if hasattr(voters_result, "__aiter__"):
            async for voter in voters_result:
                names.append(_user_label(voter))
                if len(names) >= MAX_POLL_VOTERS_PER_ANSWER:
                    break
        else:
            for voter in list(voters_result or [])[:MAX_POLL_VOTERS_PER_ANSWER]:
                names.append(_user_label(voter))
        return names, True
    except Exception:
        return [], True


async def _format_poll_context(message: discord.Message, creator_label: str) -> str:
    poll = getattr(message, "poll", None)
    if not poll:
        return ""

    question = _poll_text(getattr(poll, "question", None)) or "未命名投票"
    total_votes = getattr(poll, "total_votes", None)
    total_votes = _call_if_noarg(total_votes)
    answers = _poll_answers(poll)
    if not isinstance(total_votes, int):
        total_votes = sum(_poll_answer_count(answer) for answer in answers)

    multiple = getattr(poll, "multiple", None)
    multiple = _call_if_noarg(multiple)
    multiple_text = "複選" if multiple else "單選"

    expires_at = getattr(poll, "expires_at", None)
    expires_at = _call_if_noarg(expires_at)
    finalized = getattr(poll, "is_finalised", None) or getattr(poll, "is_finalized", None)
    finalized = bool(_call_if_noarg(finalized)) if finalized is not None else False
    status = "已結束" if finalized else "進行中"
    if expires_at:
        expires_tw = expires_at.astimezone(TW_TZ)
        status = f"{status}，結束時間：{expires_tw.strftime('%Y/%m/%d %H:%M')}"

    lines = [
        f"[Discord投票｜建立者：{creator_label}]",
        f"題目：{question}",
        f"狀態：{status}，類型：{multiple_text}，總票數：{total_votes}",
        "選項：",
    ]

    for answer in answers[:10]:
        label = _poll_text(answer) or "未命名選項"
        count = _poll_answer_count(answer)
        percent = round((count / total_votes) * 100) if total_votes else 0
        voters, attempted = await _collect_poll_voters(answer)
        voter_suffix = ""
        if voters:
            more = "..." if count > len(voters) else ""
            voter_suffix = f"（{', '.join(voters)}{more}）"
        elif attempted and count:
            voter_suffix = "（投票者讀取失敗或權限不足）"
        lines.append(f"- {label}：{count}票 {percent}%{voter_suffix}")

    if len(answers) > 10:
        lines.append(f"- 另有{len(answers) - 10}個選項未列出")

    return "\n".join(lines)


def _component_label(component) -> str:
    label = getattr(component, "label", None)
    emoji = getattr(component, "emoji", None)
    placeholder = getattr(component, "placeholder", None)
    custom_id = getattr(component, "custom_id", None)
    url = getattr(component, "url", None)
    disabled = getattr(component, "disabled", False)

    options = getattr(component, "options", None) or []
    if options:
        option_labels = [
            _clean_inline(getattr(option, "label", None) or getattr(option, "value", None), 40)
            for option in options[:8]
        ]
        text = f"選單:{_clean_inline(placeholder or custom_id or '未命名選單', 60)}（選項:{', '.join(option_labels)}）"
    else:
        text = _clean_inline(label or placeholder or custom_id or url or "", 80)
        if emoji:
            text = f"{emoji} {text}".strip()
        if url:
            text = f"{text} (連結按鈕)".strip()

    if disabled and text:
        text += "（已停用）"
    return text


def _format_components(components) -> str:
    labels = []
    for row in components or []:
        children = getattr(row, "children", None) or getattr(row, "components", None) or [row]
        for child in children:
            label = _component_label(child)
            if label:
                labels.append(label)
            if len(labels) >= MAX_COMPONENT_LABELS:
                break
        if len(labels) >= MAX_COMPONENT_LABELS:
            break

    if not labels:
        return ""

    suffix = "..." if len(labels) >= MAX_COMPONENT_LABELS else ""
    return f"按鈕/選單：{', '.join(labels)}{suffix}"


def _format_embeds_context(message: discord.Message, creator_label: str, components_text: str) -> str:
    embeds = list(getattr(message, "embeds", []) or [])
    if not embeds and not components_text:
        return ""

    heading = "[Embed面板｜建立者：{}]".format(creator_label) if embeds else "[Discord元件面板｜建立者：{}]".format(creator_label)
    lines = [heading]

    for index, embed in enumerate(embeds[:2], start=1):
        title = _clean_inline(getattr(embed, "title", "") or "", 180)
        description = _clean_inline(getattr(embed, "description", "") or "", 500)
        author = getattr(embed, "author", None)
        author_name = _clean_inline(getattr(author, "name", "") or "", 80)
        footer = getattr(embed, "footer", None)
        footer_text = _clean_inline(getattr(footer, "text", "") or "", 120)

        prefix = f"Embed{index}" if len(embeds) > 1 else "Embed"
        if title:
            lines.append(f"{prefix}標題：{title}")
        if author_name:
            lines.append(f"{prefix}作者欄：{author_name}")
        if description:
            lines.append(f"{prefix}描述：{description}")

        fields = list(getattr(embed, "fields", []) or [])
        for field in fields[:MAX_EMBED_FIELDS]:
            field_name = _clean_inline(getattr(field, "name", "") or "", 80)
            field_value = _clean_inline(getattr(field, "value", "") or "", 180)
            if field_name or field_value:
                lines.append(f"欄位：{field_name}={field_value}")
        if len(fields) > MAX_EMBED_FIELDS:
            lines.append(f"另有{len(fields) - MAX_EMBED_FIELDS}個欄位未列出")
        if footer_text:
            lines.append(f"{prefix}頁腳：{footer_text}")

    if len(embeds) > 2:
        lines.append(f"另有{len(embeds) - 2}個Embed未列出")
    if components_text:
        lines.append(components_text)

    return "\n".join(lines)


def _format_thread_context(
    message: discord.Message,
    creator_label: str,
    user_labels: dict[int, str],
) -> str:
    thread = getattr(message, "thread", None)
    if not thread:
        return ""

    owner_id = getattr(thread, "owner_id", None)
    thread_creator = user_labels.get(owner_id, f"ID:{owner_id}") if owner_id else creator_label
    name = _clean_inline(getattr(thread, "name", "") or "未命名討論串", 100)
    thread_id = getattr(thread, "id", "")
    message_count = getattr(thread, "message_count", None)
    member_count = getattr(thread, "member_count", None)
    archived = getattr(thread, "archived", None)

    details = [f"名稱：{name}"]
    if thread_id:
        details.append(f"ID:{thread_id}")
    if isinstance(message_count, int):
        details.append(f"訊息數:{message_count}")
    if isinstance(member_count, int):
        details.append(f"成員數:{member_count}")
    if archived is not None:
        details.append("已封存" if archived else "未封存")

    return f"[討論串｜建立者：{thread_creator}]\n" + "，".join(details)


async def build_discord_component_context(
    message: discord.Message,
    creator_label: str,
    user_labels: dict[int, str] | None = None,
) -> str:
    user_labels = user_labels or {}
    blocks = []

    poll_context = await _format_poll_context(message, creator_label)
    if poll_context:
        blocks.append(poll_context)

    components_text = _format_components(getattr(message, "components", []) or [])
    embed_context = _format_embeds_context(message, creator_label, components_text)
    if embed_context:
        blocks.append(embed_context)

    thread_context = _format_thread_context(message, creator_label, user_labels)
    if thread_context:
        blocks.append(thread_context)

    context = "\n".join(blocks)
    if len(context) > MAX_DISCORD_COMPONENT_CONTEXT_CHARS:
        return context[:MAX_DISCORD_COMPONENT_CONTEXT_CHARS] + "\n...（Discord元件內容過長，已截斷）"
    return context


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


async def build_chat_history(
    history_msgs: list[discord.Message],
    cached_msgs: list[discord.Message],
    current_message: discord.Message,
    bot_user_id: int,
    is_proactive: bool,
    vl_description_cache: dict[int, str],
) -> str:
    chat_history = ""
    user_labels = {
        msg.author.id: _message_author_label(msg, bot_user_id)
        for msg in [*cached_msgs, *history_msgs]
    }

    for msg in history_msgs:
        msg_short_id = str(msg.id)[-4:]
        sender = _message_author_label(msg, bot_user_id)
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
        component_context = await build_discord_component_context(msg, sender, user_labels)
        if component_context:
            content = f"{content}\n{component_context}".strip()

        if msg.id == current_message.id:
            marker = "*(無任何人提及你，這是你主動查看)* " if is_proactive else "*(當前訊息)* "
        else:
            marker = ""

        tw_time = msg.created_at.astimezone(TW_TZ)
        content = content.replace("\n", "\n    ")
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
    presence_context: str,
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
        f"[目前相關使用者Discord狀態]\n{presence_context}\n\n"
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
