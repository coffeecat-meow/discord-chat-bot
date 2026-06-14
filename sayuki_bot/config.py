from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
TW_TZ = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class Settings:
    token: str | None
    openrouter_api_key: str | None
    openrouter_model: str
    openrouter_small_model: str
    openrouter_vl_model: str
    openrouter_use_reasoning_effort: bool
    openrouter_reasoning_effort: str
    temp_dir: str
    max_queue_size: int
    history_limit: int
    proactive_cooldown_seconds: int
    echo_lookback_seconds: int
    echo_min_users: int
    system_prompt_path: Path
    memory_db_file: str
    permanent_memory_db_file: str
    server_memory_file: str
    user_stats_file: str
    short_memory_file: str
    short_memory_pending_file: Path
    short_memory_ttl_seconds: int
    short_memory_trigger_messages: int
    short_memory_min_interval_seconds: int
    short_memory_max_context_chars: int
    image_cache_ttl_seconds: int
    image_cache_max_items: int
    presence_ttl_seconds: int
    presence_max_context_users: int
    presence_max_age_seconds: int
    presence_cleanup_interval_seconds: int
    discord_component_cache_ttl_seconds: int
    discord_component_cache_max_items: int
    conversation_log_file: Path
    invocation_log_file: Path
    tool_stats_file: Path
    reminders_file: Path
    command_sync_guild_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    developer_user_ids: frozenset[int]


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default

    try:
        return int(value)
    except ValueError:
        logging.getLogger(__name__).warning("%s 必須是整數，已使用預設值 %s", name, default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "是", "啟用"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "否", "停用"}:
        return False

    logging.getLogger(__name__).warning("%s 必須是布林值，已使用預設值 %s", name, default)
    return default


def _env_path(name: str, default: Path) -> Path:
    raw_value = os.getenv(name)
    path = Path(raw_value) if raw_value else default
    return path if path.is_absolute() else BASE_DIR / path


def _env_id_set(name: str) -> frozenset[int]:
    raw_value = os.getenv(name, "")
    ids = set()
    for item in raw_value.replace(";", ",").split(","):
        value = item.strip()
        if not value:
            continue
        if value.isdigit():
            ids.add(int(value))
        else:
            logging.getLogger(__name__).warning("%s 包含非純數字ID，已忽略: %s", name, value)

    return frozenset(ids)


def load_settings() -> Settings:
    load_dotenv()

    return Settings(
        token=os.getenv("BOT_TOKEN"),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "google/gemma-2-9b-it:free"),
        openrouter_small_model=os.getenv(
            "OPENROUTER_SMALL_MODEL",
            os.getenv("OPENROUTER_MODEL", "google/gemma-2-9b-it:free"),
        ),
        openrouter_vl_model=os.getenv(
            "OPENROUTER_VL_MODEL",
            "nvidia/nemotron-nano-12b-v2-vl:free",
        ),
        openrouter_use_reasoning_effort=_env_bool("OPENROUTER_USE_REASONING_EFFORT", False),
        openrouter_reasoning_effort=os.getenv("OPENROUTER_REASONING_EFFORT", "medium").strip(),
        temp_dir=os.getenv("TEMP_DIR", "./temp"),
        max_queue_size=_env_int("MAX_QUEUE_SIZE", 5),
        history_limit=_env_int("HISTORY_LIMIT", 25),
        proactive_cooldown_seconds=_env_int("PROACTIVE_COOLDOWN_SECONDS", 120),
        echo_lookback_seconds=_env_int("ECHO_LOOKBACK_SECONDS", 60),
        echo_min_users=_env_int("ECHO_MIN_USERS", 3),
        system_prompt_path=_env_path("SYSTEM_PROMPT_PATH", BASE_DIR / "SYSTEM_PROMPT.txt"),
        memory_db_file=os.getenv("MEMORY_DB_FILE", "memory.json"),
        permanent_memory_db_file=os.getenv("PERMANENT_MEMORY_DB_FILE", "permanent_memory.json"),
        server_memory_file=os.getenv("SERVER_MEMORY_FILE", "server_memory.json"),
        user_stats_file=os.getenv("USER_STATS_FILE", "user_stats.json"),
        short_memory_file=os.getenv("SHORT_MEMORY_FILE", "short_term_memory.json"),
        short_memory_pending_file=_env_path(
            "SHORT_MEMORY_PENDING_FILE",
            BASE_DIR / "logs" / "short_memory_pending.jsonl",
        ),
        short_memory_ttl_seconds=_env_int("SHORT_MEMORY_TTL_SECONDS", 21600),
        short_memory_trigger_messages=_env_int("SHORT_MEMORY_TRIGGER_MESSAGES", 40),
        short_memory_min_interval_seconds=_env_int("SHORT_MEMORY_MIN_INTERVAL_SECONDS", 600),
        short_memory_max_context_chars=_env_int("SHORT_MEMORY_MAX_CONTEXT_CHARS", 5000),
        image_cache_ttl_seconds=_env_int("IMAGE_CACHE_TTL_SECONDS", 21600),
        image_cache_max_items=_env_int("IMAGE_CACHE_MAX_ITEMS", 500),
        presence_ttl_seconds=_env_int("PRESENCE_TTL_SECONDS", 21600),
        presence_max_context_users=_env_int("PRESENCE_MAX_CONTEXT_USERS", 8),
        presence_max_age_seconds=_env_int("PRESENCE_MAX_AGE_SECONDS", 86400),
        presence_cleanup_interval_seconds=_env_int("PRESENCE_CLEANUP_INTERVAL_SECONDS", 3600),
        discord_component_cache_ttl_seconds=_env_int("DISCORD_COMPONENT_CACHE_TTL_SECONDS", 30),
        discord_component_cache_max_items=_env_int("DISCORD_COMPONENT_CACHE_MAX_ITEMS", 500),
        conversation_log_file=_env_path("CONVERSATION_LOG_FILE", BASE_DIR / "logs" / "conversation.jsonl"),
        invocation_log_file=_env_path("INVOCATION_LOG_FILE", BASE_DIR / "logs" / "invocation.jsonl"),
        tool_stats_file=_env_path("TOOL_STATS_FILE", BASE_DIR / "tool_stats.json"),
        reminders_file=_env_path("REMINDERS_FILE", BASE_DIR / "reminders.json"),
        command_sync_guild_ids=_env_id_set("COMMAND_SYNC_GUILD_IDS"),
        admin_user_ids=_env_id_set("ADMIN_USER_IDS"),
        developer_user_ids=_env_id_set("DEVELOPER_USER_IDS"),
    )
