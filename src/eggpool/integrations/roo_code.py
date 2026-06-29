"""Roo Code integration renderer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


def build_roo_code_profile_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build a JSON profile similar to Cline for Roo Code."""
    default_model = model or (ctx.models[0]["model_id"] if len(ctx.models) == 1 else "")
    profile: dict[str, Any] = {
        "apiProvider": "openai-compatible",
        "openAiBaseUrl": ctx.base_url,
        "openAiApiKey": ctx.api_key,
    }
    if default_model:
        profile["openAiModelId"] = default_model
    return json.dumps(profile, indent=2, sort_keys=True)
