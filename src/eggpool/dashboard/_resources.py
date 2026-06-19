"""Package resource utilities for bundled assets."""

from __future__ import annotations

from importlib.resources import as_file, files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def bundled_themes_dir() -> Path:
    """Return the path to the bundled themes directory."""
    ref = files("eggpool.dashboard").joinpath("themes")
    with as_file(ref) as path:
        return path
