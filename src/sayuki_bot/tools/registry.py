"""The source of tool names, arguments, help text and dispatch metadata."""

from __future__ import annotations

from dataclasses import dataclass
import math
import re

MEMORY_IDENTITY_POLICY = (
    "記憶對象須有明確@或可辨識帳號的訊息依據；只有綽號且無法對應時先不寫，"
    "等身分明確再記，不猜ID、不把轉述寫到說話者身上。寫入成功後才說已記住。"
)


@dataclass(frozen=True)
class Tool:
    name: str
    arguments: tuple[str, ...]
    description: str
    group: str
    action: bool = False
    common: bool = False
    optional: tuple[str, ...] = ()
    defaults: tuple[tuple[str, str], ...] = ()

    def parse(self, body):
        if len(body) > 12000:
            raise ValueError("Tool arguments exceed 12000 characters")
        if not self.arguments:
            return {}
        values = body.split("|", len(self.arguments) - 1)
        if self.name == "MEM_UPDATE":
            # Preserve pipes inside values; the final msg_ID (or empty optional
            # field) selects a source for both profiles and events.
            values = body.split("|", 3)
            if len(values) == 4 and values[1].strip() in {"profile", "event"}:
                value, separator, source = values[3].rpartition("|")
                if separator and (source.strip().startswith("msg_") or not source.strip()):
                    values = [*values[:3], value, source]
        defaults = dict(self.defaults)
        for key in self.arguments[len(values) :]:
            if key not in defaults:
                break
            values.append(defaults[key])
        values = [v.strip() or defaults.get(key, "") for key, v in zip(self.arguments, values)]
        if len(values) != len(self.arguments) or any(
            not v.strip() and key not in self.optional for key, v in zip(self.arguments, values)
        ):
            raise ValueError(f"{self.name}: expected {' | '.join(self.arguments)}")
        arguments = dict(zip(self.arguments, (v.strip() for v in values)))
        if self.name == "READ_MESSAGE" and not re.fullmatch(
            r"#?msg_\d{1,20}|https://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/\d+/\d+/\d+",
            arguments["url"],
        ):
            raise ValueError(
                "READ_MESSAGE 需要完整 Discord 訊息網址或已知來源的msg_ID，請使用 MEMORY_SEARCH 結果的 url；尚未嘗試讀取，這不是權限或訊息不存在的結果。"
            )
        if self.name in {"PERMANENT_MEMORY", "EDIT_PERMANENT_MEMORY"}:
            if arguments["category"] not in {"preference", "conversation"}:
                raise ValueError("自身分類只接受preference／conversation，固定核心不能改寫")
            sources = list(dict.fromkeys(x.strip() for x in arguments["source_ids"].split(",")))
            if not 1 <= len(sources) <= 4 or any(not re.fullmatch(r"msg_[1-9][0-9]*", x) for x in sources):
                raise ValueError("自身來源須為1–4個msg_ID")
            arguments["source_ids"] = ",".join(sources)
        if self.name in {"EDIT_PERMANENT_MEMORY", "DELETE_PERMANENT_MEMORY"}:
            if not re.fullmatch(r"self:[1-9][0-9]*", arguments["entry_id"]):
                raise ValueError("自身條目須用self:ID")
        if (
            self.name == "MEM_UPDATE"
            and arguments["kind"] == "event"
            and not arguments["source_id"]
        ):
            raise ValueError(
                "event必須在尾端source_id填來源msg_ID；不能只寫在摘要內，也不會默認綁到當前訊息。"
            )
        if self.name == "MEM_UPDATE" and arguments["source_id"]:
            if not re.fullmatch(r"msg_[1-9][0-9]*", arguments["source_id"]):
                raise ValueError("source_id must be msg_ID")
        for key, value in arguments.items():
            if key in {"user_id", "channel_id", "sticker_id", "memory_id"} and (
                not value.isdigit() or int(value) <= 0
            ) and not (self.name == "SEND_STICKER" and key == "sticker_id"):  # name lookup allowed
                if key == "user_id" and self.name in {"MEM_UPDATE", "MEM_OBSERVE"}:
                    raise ValueError("user_id 必須是已確認的數字帳號 ID。" + MEMORY_IDENTITY_POLICY)
                raise ValueError(f"{key} must be a numeric Discord ID")
            if key == "message_id" and not re.fullmatch(r"#?(?:msg_)?[0-9]+", value):
                raise ValueError("Use a full msg_ID")
            if key == "seconds" and (not value.isdigit() or not 1 <= int(value) <= 300):
                raise ValueError("Timeout must be 1–300 seconds")
            if key == "minutes":
                number = float(value)
                maximum = 10080 if self.name == "POLL" else 525600
                if not math.isfinite(number) or not 0 < number <= maximum:
                    raise ValueError("Duration out of range")
            if key in {"content", "value"} and len(value) > 2000:
                raise ValueError(f"{key} exceeds 2000 characters")
            if key == "key" and len(value) > 100:
                raise ValueError("Memory key exceeds 100 characters")
            if key == "nickname" and len(value) > 32:
                raise ValueError("Nickname exceeds 32 characters")
        if "limit" in arguments:
            maximum = 20 if self.name == "CHANNEL_LOOKUP" else 30
            limit = arguments["limit"]
            if not re.fullmatch(r"[0-9]+", limit) or not 1 <= int(limit) <= maximum:
                raise ValueError(f"limit must be 1–{maximum}")
            arguments["limit"] = str(int(limit))
            if "before_message_id" in arguments:
                cursor = arguments["before_message_id"]
                if cursor and not re.fullmatch(r"#?(?:msg_)?[1-9][0-9]*", cursor):
                    raise ValueError("before_message_id must be a positive message ID")
                arguments["before_message_id"] = re.sub(r"^#?(?:msg_)?", "", cursor)
            if "types" in arguments:
                allowed = (
                    {"text", "thread", "forum", "media", "voice", "stage", "category"}
                    if self.name == "CHANNEL_LOOKUP"
                    else {"emoji", "sticker", "command"}
                )
                types = {x.strip().lower() for x in arguments["types"].split(",")} - {""}
                if not types <= allowed:
                    raise ValueError("types must be comma-separated: " + ", ".join(sorted(allowed)))
                arguments["types"] = ",".join(sorted(types))
        if self.name == "VIEW_STICKER" and arguments["source_message_id"]:
            if not re.fullmatch(r"#?(?:msg_)?[1-9][0-9]*", arguments["source_message_id"]):
                raise ValueError("source_message_id must be a full msg_ID")
            arguments["source_message_id"] = "msg_" + re.sub(
                r"^#?(?:msg_)?", "", arguments["source_message_id"]
            )
        if self.name == "BUTTON_UI":
            labels = [x.strip() for x in re.split("[,，]", arguments["options"]) if x.strip()]
            if (
                not 1 <= len(labels) <= 25
                or len(set(labels)) != len(labels)
                or any(len(x) > 80 for x in labels)
            ):
                raise ValueError("Panel needs 1–25 unique labels, at most 80 characters each")
            recipient = arguments["recipient_id"]
            if recipient and not re.fullmatch(r"[1-9][0-9]*", recipient):
                raise ValueError("recipient_id must be a confirmed numeric member ID, or empty")
        if self.name == "POLL":
            minutes = float(arguments["minutes"])
            if minutes < 60 or minutes % 60:
                raise ValueError(
                    "Discord polls require whole hours: minutes must be a multiple of 60 (60–10080)"
                )
            options = [x.strip() for x in re.split("[,，]", arguments["options"]) if x.strip()]
            if not 2 <= len(options) <= 10:
                raise ValueError("Poll needs 2–10 options")
            if arguments["multiple"].lower() not in {"true", "false"}:
                raise ValueError("multiple must be true or false")
        if self.name in {"MEM_UPDATE", "MEM_DELETE"} and arguments["kind"] not in {
            "profile",
            "event",
        }:
            raise ValueError("Memory kind must be profile or event")
        if self.name == "MEM_OBSERVE":
            if arguments["basis"] not in {"reported", "inferred"}:
                raise ValueError("Observation basis must be reported or inferred")
            ids = arguments["source_ids"].split(",")
            if not 1 <= len(ids) <= 8 or any(
                not re.fullmatch(r"(?:msg_)?[1-9][0-9]*", x.strip()) for x in ids
            ):
                raise ValueError("Use 1–8 comma-separated source message IDs")
        if self.name == "MEM_REVISE" and arguments["relation"] not in {
            "replace",
            "context",
            "delete",
        }:
            raise ValueError("Profile relation must be replace, context or delete")
        return arguments

    def help(self):
        suffix = (
            (
                "（尾端參數可省略："
                + "、".join(f"{key}={value or '留空'}" for key, value in self.defaults)
                + "）"
            )
            if self.defaults
            else ""
        )
        return f"[[{self.name}: {' | '.join(self.arguments)}]]：{self.description}{suffix}"


