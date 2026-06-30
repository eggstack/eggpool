"""Cline integration renderer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from eggpool.integrations.common import resolve_optional_model

if TYPE_CHECKING:
    from eggpool.integrations.common import IntegrationContext


def build_cline_profile_snippet(
    ctx: IntegrationContext, model: str | None = None
) -> str:
    """Build a JSON profile with provider type, base URL, and key guidance."""
    default_model = resolve_optional_model(ctx, model)
    profile: dict[str, Any] = {
        "apiProvider": "openai-compatible",
        "openAiBaseUrl": ctx.base_url,
        "openAiApiKey": ctx.api_key,
    }
    if default_model:
        profile["openAiModelId"] = default_model
    return json.dumps(profile, indent=2, sort_keys=True)
