"""Voice plugin adapter. Configuration lives under [plugins.voice]."""
from .plugin import VoicePlugin
from .config import validate_config


def create_plugin(bot):
    return VoicePlugin(bot)
