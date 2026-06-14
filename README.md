# Discord Chat Bot

A general-purpose Discord group chat bot powered by OpenRouter-compatible chat models.

This project provides a modular Python bot with conversation memory, image analysis, web search through Obscura, math tools, reminders, reactions, button interactions, proactive channel watching, admin slash commands, and configurable character prompts.

[繁體中文 README](README.zh-TW.md)

## Features

- Discord message handling with chat history context
- OpenRouter text model and vision model support
- Configurable character prompt via `SYSTEM_PROMPT.txt`
- Structured user memory and permanent bot memory
- Proactive channel watching
- Split-message delivery with simulated typing
- Reply targeting for recent messages
- Discord channel tags and message links are resolved into readable context
- Obscura-powered browser search
- Math calculation, function plotting, and LaTeX rendering
- Reactions, reminders, role checks, user timeouts, presence updates, and button UI
- Admin-only slash commands

## Project Structure

- `bot.py` - compatibility entry point
- `SYSTEM_PROMPT.example.txt` - public prompt template
- `math_tools.py` - math, plotting, and LaTeX rendering helpers
- `sayuki_bot/config.py` - environment configuration
- `sayuki_bot/bot_app.py` - Discord bot setup, events, and slash commands
- `sayuki_bot/llm.py` - OpenRouter text and vision calls
- `sayuki_bot/memory.py` - structured JSON memory storage
- `sayuki_bot/message_context.py` - chat history and context building
- `sayuki_bot/scheduler.py` - queue, tool tag handling, and message sending
- `sayuki_bot/search.py` - Obscura browser search wrapper
- `sayuki_bot/ui.py` - Discord button interaction view

## Installation

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

On Windows:

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

Copy the examples:

```bash
cp .env.example .env
cp SYSTEM_PROMPT.example.txt SYSTEM_PROMPT.txt
```

Edit `.env`:

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

Edit `SYSTEM_PROMPT.txt` and replace the role section with your bot persona.

Optional settings:

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
PRESENCE_TTL_SECONDS=21600
PRESENCE_MAX_CONTEXT_USERS=8
CONVERSATION_LOG_FILE=logs/conversation.jsonl
INVOCATION_LOG_FILE=logs/invocation.jsonl
TOOL_STATS_FILE=tool_stats.json
REMINDERS_FILE=reminders.json
OBSCURA_BIN=./obscura-aarch64-macos/obscura
MAX_MEMORY_CONTEXT_CHARS=120000
```

`OPENROUTER_USE_REASONING_EFFORT=true` sends `reasoning.effort` to the text model only. Leave it `false` for models that do not support reasoning effort. `OPENROUTER_REASONING_EFFORT` is usually `low`, `medium`, or `high`.

`OPENROUTER_SMALL_MODEL` is used for short-term memory digestion when the bot is invoked. If it is omitted, the bot uses `OPENROUTER_MODEL`.

## Web Search

Web search uses [h4ckf0r0day/obscura](https://github.com/h4ckf0r0day/obscura).

Download the correct Obscura binary for your platform and either:

- Put it in your `PATH` and set `OBSCURA_BIN=obscura`
- Place it inside the project and set a project-relative path, such as `OBSCURA_BIN=./obscura-aarch64-macos/obscura`

Obscura binaries are ignored by git through `obscura*`.

## Prompt Tools

The model can emit hidden tool tags such as:

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
- `[[CHECK_PRESENCE: user_id]]`
- `[[STATUS: text]]`

See `SYSTEM_PROMPT.example.txt` for the full tool list.

## Admin Slash Commands

Only Discord user IDs listed in `ADMIN_USER_IDS` can use these commands. Command responses are ephemeral.

- `/sayuki_look [note]` - ask the bot to proactively inspect the current channel
- `/sayuki_interrupt` - clear queued requests and stop unsent split output
- `/sayuki_status` - show queue and message stats
- `/sayuki_permissions` - diagnose bot permissions in the current channel/server
- `/sayuki_tool_stats` - inspect tool calls, failures, and average durations
- `/sayuki_debug_last` - show the latest processing/debug summary
- `/sayuki_logs [kind] [day] [limit]` - inspect conversation, LLM invocation, or short-term memory logs
- `/sayuki_reminders [limit]` - inspect pending reminders
- `/sayuki_cancel_reminder reminder_id` - cancel a pending reminder
- `/sayuki_clear_status` - clear the bot presence
- `/sayuki_reload_prompt` - reload `SYSTEM_PROMPT.txt`
- `/sayuki_memory_user user_id` - inspect a user's memory
- `/sayuki_memory_permanent` - inspect permanent bot memory
- `/sayuki_memory_server` - inspect memory for the current Discord server
- `/sayuki_user_stats user_id` - inspect user interaction statistics

## Memory Files

Runtime files are ignored by git:

- `memory.json`
- `permanent_memory.json`
- `server_memory.json`
- `short_term_memory.json`
- `logs/short_memory_pending.jsonl`
- `user_stats.json`
- `tool_stats.json`
- `reminders.json`
- `logs/conversation.jsonl`
- `logs/invocation.jsonl`

`memory.json` stores structured user profiles. It can be a single profile, a list of profiles, or a mapping of `{ "user_id": profile }`.

At runtime, the bot injects full memory only for users related to the current context, such as the current speaker, recent channel participants, mentioned users, reply targets, and resolved message-link authors. Other profiles are injected as a compact index, and the model can request a full profile with `[[LOOKUP_MEMORY: user_id]]`.

Discord presence/status is cached from gateway presence updates and injected only for users related to the current context. The model can request a cached user status with `[[CHECK_PRESENCE: user_id]]`. Enable the Presence Intent in the Discord Developer Portal for this to work.

`server_memory.json` stores Discord-server-specific jokes, nicknames, running gags, and important server events.

`short_term_memory.json` stores expiring channel summaries and recent user-bot interaction summaries. Raw short-term candidates are appended to `logs/short_memory_pending.jsonl` and are not summarized in the background. They are digested only when the bot is actually invoked, so idle chat does not create LLM calls.

`user_stats.json` stores lightweight interaction counters such as messages seen, bot triggers, bot replies, last interaction times, and channel-level counts.

`tool_stats.json` stores persistent tool usage counters for today, current month, and all time, including failures and average duration.

`reminders.json` stores pending reminders so they can be restored after a bot restart.

`logs/conversation.jsonl` stores conversation records: timestamps, channel/user identifiers, trigger reason, user message text, bot response text, and query tool usage.

`logs/invocation.jsonl` stores LLM call records, including call type, model, non-system input messages, output text, duration, and success/error status. The system prompt content is omitted and only its character count is recorded.

## Running

```bash
python bot.py
```

or:

```bash
python -m sayuki_bot
```

## Git Hygiene

These files are intentionally ignored:

- `.env`
- `SYSTEM_PROMPT.txt`
- `SYSTEM_PROMPT.private.txt`
- `README.private.md`
- `memory.json`
- `permanent_memory.json`
- `tool_stats.json`
- `logs/`
- `obscura*`
- virtualenvs, caches, and temporary files
