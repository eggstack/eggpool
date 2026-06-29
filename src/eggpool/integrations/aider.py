"""Aider integration renderer."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from eggpool.integrations.common import IntegrationContext


def default_aider_path(cwd: Path) -> Path:
    """Return the default Aider env file path."""
    return cwd / ".env.eggpool"


def build_aider_env_snippet(ctx: IntegrationContext, model: str | None = None) -> str:
    """Build shell export statements for Aider.

    Returns lines setting OPENAI_API_KEY, OPENAI_API_BASE, and
    optionally an aider ``--model`` flag.
    """
    lines = [
        f"export OPENAI_API_KEY={shlex.quote(ctx.api_key)}",
        f"export OPENAI_API_BASE={shlex.quote(ctx.base_url)}",
    ]
    if model:
        lines.append(f"aider --model {shlex.quote(model)}")
    return "\n".join(lines)
