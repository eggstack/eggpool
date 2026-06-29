"""Kilo integration renderer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


def build_kilo_openai_compatible_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build a JSON/OpenAI-compatible provider block for Kilo."""
    provider: dict[str, Any] = {
        "name": "EggPool",
        "apiBase": ctx.base_url,
        "apiKey": ctx.api_key,
        "models": {},
    }
    for m in ctx.models:
        mid = m["model_id"]
        entry: dict[str, Any] = {}
        limits = m.get("effective_limits", {})
        ctx_tokens = limits.get("context_tokens")
        if ctx_tokens is not None and ctx_tokens > 0:
            entry["context_length"] = ctx_tokens
        provider["models"][mid] = entry if entry else {}
    if model and model not in provider["models"]:
        provider["models"][model] = {}
    return json.dumps({"openai_compatible": provider}, indent=2, sort_keys=True)
