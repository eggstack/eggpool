"""Goose integration renderer."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from eggpool.integrations.common import (
    render_toml_string,
    resolve_optional_model,
)

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


def build_goose_config_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build a config snippet for Goose."""
    default_model = resolve_optional_model(ctx, model)
    lines = [
        "[provider.eggpool]",
        f"base_url = {render_toml_string(ctx.base_url)}",
        f"api_key = {render_toml_string(ctx.api_key)}",
    ]
    if default_model:
        lines.append(f"default_model = {render_toml_string(default_model)}")
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
