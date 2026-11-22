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
class ModelSettings:
    name: str
    api_key: str | None
    url: str


@dataclass(frozen=True)
class Settings:
    token: str | None
    model: ModelSettings
    small_model: ModelSettings
    vl_model: ModelSettings
    asr_model: ModelSettings | None
    asr_provider: str
    asr_language: str | None
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
    voice_recordings_dir: Path
    voice_bridge_enabled: bool
    voice_bridge_url: str
    voice_bridge_port: int
    voice_bridge_pcm_host: str
    voice_bridge_pcm_port: int
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

    api_key = os.getenv("OPENROUTER_API_KEY")
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()
    model_name = os.getenv("OPENROUTER_MODEL", "google/gemma-2-9b-it:free").strip()
    small_model_name = os.getenv("OPENROUTER_SMALL_MODEL", model_name).strip()
    vl_model_name = os.getenv(
        "OPENROUTER_VL_MODEL",
        "nvidia/nemotron-nano-12b-v2-vl:free",
    ).strip()
    asr_model_name = os.getenv("ASR_MODEL", "").strip()
    asr_model = None
    if asr_model_name:
        asr_provider = os.getenv("ASR_PROVIDER", "nvidia").strip().lower()
        provider_default_urls = {
            "nvidia": "https://integrate.api.nvidia.com/v1",
            "openai": "https://api.openai.com/v1",
            "elevenlabs": "https://api.elevenlabs.io",
        }
        asr_model = ModelSettings(
            name=asr_model_name,
            api_key=os.getenv("ASR_API_KEY") or api_key,
            url=os.getenv(
                "ASR_BASE_URL",
                provider_default_urls.get(asr_provider, "https://integrate.api.nvidia.com/v1"),
            ).strip(),
        )
    asr_provider = os.getenv("ASR_PROVIDER", "nvidia").strip().lower()
    asr_language = os.getenv("ASR_LANGUAGE", "").strip() or None

    return Settings(
        token=os.getenv("BOT_TOKEN"),
        model=ModelSettings(
            name=model_name,
            api_key=api_key,
            url=base_url,
        ),
        small_model=ModelSettings(
            name=small_model_name,
            api_key=api_key,
            url=base_url,
        ),
        vl_model=ModelSettings(
            name=vl_model_name,
            api_key=api_key,
            url=base_url,
        ),
        asr_model=asr_model,
        asr_provider=asr_provider,
        asr_language=asr_language,
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
        voice_recordings_dir=_env_path(
            "VOICE_RECORDINGS_DIR",
            BASE_DIR / "logs" / "voice",
        ),
        voice_bridge_enabled=_env_bool("VOICE_BRIDGE_ENABLED", True),
        voice_bridge_url=os.getenv(
            "VOICE_BRIDGE_URL",
            "http://127.0.0.1:8787",
        ).strip(),
        voice_bridge_port=_env_int("VOICE_BRIDGE_PORT", 8787),
        voice_bridge_pcm_host=os.getenv("VOICE_BRIDGE_PCM_HOST", "127.0.0.1").strip(),
        voice_bridge_pcm_port=_env_int("VOICE_BRIDGE_PCM_PORT", 8788),
        command_sync_guild_ids=_env_id_set("COMMAND_SYNC_GUILD_IDS"),
        admin_user_ids=_env_id_set("ADMIN_USER_IDS"),
        developer_user_ids=_env_id_set("DEVELOPER_USER_IDS"),
    )
