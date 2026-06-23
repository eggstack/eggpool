"""EggPool - Python proxy that aggregates OpenCode Go subscriptions."""

from __future__ import annotations

from importlib.metadata import version as _get_version

try:
    __version__ = _get_version("eggpool")
except Exception:
    __version__ = "0.0.0"
