from __future__ import annotations

import asyncio
import io
import logging
import wave
from dataclasses import dataclass
from typing import Protocol

import requests
from openai import AsyncOpenAI

from .config import ModelSettings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ASRResult:
    text: str
    language: str | None = None
    confidence: float | None = None


class ASRProvider(Protocol):
    async def transcribe(self, pcm_data: bytes) -> ASRResult: ...


def pcm_to_wav(pcm_data: bytes) -> bytes:
    mono_pcm = bytearray(len(pcm_data) // 4 * 2)
    for index in range(0, len(pcm_data) - 3, 4):
        left = int.from_bytes(pcm_data[index:index + 2], "little", signed=True)
        right = int.from_bytes(pcm_data[index + 2:index + 4], "little", signed=True)
        average = max(-32768, min(32767, int((left + right) / 2)))
        mono_pcm.extend(average.to_bytes(2, "little", signed=True))

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(48000)
        wav_file.writeframes(bytes(mono_pcm))
    return wav_buffer.getvalue()


class OpenAICompatibleASR:
    def __init__(
        self,
        settings: ModelSettings,
        language: str | None = None,
        *,
        provider_name: str = "openai",
    ) -> None:
        self.settings = settings
        self.language = language or None
        self.provider_name = provider_name

    async def transcribe(self, pcm_data: bytes) -> ASRResult:
        if not pcm_data:
            return ASRResult("")
        if not self.settings.api_key:
            raise RuntimeError(f"{self.provider_name.upper()} ASR 未設定 API key")

        wav_data = pcm_to_wav(pcm_data)
        client = AsyncOpenAI(
            base_url=self.settings.url,
            api_key=self.settings.api_key,
        )
        try:
            kwargs = {
                "model": self.settings.name,
                "file": ("voice.wav", wav_data, "audio/wav"),
            }
            if self.language:
                kwargs["language"] = self.language

            result = await client.audio.transcriptions.create(
                response_format="text",
                **kwargs,
            )
            text = result.strip() if isinstance(result, str) else str(result).strip()
            return ASRResult(text=text, language=self.language)
        finally:
            await client.close()


class NvidiaASR(OpenAICompatibleASR):
    def __init__(self, settings: ModelSettings, language: str | None = None) -> None:
        super().__init__(
            settings,
            language,
            provider_name="nvidia",
        )


class ElevenLabsASR:
    def __init__(self, settings: ModelSettings, language: str | None = None) -> None:
        self.settings = settings
        self.language = language or None

    async def transcribe(self, pcm_data: bytes) -> ASRResult:
        if not pcm_data:
            return ASRResult("")
        if not self.settings.api_key:
            raise RuntimeError("ELEVENLABS ASR 未設定 API key")

        wav_data = pcm_to_wav(pcm_data)
        base_url = self.settings.url.rstrip("/")
        endpoint = f"{base_url}/speech-to-text"

        def _request() -> ASRResult:
            data: dict[str, str] = {"model_id": self.settings.name}
            if self.language:
                data["language_code"] = self.language
            response = requests.post(
                endpoint,
                headers={"xi-api-key": self.settings.api_key},
                data=data,
                files={"file": ("voice.wav", wav_data, "audio/wav")},
                timeout=60,
            )
            if not response.ok:
                detail = response.text.strip()
                raise RuntimeError(
                    f"ElevenLabs ASR HTTP {response.status_code}: {detail or 'unknown error'}"
                )
            payload = response.json()
            return ASRResult(
                text=str(payload.get("text", "")).strip(),
                language=payload.get("language_code") or self.language,
            )

        return await asyncio.to_thread(_request)


class ASRManager:
    def __init__(self, provider: ASRProvider) -> None:
        self.provider = provider

    async def transcribe(self, pcm_data: bytes) -> ASRResult:
        return await self.provider.transcribe(pcm_data)


def create_asr_manager(
    provider_name: str,
    settings: ModelSettings,
    language: str | None = None,
) -> ASRManager:
    normalized = provider_name.strip().lower()
    if normalized in {"nvidia", "nvidia-api"}:
        provider: ASRProvider = NvidiaASR(settings, language)
    elif normalized in {"elevenlabs", "eleven-labs", "11labs"}:
        provider = ElevenLabsASR(settings, language)
    elif normalized in {"openai", "openai-compatible"}:
        provider = OpenAICompatibleASR(settings, language)
    else:
        raise ValueError(f"不支援的 ASR provider: {provider_name}")
    return ASRManager(provider)