TOOLS = [
    Tool(
        "TOOL_HELP",
        ("工具名稱或分類",),
        "取得低頻工具的完整用法；分類 discord、web、memory、math、social",
        "discovery",
        common=True,
    ),
    Tool("SEARCH", ("query",), "搜尋網路", "web", common=True),
    Tool("READ_WEB", ("url",), "讀取指定網址", "web"),
    Tool(
        "VIEW_IMAGE",
        ("message_id",),
        "查看已提供的訊息圖片，使用完整msg_ID；動圖只看有限取樣，不代表完整動畫",
        "discord",
        common=True,
    ),
    Tool(
        "VIEW_STICKER",
        ("sticker_id", "source_message_id"),
        "查看本群或可讀訊息的貼圖，來源在歷史外時填完整msg_ID（先READ_MESSAGE查原文）；動態貼圖只看有限取樣，Lottie只看預覽。缺圖不猜內容",
        "discord",
        optional=("source_message_id",),
        defaults=(("source_message_id", ""),),
    ),
    Tool(
        "SEND_STICKER",
        ("sticker_id",),
        "發送本群可用貼圖，填ID或貼圖名稱（可先用GUILD_ASSET_SEARCH查）；表情符號不是貼圖，不用此工具；獨立發一則，成功前不要說已發送",
        "social",
        True,
    ),
    Tool(
        "CHANNEL_LOOKUP",
        ("query", "types", "limit"),
        "按名稱、ID或主題尋找可共享且可讀頻道，query可空；types可空或text,thread,forum,media,voice,stage,category；最多20筆",
        "discord",
        optional=("query", "types"),
        defaults=(("types", ""), ("limit", "20")),
    ),
    Tool(
        "READ_CHANNEL",
        ("channel_id", "before_message_id", "limit"),
        "讀取可共享頻道訊息，由新到舊；before_message_id空為最新，下一頁沿用next_before_message_id；每頁1–30則且有字元上限，truncated表示內容縮短；附件清單不代表看過圖片，需READ_MESSAGE查原文後VIEW_IMAGE",
        "discord",
        optional=("before_message_id",),
        defaults=(("before_message_id", ""), ("limit", "25")),
    ),
    Tool("READ_MESSAGE", ("url",), "查證Discord原始訊息；填完整連結或已知來源msg_ID，不猜頻道", "discord", common=True),
    Tool(
        "GUILD_ASSET_SEARCH",
        ("query", "types", "limit"),
        "尋找本群表情、貼圖和本Bot指令，按名稱或ID查找已註冊子指令；query可空；types可空或emoji,sticker,command；最多30筆，不代表其他Bot指令。表情符號直接把結果的text（<:名稱:ID>）寫進回覆就會顯示，貼圖才用SEND_STICKER",
        "discord",
        optional=("query", "types"),
        defaults=(("types", ""), ("limit", "30")),
    ),
    Tool("INSPECT_POLL", ("message_id",), "查看投票選項和票數", "discord"),
    Tool("CHECK_ROLES", ("user_id",), "查詢成員身分組", "discord"),
    Tool("CHECK_PRESENCE", ("user_id",), "查詢快取在線狀態和活動", "discord"),
    Tool("USER_STATS", ("user_id",), "查詢已儲存的互動統計", "memory"),
    Tool(
        "MEMORY_SEARCH",
        ("query",),
        "查找相關記憶與對話索引；原文需READ_MESSAGE查證",
        "memory",
        common=True,
    ),
    Tool(
        "MEM_UPDATE",
        ("user_id", "kind", "key", "value", "source_id"),
        "本人明示穩定資訊用profile，暫時近況用event；勿從一次近況推成習慣。轉述／推測用MEM_OBSERVE（可查用法），更正用MEM_REVISE。"
        "自己的偏好／經歷用PERMANENT_MEMORY，先查格式；此工具只寫群友。"
        "event的key/value只作檢索詞，回覆依來源原文；event必填尾端source_id（msg_ID），不可只寫在摘要內；"
        "profile可省略（沿用當前／reply），指定時綁該原文及其reply依據，不以本輪代替；較早原文不能覆蓋本人新更正。發話時間不等於事件開始時間。"
        + MEMORY_IDENTITY_POLICY,
        "memory",
        True,
        True,
        optional=("source_id",),
        defaults=(("source_id", ""),),
    ),
    Tool("MEM_DELETE", ("user_id", "kind", "key"), "移除本頻道指定記憶條目", "memory", True),
    Tool(
        "MEM_OBSERVE",
        ("user_id", "basis", "source_ids", "key", "value"),
        "保留有依據的長期印象：reported是他人轉述，inferred是自己的推測（含對傳聞可信度的推測）；"
        "source_ids填已知可讀訊息ID，以逗號分隔最多8則。保留時間、情境與不確定性；"
        "一次近況不推成習慣，重複觀察也不等於本人確認。可更新同key的印象，不能覆蓋本人陳述；本人更正用MEM_REVISE。"
        + MEMORY_IDENTITY_POLICY,
        "memory",
        True,
    ),
    Tool(
        "MEM_REVISE",
        ("memory_id", "relation", "context", "value"),
        "本人更正可讀畫像id：replace更正原情境（context留空），context補充不同情境並保留原偏好（填情境），delete撤回（context/value留空）；僅依本人明確表達",
        "memory",
        True,
        optional=("context", "value"),
    ),
    Tool("LOOKUP_MEMORY", ("user_id",), "依人物帳號ID查記憶", "memory"),
    Tool("REACT", ("emoji",), "對目前訊息加表情反應，emoji填Unicode表情、本群表情名稱或<:名稱:ID>", "social", True, True),
    Tool("PING", ("user_id",), "在文字中明確提及；平常直接稱呼", "presentation", common=True),
    Tool(
        "REPLY_TO",
        ("message_id",),
        "只指定緊接的一段回覆，後續通常直接發頻道",
        "presentation",
        common=True,
    ),
    Tool("REMIND", ("minutes", "content"), "設定稍後提醒", "social", True, True),
    Tool("STATUS", ("text",), "更新動態狀態，30分鐘後清空", "social", True),
    Tool("MUTE", ("user_id", "seconds"), "任性禁言，上限300秒，不能對Bot或群主", "social", True),
    Tool("DM_USER", ("user_id", "content"), "私訊目前群組成員", "social", True),
    Tool(
        "THREAD",
        ("name", "content"),
        "建立討論串，首則文字可留空",
        "social",
        True,
        optional=("content",),
    ),
    Tool("SEND_CHANNEL_MESSAGE", ("channel_id", "content"), "發送到允許的其他頻道", "social", True),
    Tool(
        "NICKNAME",
        ("user_id", "nickname"),
        "修改成員暱稱，留空清除",
        "social",
        True,
        optional=("nickname",),
    ),
    Tool(
        "SERVER_EVENT",
        ("name", "start", "end", "location", "description"),
        "建立活動，日期使用ISO格式",
        "social",
        True,
    ),
    Tool(
        "BUTTON_UI",
        ("title", "description", "options", "recipient_id"),
        "開選擇面板，全群每人一次；固定10分鐘，重啟不恢復。選項逗號分隔；recipient_id只填明確指定的群友ID，未指定留空，不猜；點擊會帶原提問者、面板對象與點擊者接續對話",
        "social",
        True,
        optional=("recipient_id",),
        defaults=(("recipient_id", ""),),
    ),
    Tool(
        "POLL",
        ("title", "minutes", "multiple", "options"),
        "建立原生投票，minutes為有效期限且須為60的倍數（60–10080）；multiple為true或false，選項逗號分隔；未結束前可改票，不是BUTTON_UI的每人一次",
        "social",
        True,
    ),
    Tool("MATH_CALC", ("expression",), "計算數學算式", "math"),
    Tool("MATH_PLOT", ("expression",), "繪製函數", "math", True),
    Tool("MATH_LATEX", ("expression",), "渲染LaTeX圖片", "math", True),
    Tool("TEXT_TRANSFORM", ("operation", "text"), "編碼、解碼或雜湊文字", "math"),
    Tool("PERMANENT_MEMORY", ("category", "content", "source_ids"), "自身preference的content須逐字引用自己簡短偏好原話，不得摘要改寫；conversation的content僅檢索，需自己與群友各一則原文。source_ids填1–4個msg_ID逗號分隔；即興日常不存成經歷，不能改固定核心", "memory", True),
    Tool("EDIT_PERMANENT_MEMORY", ("entry_id", "category", "content", "source_ids"), "以self:ID更新自身條目，分類／原文條件同PERMANENT_MEMORY，不能改固定核心", "memory", True),
    Tool("DELETE_PERMANENT_MEMORY", ("entry_id",), "以self:ID刪除可讀範圍的自身條目", "memory", True),
    Tool("SERVER_MEMORY", ("kind", "content"), "記錄群組梗與共同事件", "memory", True),
    Tool("DELETE_SERVER_MEMORY", ("key",), "刪除指定群組記憶", "memory", True),
]
REGISTRY = {tool.name: tool for tool in TOOLS}
assert len(REGISTRY) == len(TOOLS)


