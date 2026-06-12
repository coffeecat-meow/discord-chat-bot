# Discord Chat Bot

一個可自訂角色的通用 Discord 群聊機器人，使用 OpenRouter 相容的聊天模型。

本專案提供模組化 Python bot，包含對話記憶、圖片解析、Obscura 網頁搜尋、數學工具、提醒、反應、按鈕互動、主動查看頻道、管理員 slash command，以及可替換的角色 prompt。

[English README](README.md)

## 功能

- Discord 訊息處理與近期對話上下文
- OpenRouter 文字模型與視覺模型
- 透過 `SYSTEM_PROMPT.txt` 自訂角色
- 結構化使用者記憶與永久記憶
- 主動查看頻道
- 分段發送與模擬輸入中
- 可指定回覆近期訊息
- 可解析 Discord 頻道標記與訊息連結
- Obscura 瀏覽器搜尋
- 數學計算、函數繪圖、LaTeX 圖片渲染
- 反應、提醒、身分組查詢、禁言、動態狀態、按鈕 UI
- 管理員限定 slash command

## 專案結構

- `bot.py`：啟動入口
- `SYSTEM_PROMPT.example.txt`：公開 prompt 範例
- `math_tools.py`：數學、繪圖、LaTeX 渲染
- `sayuki_bot/config.py`：環境變數設定
- `sayuki_bot/bot_app.py`：Discord bot 建立、事件、slash command
- `sayuki_bot/llm.py`：OpenRouter 文字與視覺模型呼叫
- `sayuki_bot/memory.py`：結構化 JSON 記憶
- `sayuki_bot/message_context.py`：聊天歷史與上下文
- `sayuki_bot/scheduler.py`：佇列、工具標記、訊息發送
- `sayuki_bot/search.py`：Obscura 搜尋包裝
- `sayuki_bot/ui.py`：Discord 按鈕互動

## 安裝

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Windows：

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## 設定

複製範例檔：

```bash
cp .env.example .env
cp SYSTEM_PROMPT.example.txt SYSTEM_PROMPT.txt
```

編輯 `.env`：

```env
BOT_TOKEN=
OPENROUTER_API_KEY=
OPENROUTER_MODEL=google/gemma-2-9b-it:free
OPENROUTER_SMALL_MODEL=google/gemma-2-9b-it:free
OPENROUTER_VL_MODEL=nvidia/nemotron-nano-12b-v2-vl:free
OPENROUTER_USE_REASONING_EFFORT=false
OPENROUTER_REASONING_EFFORT=medium
ADMIN_USER_IDS=123456789012345678
DEVELOPER_USER_IDS=123456789012345678
```

編輯 `SYSTEM_PROMPT.txt`，在 role 區塊填入你自己的角色設定。

可選設定：

```env
MAX_QUEUE_SIZE=5
HISTORY_LIMIT=25
PROACTIVE_COOLDOWN_SECONDS=120
ECHO_LOOKBACK_SECONDS=60
ECHO_MIN_USERS=3
SYSTEM_PROMPT_PATH=SYSTEM_PROMPT.txt
MEMORY_DB_FILE=memory.json
PERMANENT_MEMORY_DB_FILE=permanent_memory.json
SERVER_MEMORY_FILE=server_memory.json
USER_STATS_FILE=user_stats.json
SHORT_MEMORY_FILE=short_term_memory.json
SHORT_MEMORY_PENDING_FILE=logs/short_memory_pending.jsonl
SHORT_MEMORY_TTL_SECONDS=21600
SHORT_MEMORY_TRIGGER_MESSAGES=40
SHORT_MEMORY_MIN_INTERVAL_SECONDS=600
SHORT_MEMORY_MAX_CONTEXT_CHARS=5000
IMAGE_CACHE_TTL_SECONDS=21600
IMAGE_CACHE_MAX_ITEMS=500
CONVERSATION_LOG_FILE=logs/conversation.jsonl
INVOCATION_LOG_FILE=logs/invocation.jsonl
TOOL_STATS_FILE=tool_stats.json
OBSCURA_BIN=./obscura-aarch64-macos/obscura
MAX_MEMORY_CONTEXT_CHARS=120000
```

`OPENROUTER_USE_REASONING_EFFORT=true` 時，只會對文字模型送出 `reasoning.effort`，VL模型不會套用。若模型不支援effort level，請維持 `false`。`OPENROUTER_REASONING_EFFORT` 通常可填 `low`、`medium`、`high`。

`OPENROUTER_SMALL_MODEL` 用於 bot 被呼叫時的短期記憶消化。若未設定，會沿用 `OPENROUTER_MODEL`。

## 網頁搜尋

