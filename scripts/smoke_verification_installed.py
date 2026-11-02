"""Installed wheel python -I; real engine/model/compiler/tools, fake SDK/Discord."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import sayuki_bot
from sayuki_bot.config import load_config
from sayuki_bot.context.compiler import ContextCompiler
from sayuki_bot.conversation.engine import Engine
from sayuki_bot.conversation.models import Event, Turn
from sayuki_bot.llm.client import ModelClient
from sayuki_bot.memory.service import MemoryService
from sayuki_bot.storage.database import Database
from sayuki_bot.tools.executor import ToolExecutor


async def main():
    origin = Path(sayuki_bot.__file__).resolve()
    assert origin.is_relative_to(Path(sys.prefix).resolve())
    with tempfile.TemporaryDirectory(prefix="verification-installed-") as directory:
        root = Path(directory)
        (root / "persona.txt").write_text("合成測試角色")
        (root / "runtime.toml").write_text('[paths]\npersona="persona.txt"\ndatabase="scene.sqlite3"\n[model]\nchat="synthetic"\n')
        cfg = load_config(root / "runtime.toml")
        db = Database(":memory:")
        try:
            now = datetime.now(timezone.utc)
            guild = NS(id=30)

            @asynccontextmanager
            async def typing():
                yield

            channel = NS(id=20, typing=typing, send=AsyncMock(return_value=NS(jump_url="https://discord.com/channels/30/20/999")))
            source = NS(id=110, guild=guild, channel=channel, author=NS(id=10, display_name="群友"),
                        content="試音在二樓", created_at=now, mentions=[], reference=None, attachments=[], stickers=[],
                        jump_url="https://discord.com/channels/30/20/110")
            channel.fetch_message = AsyncMock(return_value=source)
            event = Event(110, 30, 20, 10, "群友", source.content, now)
            db.record_event(event)
            memory = MemoryService(db)
            responses = [NS(choices=[NS(message=NS(content=text), finish_reason="stop")], usage=None)
                         for text in ("從歷史回答但沒查過", "[[READ_MESSAGE: msg_110]]", "確認是二樓")]
            sdk = NS(chat=NS(completions=NS(create=AsyncMock(side_effect=responses))))
            model = ModelClient(cfg, db, "synthetic-placeholder", client=sdk)
            bot = NS(user=NS(id=99), get_guild=lambda identifier: guild)
            access = NS(channel=lambda *args: channel, allowed_channels=lambda turn: [20], authorize=lambda *args: None)
            executor = ToolExecutor(bot, cfg, db, memory, model, access)
            sent = []

            async def send_parts(turn, parts):
                sent.extend(p.text for p in parts if p.kind == "text" and p.text)

            delivery = NS(db=db, send_parts=send_parts, send_text=AsyncMock())
            engine = Engine(cfg, ContextCompiler(cfg, memory), model, executor, delivery, access)
            current = Event(9000, 30, 20, 10, "群友", "幫我打開msg_110確認原文", now, True)
            turn = Turn(current, NS(channel=channel), [event], "mention", bot_id=99)
            await engine.run(turn)
            assert sent == ["確認是二樓"] and channel.fetch_message.await_count == 1
            assert not turn.verification["pending"] and turn.verification["outcomes"] == {"id:110": "live_verified"}
            result = await executor.execute(turn, "POLL", "午餐 | 60 | false | A,B")
            assert result["status"] == "ok" and result["result"]["vote_change_allowed_until_close"] is True
            assert channel.send.call_args.kwargs["poll"].duration.total_seconds() == 3600
            print(json.dumps({"package": str(origin), "literal_read_guard": "passed", "known_id_fetch": "passed",
                              "actual_poll_feedback": "passed", "fake_sdk_calls": sdk.chat.completions.create.await_count,
                              "real_model_calls": 0, "discord_calls": 0}, ensure_ascii=False, indent=2))
        finally:
            db.close()


if __name__ == "__main__":
    asyncio.run(main())
