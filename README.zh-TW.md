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
OPENROUTER_VL_MODEL=nvidia/nemotron-nano-12b-v2-vl:free
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
OBSCURA_BIN=./obscura-aarch64-macos/obscura
MAX_MEMORY_CONTEXT_CHARS=120000
```

## 網頁搜尋

搜尋功能使用 [h4ckf0r0day/obscura](https://github.com/h4ckf0r0day/obscura)。

請下載符合平台的 Obscura binary，並選擇其中一種方式設定：

- 放進 `PATH`，設定 `OBSCURA_BIN=obscura`
- 放在專案內，設定專案相對路徑，例如 `OBSCURA_BIN=./obscura-aarch64-macos/obscura`

Obscura binary 已透過 `obscura*` 加入 `.gitignore`。

## Prompt 工具

模型可以輸出隱藏工具標記，例如：

- `[[SEARCH: query]]`
- `[[REPLY_TO: #msg_1234]]`
- `[[SPLIT]]` / `[[SPLIT-WAIT]]`
- `[[REMIND: minutes | content]]`
- `[[BUTTON_UI: title | description | option1, option2]]`
- `[[MATH_CALC: expression]]`
- `[[MEM_SET: user_id | field | content]]`
- `[[STATUS: text]]`

完整工具列表請看 `SYSTEM_PROMPT.example.txt`。

## 管理員 Slash Commands

只有列在 `ADMIN_USER_IDS` 的 Discord 使用者 ID 可以使用。指令回覆都是 ephemeral，只有執行者看得到。

- `/sayuki_look [note]`：讓 bot 主動查看目前頻道
- `/sayuki_interrupt`：清除佇列並停止尚未輸出的分段訊息
- `/sayuki_status`：查看佇列與發言統計
- `/sayuki_clear_status`：清空 bot 動態狀態
- `/sayuki_reload_prompt`：重新讀取 `SYSTEM_PROMPT.txt`
- `/sayuki_memory_user user_id`：查看指定使用者記憶
- `/sayuki_memory_permanent`：查看永久記憶

## 記憶檔案

執行時資料已加入 `.gitignore`：

- `memory.json`
- `permanent_memory.json`

`memory.json` 使用結構化使用者 profile，可放單一 profile、profile 陣列，或 `{ "user_id": profile }` 形式。

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
- `obscura*`
- 虛擬環境、快取、暫存檔
