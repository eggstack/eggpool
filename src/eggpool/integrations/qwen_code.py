"""Qwen Code integration renderer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from eggpool.integrations.common import IntegrationContext


def default_qwen_code_path(home: Path) -> Path:
    """Return the default Qwen Code config path."""
    return home / ".qwen" / "eggpool.json"


def build_qwen_code_provider_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build a JSON provider block for Qwen Code settings."""
    provider: dict[str, object] = {
        "name": "EggPool",
        "type": "openai",
        "base_url": ctx.base_url,
        "api_key": ctx.api_key,
    }
    if model:
        provider["model"] = model
    return json.dumps(provider, indent=2, sort_keys=True)
