"""Provider-aware model ID parsing and formatting utilities."""

from __future__ import annotations


def parse_model_provider(
    model_id: str, known_providers: set[str] | None = None
) -> tuple[str, str | None]:
    """Parse 'model-id/provider-id' into (model_id, provider_id).

    If no configured provider suffix is present, returns (model_id, None).
    """
    normalized = model_id.strip()
    if "/" in normalized:
        base, candidate = normalized.rsplit("/", 1)
        if (
            base
            and candidate
            and (known_providers is None or candidate in known_providers)
        ):
            return base, candidate
    return normalized, None


def format_model_provider(model_id: str, provider_id: str) -> str:
    """Format as 'model-id/provider-id'."""
    return f"{model_id}/{provider_id}"