def tool_help(selection: str):
    selection = selection.strip().upper()
    selected = [t for t in TOOLS if t.name == selection or t.group.upper() == selection]
    return selected


# Category descriptions are presentation only. Names and contracts come from TOOLS.
GROUP_DESCRIPTIONS = {
    "discord": "訊息、頻道、貼圖、群資源、成員與投票查詢",
    "web": "網頁",
    "memory": "人物、群組與自身記憶的查找、寫入、更正及刪除",
    "social": "互動、提醒與群組管理",
    "math": "計算、繪圖、LaTeX、文字編碼與雜湊",
}


def instruction_block(loaded=(), compact=True):
    active = [t for t in TOOLS if not compact or t.common or t.name in loaded]
    hidden = [t for t in TOOLS if t not in active]
    lines = [t.help() for t in active]
    if hidden:
        groups = {}
        for tool in hidden:
            groups.setdefault(tool.group, []).append(tool.name)
        lines.append("其他工具，詳細用法用TOOL_HELP查名稱或分類：")
        lines.extend(
            f"{group}（{GROUP_DESCRIPTIONS.get(group, group)}）：" + ",".join(names)
            for group, names in groups.items()
        )
    return (
        "<tools>\n"
        + "工具呼叫只使用下列 [[工具名稱: 參數]] 文字格式；不要輸出 DSML、XML 或原生 function-call 標記。\n"
        + ("呼叫後等待工具回傳，不自行編寫工具結果。\n" if compact else "")
        + "\n".join(lines)
        + "\n文字可搭配工具。[[SPLIT]]分段，[[SPLIT-WAIT]]保留較長停頓，[[$NO_NEED_TO_ANSWER$]]表示不插話。工具結果成功前不能宣稱已完成。\n</tools>"
    )
