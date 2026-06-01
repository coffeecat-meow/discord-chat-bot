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
OPENROUTER_VL_MODEL=nvidia/nemotron-nano-12b-v2-vl:free
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
OBSCURA_BIN=./obscura-aarch64-macos/obscura
MAX_MEMORY_CONTEXT_CHARS=120000
```

## Web Search

Web search uses [h4ckf0r0day/obscura](https://github.com/h4ckf0r0day/obscura).

Download the correct Obscura binary for your platform and either:

- Put it in your `PATH` and set `OBSCURA_BIN=obscura`
- Place it inside the project and set a project-relative path, such as `OBSCURA_BIN=./obscura-aarch64-macos/obscura`

Obscura binaries are ignored by git through `obscura*`.

## Prompt Tools

The model can emit hidden tool tags such as:

- `[[SEARCH: query]]`
- `[[REPLY_TO: #msg_1234]]`
- `[[SPLIT]]` / `[[SPLIT-WAIT]]`
- `[[REMIND: minutes | content]]`
- `[[BUTTON_UI: title | description | option1, option2]]`
- `[[MATH_CALC: expression]]`
- `[[MEM_SET: user_id | field | content]]`
- `[[STATUS: text]]`

See `SYSTEM_PROMPT.example.txt` for the full tool list.

## Admin Slash Commands

Only Discord user IDs listed in `ADMIN_USER_IDS` can use these commands. Command responses are ephemeral.

- `/sayuki_look [note]` - ask the bot to proactively inspect the current channel
- `/sayuki_interrupt` - clear queued requests and stop unsent split output
- `/sayuki_status` - show queue and message stats
- `/sayuki_clear_status` - clear the bot presence
- `/sayuki_reload_prompt` - reload `SYSTEM_PROMPT.txt`
- `/sayuki_memory_user user_id` - inspect a user's memory
- `/sayuki_memory_permanent` - inspect permanent bot memory

## Memory Files

Runtime files are ignored by git:

- `memory.json`
- `permanent_memory.json`

`memory.json` stores structured user profiles. It can be a single profile, a list of profiles, or a mapping of `{ "user_id": profile }`.

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
- `obscura*`
- virtualenvs, caches, and temporary files
