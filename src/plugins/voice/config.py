"""Voice-owned configuration validation."""
import re
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit


def validate_config(config):
    voice = config.get("plugins", "voice", {})
    if not isinstance(voice, dict):
        raise ValueError("plugins.voice must be a table")
    
    if type(voice.get("recordings_enabled", False)) is not bool:
        raise ValueError("plugins.voice.recordings_enabled must be a boolean")
    
    provider = voice.get("asr_provider", "openai-compatible")
    if provider not in {"openai-compatible", "nvidia", "elevenlabs"}:
        raise ValueError("Unknown voice ASR provider")
    
    if not voice.get("asr_model") or not voice.get("asr_api_key_env"):
        raise ValueError("Voice plugin requires asr_model and asr_api_key_env")
    
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", voice["asr_api_key_env"]):
        raise ValueError("asr_api_key_env must be an environment variable name")
    
    defaults = {
        "nvidia": "https://integrate.api.nvidia.com/v1", 
        "elevenlabs": "https://api.elevenlabs.io/v1", 
        "openai-compatible": "https://api.openai.com/v1"
    }
    endpoint = urlsplit(voice.get("asr_base_url", defaults[provider]))
    if not endpoint.hostname or endpoint.username or endpoint.password or not (endpoint.scheme == "https" or endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("Voice ASR URL must use HTTPS, except loopback development")
    
    for key, default in (("bridge_port", 8787), ("pcm_port", 8788)):
        value = voice.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError(f"plugins.voice.{key} must be a TCP port")
        
    if voice.get("bridge_port", 8787) == voice.get("pcm_port", 8788):
        raise ValueError("Voice bridge and PCM ports must differ")
    
    # silence = voice.get("silence_seconds", 1.5)
    # if isinstance(silence, bool) or not isinstance(silence, (int, float)) or not 0.25 <= silence <= 10:
    #     raise ValueError("plugins.voice.silence_seconds must be between 0.25 and 10")


def startup_check(config):
    voice = config.get("plugins", "voice", {})
    errors = []
    key = voice.get("asr_api_key_env")
    if key and not os.getenv(key):
        errors.append(f"Set {key} in .env for voice ASR")
    if not shutil.which("node"):
        errors.append("Voice support requires Node.js")
    plugin_dir = Path(__file__).resolve().parent
    if not all((plugin_dir / name).is_file() for name in ("voice_bridge.js", "package.json")):
        errors.append("Voice plugin bridge files are missing")
    return errors
