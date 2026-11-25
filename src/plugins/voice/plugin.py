"""Voice bridge plugin: ASR transcripts enter the normal Turn/Engine pipeline."""

from __future__ import annotations

import asyncio
import base64
import hmac
import logging
import os
import secrets
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from .asr import ASRProvider, pcm_to_wav
from plugins.api import InboundEvent, Route

from sayuki_bot.tools.registry import Tool
from sayuki_bot.discord.bot import SayukiBot

logger = logging.getLogger(__name__)
MAX_PCM_BYTES = 8 * 1024 * 1024


class VoicePlugin:
    """Optional receiver; transcripts route to the text channel where voice was joined."""

    name = "voice"

    def __init__(self, bot: SayukiBot):
        self.bot = bot
        self.config = bot.config.get("plugins", "voice", {})
        self.asr = ASRProvider(bot.config)
        self.secret = secrets.token_urlsafe(32)
        self.routes: dict[tuple[int, int], int] = {}
        self.runner = None
        self.process: subprocess.Popen | None = None
        self.semaphore = asyncio.Semaphore(4)

    def register(self, runtime):
        runtime.add_tool(
            Tool("VOICE_JOIN", (), "Join your current voice channel and route ASR transcripts to Sayuki; admin only", "voice", True),
            self.execute_tool,
        )
        runtime.add_tool(
            Tool("VOICE_LEAVE", (), "Leave the active voice channel and stop transcription; admin only", "voice", True),
            self.execute_tool,
        )

    async def execute_tool(self, turn, name, arguments):
        if name == "VOICE_JOIN":
            member = getattr(turn.source, "author", None)
            channel = getattr(getattr(member, "voice", None), "channel", None)
            if not channel:
                raise PermissionError("Join a voice channel before requesting VOICE_JOIN")
            return await self.join(turn.event.guild_id, channel.id, turn.event.channel_id)
        if name == "VOICE_LEAVE":
            return await self.leave(turn.event.guild_id)
        raise KeyError(name)

    async def start(self):
        if self.runner:
            return
        app = web.Application(client_max_size=12 * 1024 * 1024)
        app.router.add_post("/voice/pcm", self._handle_pcm)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        try:
            await web.TCPSite(
                self.runner, "127.0.0.1", int(self.config.get("pcm_port", 8788))
            ).start()
        except Exception:
            await self.stop()
            raise
        script = Path(__file__).resolve().parent / "voice_bridge.js"
        if not script.is_file():
            await self.stop()
            raise FileNotFoundError(f"Voice bridge script missing: {script}")
        inherited = (
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA",
        )
        env = {key: os.environ[key] for key in inherited if key in os.environ}
        env.update(
            BOT_TOKEN=os.environ.get("BOT_TOKEN", ""),
            VOICE_BRIDGE_PORT=str(self.config.get("bridge_port", 8787)),
            VOICE_BRIDGE_PCM_URL=f"http://127.0.0.1:{self.config.get('pcm_port', 8788)}/voice/pcm",
            VOICE_BRIDGE_SECRET=self.secret,
            VOICE_ASR_SILENCE_SECONDS=str(self.config.get("silence_seconds", 1.5)),
        )
        try:
            self.process = subprocess.Popen(
            ["node", str(script)], cwd=str(script.parent), env=env
            )
        except Exception:
            await self.stop()
            raise
        logger.info("Voice bridge started (pid=%s)", self.process.pid)

    async def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
        self.process = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    async def join(self, guild_id: int, voice_channel_id: int, text_channel_id: int):
        self.routes[(guild_id, voice_channel_id)] = text_channel_id
        try:
            result = await self._request(
                "/join",
                {"guildId": str(guild_id), "channelId": str(voice_channel_id)},
            )
            for key in list(self.routes):
                if key[0] == guild_id and key[1] != voice_channel_id:
                    self.routes.pop(key, None)
            return result
        except Exception:
            self.routes.pop((guild_id, voice_channel_id), None)
            raise

    async def leave(self, guild_id: int, voice_channel_id: int | None = None):
        result = await self._request("/leave", {"guildId": str(guild_id)})
        for key in list(self.routes):
            if key[0] == guild_id and (voice_channel_id is None or key[1] == voice_channel_id):
                self.routes.pop(key, None)
        return result

    async def _request(self, path, payload):
        import aiohttp

        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Voice bridge process is not running")
        url = f"http://127.0.0.1:{self.config.get('bridge_port', 8787)}{path}"
        last = None
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            for attempt in range(20):
                try:
                    async with session.post(
                        url, json=payload, headers={"x-voice-bridge-secret": self.secret}
                    ) as response:
                        data = await response.json()
                        if response.status >= 400:
                            raise RuntimeError(
                                data.get("error", f"Voice bridge HTTP {response.status}")
                            )
                        return data
                except (aiohttp.ClientError, RuntimeError) as exc:
                    last = exc
                    if (
                        "not ready" not in str(exc).lower()
                        and not isinstance(exc, aiohttp.ClientError)
                    ):
                        raise
                    if attempt < 19:
                        await asyncio.sleep(0.5)
        raise RuntimeError(f"Voice bridge did not become ready: {last}")

    async def _handle_pcm(self, request):
        if not hmac.compare_digest(request.headers.get("x-voice-bridge-secret", ""), self.secret):
            raise web.HTTPUnauthorized()
        try:
            payload = await request.json()
            user_id, guild_id, voice_id = (
                int(payload["userId"]), int(payload["guildId"]), int(payload["channelId"])
            )
            pcm = base64.b64decode(payload["pcmBase64"], validate=True)
        except (KeyError, TypeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid PCM event")
        if not pcm or len(pcm) % 4:
            raise web.HTTPBadRequest(text="PCM must contain complete stereo frames")
        if len(pcm) > MAX_PCM_BYTES:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_PCM_BYTES, actual_size=len(pcm))
        try:
            self._validate_voice_source(guild_id, voice_id, user_id)
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc))
        if self.semaphore.locked():
            raise web.HTTPServiceUnavailable(text="Voice transcription capacity is full")
        async with self.semaphore:
            try:
                text = (await self.asr.transcribe(pcm)).text.strip()
                if not text:
                    return web.json_response({"status": "empty"})
                queued = await self._submit_transcript(guild_id, voice_id, user_id, text)
                if self.config.get("recordings_enabled", False):
                    await asyncio.to_thread(self._save_recording, guild_id, user_id, pcm)
                return web.json_response({"status": queued})
            except PermissionError as exc:
                logger.info("Voice event rejected: %s", exc)
                raise web.HTTPForbidden(text=str(exc))
            except Exception:
                logger.exception("Voice transcript processing failed")
                raise web.HTTPInternalServerError(text="Voice processing failed")

    def _validate_voice_source(self, guild_id, voice_id, user_id):
        if guild_id not in self.bot.runtime_guilds:
            raise PermissionError("Guild is not trusted")
        text_id = self.routes.get((guild_id, voice_id))
        if text_id is None:
            raise PermissionError("There is no active text route for this voice channel")
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        text_channel = guild.get_channel_or_thread(text_id) if guild and text_id else None
        if (
            not member
            or not member.voice
            or not member.voice.channel
            or member.voice.channel.id != voice_id
        ):
            raise PermissionError("Speaker is no longer in the active voice channel")
        if not text_channel or text_channel.guild.id != guild_id:
            raise PermissionError("Voice session output channel is unavailable")
        return member, text_channel

    async def _submit_transcript(self, guild_id, voice_id, user_id, text):
        bot = self.bot
        member, text_channel = self._validate_voice_source(guild_id, voice_id, user_id)
        now = datetime.now(timezone.utc)
        route = Route(
            self.name,
            f"{guild_id}:{voice_id}",
            guild_id,
            -voice_id,
            lambda output: self._deliver(text_channel, output),
        )
        status = bot.plugins.submit(
            InboundEvent(
                route, member.id, member.display_name,
                f"\u8a9e\u97f3\u8f49\u6587: {text[:1800]}", now, time.time_ns(),
                (
                "\u6b64\u8a0a\u606f\u4f86\u81ea\u8a9e\u97f3 ASR ",
                "\u8f49\u5beb\uff1b\u539f\u6587\u53ef\u80fd\u6709\u8fa8\u8b58\u932f\u8aa4\u3002",
                ),
                source=SimpleNamespace(channel=text_channel, author=member),
            )
        )
        if status == "queue_full":
            logger.warning("Voice transcript dropped because the conversation queue is full")
        return status

    async def _deliver(self, channel, text):
        import discord
        await channel.send(
            content=text,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )

    def _save_recording(self, guild_id, user_id, pcm):
        root = Path(self.config.get("recordings_directory", "data/voice-recordings"))
        if not root.is_absolute():
            root = self.bot.config.root / root
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{stamp}_{guild_id}_{user_id}_{secrets.token_hex(4)}.wav"
        (root / filename).write_bytes(pcm_to_wav(pcm))
