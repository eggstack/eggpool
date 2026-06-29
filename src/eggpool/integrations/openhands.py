"""OpenHands integration renderer."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


def build_openhands_config_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build a TOML/env config snippet for OpenHands."""
    default_model = model or (ctx.models[0]["model_id"] if len(ctx.models) == 1 else "")
    lines = [
        "[llm]",
        f'base_url = "{ctx.base_url}"',
        f'api_key = "{ctx.api_key}"',
        f'model = "{default_model}"',
    ]
    return "\n".join(lines)


def build_openhands_env_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build shell export statements for OpenHands."""
    default_model = model or (ctx.models[0]["model_id"] if len(ctx.models) == 1 else "")
    lines = [
        f"export LLM_BASE_URL={shlex.quote(ctx.base_url)}",
        f"export LLM_API_KEY={shlex.quote(ctx.api_key)}",
    ]
    if default_model:
        lines.append(f"export LLM_MODEL={shlex.quote(default_model)}")
    return "\n".join(lines)
