"""Optional installation step: build fonts before time-limited math workers run."""

from sayuki_bot.tools.math_tools import _sanitize_worker_environment

_sanitize_worker_environment()
from matplotlib import font_manager

print(f"Math font cache ready: {len(font_manager.fontManager.ttflist)} fonts")
