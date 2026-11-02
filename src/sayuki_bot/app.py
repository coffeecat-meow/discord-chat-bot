from __future__ import annotations

import argparse
import logging
import os

from sayuki_bot.config import ROOT, load_config


def main():
    parser = argparse.ArgumentParser(description="Sayuki 4.6")
    parser.add_argument("--config")
    parser.add_argument(
        "--check", action="store_true", help="Validate local config and prompt without connecting"
    )
    args = parser.parse_args()
    from dotenv import load_dotenv

    selected = args.config or os.getenv("SAYUKI_CONFIG")
    if selected:
        config = load_config(selected)
        load_dotenv(config.root / ".env")
    else:
        # Preserve source-checkout .env selection. Installed wheels should use
        # an explicit config path, whose deployment directory owns its secrets.
        load_dotenv(ROOT / ".env")
        config = load_config()
    if not config.path("persona").is_file():
        parser.error("Persona file does not exist")
    if args.check:
        print("Configuration and persona path OK; no model or Discord connection made")
        return
    from sayuki_bot.llm.routing import primary_key, route

    api_key = primary_key(config)
    if not os.getenv("BOT_TOKEN") or not api_key:
        parser.error(f"Set BOT_TOKEN and {route(config).api_key_env} in .env")
    if not route(config).model:
        parser.error("Configure the primary chat model")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from pathlib import Path

    from sayuki_bot.storage import jsonl_log

    log_dir = Path(config.get("logs", "directory", "logs")).expanduser()
    jsonl_log.configure(
        log_dir if log_dir.is_absolute() else config.root / log_dir,
        tz=config.sleep.timezone,
        max_mb=config.get("logs", "max_mb", 50),
    )
    from sayuki_bot.discord.bot import SayukiBot
    from sayuki_bot.storage.instance_lock import DatabaseLock

    with DatabaseLock(config.path("database")):
        SayukiBot(config, api_key).run(os.environ["BOT_TOKEN"])
