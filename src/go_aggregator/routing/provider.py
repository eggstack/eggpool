"""Provider ID parsing utilities for model IDs."""

from __future__ import annotations

from go_aggregator.catalog.cache import parse_model_id


def parse_model_provider(model_id: str) -> tuple[str, str | None]:
    """Parse 'model-id/provider-id' into (model_id, provider_id).

    If no '/', returns (model_id, None).
    """
    return parse_model_id(model_id)


def format_model_provider(model_id: str, provider_id: str) -> str:
    """Format as 'model-id/provider-id'."""
    return f"{model_id}/{provider_id}"
