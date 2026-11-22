from __future__ import annotations

import asyncio
import base64
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import requests
from aiohttp import web

from .asr import pcm_to_wav
from .types import ChatBot, Message, Room, User


logger = logging.getLogger(__name__)


def start_voice_bridge_process(
    base_dir: Path,
    token: str,
    bridge_url: str,
    bridge_port: int,
    pcm_port: int,
    pcm_host: str = "127.0.0.1",
) -> subprocess.Popen | None:
    package_script = base_dir / "voice_bridge.js"
    if not package_script.exists():
        logger.error("找不到 Voice bridge 腳本: %s", package_script)
        return None

    env = os.environ.copy()
    env["BOT_TOKEN"] = token
    env["VOICE_BRIDGE_PORT"] = str(bridge_port)
    env["VOICE_BRIDGE_PCM_URL"] = (
        f"http://{pcm_host}:{pcm_port}/voice/pcm"
    )
    try:
        process = subprocess.Popen(
            ["node", str(package_script)],
            cwd=str(base_dir),
            env=env,
        )
        logger.info("已啟動 Node.js voice bridge (PID %s)", process.pid)
        return process
    except Exception as exc:
        logger.error("啟動 Node.js voice bridge 失敗: %s", exc)
        return None


class VoiceBridgeClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8787") -> None:
        self.base_url = base_url.rstrip("/")

    async def join(self, guild_id: int, channel_id: int) -> dict:
        payload = {"guildId": str(guild_id), "channelId": str(channel_id)}
        logger.info(
            "Voice bridge join payload: guild_id=%s, channel_id=%s",
            guild_id,
            channel_id,
        )
        last_error: Exception | None = None
        for _ in range(20):
            try:
                return await asyncio.to_thread(
                    self._post_json,
                    "/join",
                    payload,
                )
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                if "not ready" not in message and "connection failed" not in message:
                    raise
                await asyncio.sleep(0.5)
        if last_error is not None:
            raise last_error
        raise RuntimeError("Voice bridge 加入語音逾時")

    async def leave(self, guild_id: int) -> dict:
        return await asyncio.to_thread(
            self._post_json,
            "/leave",
            {"guildId": guild_id},
        )

    def _post_json(self, path: str, payload: dict) -> dict:
        try:
            response = requests.post(
                f"{self.base_url}{path}",
                json=payload,
                timeout=10,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Voice bridge connection failed: {exc}") from exc
        if not response.ok:
            raise RuntimeError(
                f"Voice bridge HTTP {response.status_code}: {response.text}"
            )
        return response.json()


class VoiceBridgeServer:
    def __init__(
        self,
        bot: ChatBot,
        host: str,
        port: int,
        recordings_dir: Path,
    ) -> None:
        self.bot = bot
        self.host = host
        self.port = port
        self.recordings_dir = recordings_dir
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application(client_max_size=32 * 1024 * 1024)
        app.router.add_post("/voice/pcm", self._handle_pcm)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, self.host, self.port).start()
        logger.info(
            "Voice bridge PCM callback listening on http://%s:%d/voice/pcm",
            self.host,
            self.port,
        )

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    async def _handle_pcm(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            pcm = base64.b64decode(payload["pcmBase64"])
            user_id = int(payload["userId"])
            guild_id = int(payload["guildId"])
            channel_id = int(payload["channelId"])

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            recording_path = (
                self.recordings_dir
                / f"{timestamp}_{user_id}_{uuid4().hex[:8]}.wav"
            )
            wav_data = pcm_to_wav(pcm)
            await asyncio.to_thread(
                self._save_recording,
                recording_path,
                wav_data,
            )
            logger.info("已儲存語音錄音: %s", recording_path)

            asr_manager = self.bot.sayuki.asr_manager
            if asr_manager is None:
                return web.json_response({"text": ""})

            asr_result = await asr_manager.transcribe(pcm)
            text = asr_result.text.strip()
            if not text:
                return web.json_response({"text": ""})

            voice_channel = self.bot.get_channel(channel_id)
            if voice_channel is None:
                voice_channel = await self.bot.fetch_channel(channel_id)

            discord_user = self.bot.get_user(user_id)
            if discord_user is None:
                discord_user = await self.bot.fetch_user(user_id)

            guild = getattr(voice_channel, "guild", None)
            room = Room.from_discord(
                voice_channel,
                registry=self.bot.sayuki.room_registry,
            )
            message = Message.voice(
                author=User.from_discord(discord_user),
                room=room,
                text=text,
                pcm_data=pcm,
            )
            await self.bot.sayuki.process_chat_message(message)
            logger.info("recognized: %s", text)
            return web.json_response({"text": text, "guildId": guild_id})
        except Exception as exc:
            logger.error("Voice bridge PCM 處理失敗: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    @staticmethod
    def _save_recording(path: Path, wav_data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(wav_data)
