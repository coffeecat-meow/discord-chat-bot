"""ASR providers for the optional voice plugin."""

from __future__ import annotations

import io
import os
import wave
from dataclasses import dataclass

from openai import AsyncOpenAI

from sayuki_bot.api import Config


@dataclass(frozen=True)
class ASRResult:
    text: str
    language: str | None = None


def pcm_to_wav(pcm: bytes) -> bytes:
    """Convert Discord's 48 kHz stereo signed 16-bit PCM to mono WAV."""
    usable = len(pcm) - len(pcm) % 4
    mono = bytearray()
    for offset in range(0, usable, 4):
        left = int.from_bytes(pcm[offset : offset + 2], "little", signed=True)
        right = int.from_bytes(pcm[offset + 2 : offset + 4], "little", signed=True)
        mono.extend(int((left + right) / 2).to_bytes(2, "little", signed=True))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(mono)
    return output.getvalue()


class ASRProvider:
    def __init__(self, config: Config):
        self.config = config

    async def transcribe(self, pcm: bytes) -> ASRResult:
        if not pcm:
            return ASRResult("")
        section = self.config.get("plugins", "voice", {})
        key = os.environ.get(section["asr_api_key_env"], "")
        if not key:
            raise RuntimeError(f"Set {section['asr_api_key_env']} for voice ASR")
        wav = pcm_to_wav(pcm)
        if section.get("asr_provider", "openai-compatible") == "elevenlabs":
            return await self._elevenlabs(section, key, wav)
        defaults = {
            "nvidia": "https://integrate.api.nvidia.com/v1",
            "openai-compatible": "https://api.openai.com/v1",
        }
        base_url = section.get(
            "asr_base_url",
            defaults.get(section.get("asr_provider"), "https://api.openai.com/v1"),
        )
        client = AsyncOpenAI(base_url=base_url, api_key=key, timeout=60)
        try:
            args = {"model": section["asr_model"], "file": ("voice.wav", wav, "audio/wav")}
            if section.get("asr_language"):
                args["language"] = section["asr_language"]
            result = await client.audio.transcriptions.create(response_format="text", **args)
            return ASRResult(result.strip() if isinstance(result, str) else str(result).strip())
        finally:
            await client.close()

    async def _elevenlabs(self, section, key, wav):
        import aiohttp

        base = section.get("asr_base_url", "https://api.elevenlabs.io/v1").rstrip("/")
        data = aiohttp.FormData()
        data.add_field("model_id", section["asr_model"])
        if section.get("asr_language"):
            data.add_field("language_code", section["asr_language"])
        data.add_field("file", wav, filename="voice.wav", content_type="audio/wav")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            async with session.post(
                f"{base}/speech-to-text", headers={"xi-api-key": key}, data=data
            ) as response:
                if response.status >= 400:
                    raise RuntimeError(f"ElevenLabs ASR HTTP {response.status}")
                result = await response.json()
                return ASRResult(str(result.get("text", "")).strip(), result.get("language_code"))
