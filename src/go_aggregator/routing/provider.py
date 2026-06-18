"""Provider ID parsing utilities for model IDs."""

from __future__ import annotations


def parse_model_provider(model_id: str) -> tuple[str, str | None]:
    """Parse 'model-id/provider-id' into (model_id, provider_id).

    If no '/', returns (model_id, None).
    """
    if "/" in model_id:
        parts = model_id.rsplit("/", 1)
        return parts[0], parts[1]
    return model_id, None


def format_model_provider(model_id: str, provider_id: str) -> str:
    """Format as 'model-id/provider-id'."""
    return f"{model_id}/{provider_id}"
