"""Provider-aware model ID parsing and formatting utilities."""

from __future__ import annotations


def parse_model_provider(
    model_id: str, known_providers: set[str] | None = None
) -> tuple[str, str | None]:
    """Parse 'model-id/provider-id' into (model_id, provider_id).

    Splits on the final ``/``. The suffix is only treated as a
    ``provider_id`` when ``known_providers`` is supplied and the suffix
    matches one of them; otherwise the input is returned unchanged so
    the caller can produce a proper ``ModelNotFoundError`` with the
    original model string.

    Leading/trailing whitespace is stripped from the input. Empty base
    or candidate segments (e.g. ``"foo/"`` or ``"/foo"``) are treated as
    not having a provider suffix and the whole string is returned as
    the model id.
    """
    normalized = model_id.strip()
    if "/" not in normalized:
        return normalized, None

    base, candidate = normalized.rsplit("/", 1)
    if not base or not candidate:
        return normalized, None
    if known_providers is not None and candidate not in known_providers:
        return normalized, None
    return base, candidate


def format_model_provider(model_id: str, provider_id: str) -> str:
    """Format as 'model-id/provider-id'."""
    return f"{model_id}/{provider_id}"
