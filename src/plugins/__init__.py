"""Formal plugin package. Each child package is an independently toggleable plugin."""
from .api import InboundEvent, Plugin, Route
from .manager import PluginManager

__all__ = ("InboundEvent", "Plugin", "PluginManager", "Route")