搜尋功能使用 [h4ckf0r0day/obscura](https://github.com/h4ckf0r0day/obscura)。

請下載符合平台的 Obscura binary，並選擇其中一種方式設定：

- 放進 `PATH`，設定 `OBSCURA_BIN=obscura`
- 放在專案內，設定專案相對路徑，例如 `OBSCURA_BIN=./obscura-aarch64-macos/obscura`

Obscura binary 已透過 `obscura*` 加入 `.gitignore`。

## Prompt 工具

模型可以輸出隱藏工具標記，例如：

- `[[SEARCH: query]]`
- `[[READ_WEB: URL]]`
- `[[REPLY_TO: #msg_1234]]`
- `[[SPLIT]]` / `[[SPLIT-WAIT]]`
- `[[REMIND: minutes | content]]`
- `[[BUTTON_UI: title | description | option1, option2]]`
- `[[POLL: title | minutes | multiple | option1, option2]]`
- `[[VIEW_IMAGE: #msg_1234]]`
- `[[MATH_CALC: expression]]`
- `[[MEM_SET: user_id | field | content]]`
- `[[MEM_EVENT_FOR: user_id | YYYY-MM-DD | type | source | content]]`
- `[[SERVER_MEMORY: type | content]]`
- `[[DM_USER: user_id | message]]`
- `[[THREAD: thread title | first message]]`
- `[[NICKNAME: user_id | new nickname]]`
- `[[SERVER_EVENT: event name | YYYY-MM-DD HH:MM | YYYY-MM-DD HH:MM | location | description]]`
- `[[USER_STATS: user_id]]`
- `[[LOOKUP_MEMORY: user_id]]`
- `[[STATUS: text]]`

完整工具列表請看 `SYSTEM_PROMPT.example.txt`。

## 管理員 Slash Commands

只有列在 `ADMIN_USER_IDS` 的 Discord 使用者 ID 可以使用。指令回覆都是 ephemeral，只有執行者看得到。

- `/sayuki_look [note]`：讓 bot 主動查看目前頻道
- `/sayuki_interrupt`：清除佇列並停止尚未輸出的分段訊息
- `/sayuki_status`：查看佇列與發言統計
- `/sayuki_tool_stats`：查看工具呼叫、失敗次數與平均耗時
- `/sayuki_debug_last`：查看最近一次處理/debug摘要
- `/sayuki_logs [kind] [day] [limit]`：查看對話、LLM調用或短期記憶紀錄
- `/sayuki_clear_status`：清空 bot 動態狀態
- `/sayuki_reload_prompt`：重新讀取 `SYSTEM_PROMPT.txt`
- `/sayuki_memory_user user_id`：查看指定使用者記憶
- `/sayuki_memory_permanent`：查看永久記憶
- `/sayuki_memory_server`：查看目前伺服器記憶
- `/sayuki_user_stats user_id`：查看指定使用者互動統計

## 記憶檔案

執行時資料已加入 `.gitignore`：

- `memory.json`
- `permanent_memory.json`
- `server_memory.json`
- `short_term_memory.json`
- `logs/short_memory_pending.jsonl`
- `user_stats.json`
- `tool_stats.json`
- `logs/conversation.jsonl`
- `logs/invocation.jsonl`

`memory.json` 使用結構化使用者 profile，可放單一 profile、profile 陣列，或 `{ "user_id": profile }` 形式。

執行時只會完整附送目前情境相關使用者的記憶，例如目前對話者、近期頻道參與者、被提及的人、回覆目標，以及訊息連結解析出的作者。其他使用者只會附成精簡索引，模型需要時可用 `[[LOOKUP_MEMORY: user_id]]` 查完整 profile。

`server_memory.json` 儲存Discord伺服器專屬的梗、稱呼、黑歷史與重大事件。

`short_term_memory.json` 儲存會過期的頻道摘要，以及近期使用者與 bot 的互動摘要。短期記憶候選原文會先寫入 `logs/short_memory_pending.jsonl`，平常不背景摘要；只有 bot 真的被呼叫時才會消化有效內容，所以閒聊不會產生LLM調用。

`user_stats.json` 儲存輕量互動統計，例如看過訊息數、觸發 bot 次數、bot 回覆次數、最後互動時間與頻道統計。

`tool_stats.json` 儲存持久化工具統計，包含今日、本月、永久的呼叫次數、失敗次數與平均耗時。

`logs/conversation.jsonl` 儲存對話紀錄：時間、頻道/使用者ID、觸發原因、使用者文字、bot回覆文字與查詢工具使用情況。

`logs/invocation.jsonl` 儲存LLM調用紀錄，包含調用類型、模型、非system的輸入訊息、輸出文字、耗時與成功/錯誤狀態。system prompt內容不會逐次寫入，只會記錄字數。

## 啟動

```bash
python bot.py
```

或：

```bash
python -m sayuki_bot
```

## Git 注意事項

以下檔案不會提交：

- `.env`
- `SYSTEM_PROMPT.txt`
- `SYSTEM_PROMPT.private.txt`
- `README.private.md`
- `memory.json`
- `permanent_memory.json`
- `tool_stats.json`
- `logs/`
- `obscura*`
- 虛擬環境、快取、暫存檔
