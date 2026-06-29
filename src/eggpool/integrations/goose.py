"""Goose integration renderer."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


def build_goose_config_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build a config snippet for Goose."""
    default_model = model or (ctx.models[0]["model_id"] if len(ctx.models) == 1 else "")
    lines = [
        "[provider.eggpool]",
        f'base_url = "{ctx.base_url}"',
        f'api_key = "{ctx.api_key}"',
    ]
    if default_model:
        lines.append(f'default_model = "{default_model}"')
    return "\n".join(lines)


def build_goose_env_snippet(ctx: IntegrationContext, model: str | None = None) -> str:
    """Build shell export statements for Goose."""
    lines = [
        f"export GOOSE_PROVIDER__BASE_URL={shlex.quote(ctx.base_url)}",
        f"export GOOSE_PROVIDER__API_KEY={shlex.quote(ctx.api_key)}",
    ]
    if model:
        lines.append(f"export GOOSE_PROVIDER__MODEL={shlex.quote(model)}")
    return "\n".join(lines)
