"""Package resource utilities for bundled assets."""

from __future__ import annotations

from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable


def bundled_themes_dir() -> Traversable:
    """Return the bundled themes directory as a traversable resource."""
    return files("eggpool.dashboard").joinpath("themes")
