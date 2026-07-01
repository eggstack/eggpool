"""Usage-blob canonicalisation for cross-protocol cost attribution.

Both OpenAI and Anthropic return usage in slightly different shapes.
Canonicalisation normalises to a common internal representation so the
cost calculator and quota tracker can operate uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eggpool.transcoder.json_helpers import token_count_from

_ANTHROPIC_USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


@dataclass(frozen=True, slots=True)
class CanonicalUsage:
    """Normalised usage counts for a completed request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        """Serialise to a dict suitable for persistence."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }


def canonicalise_usage(
    raw: dict[str, Any],
    *,
    protocol: str,
) -> CanonicalUsage:
    """Normalise a raw usage blob from an upstream response.

    Parameters
    ----------
    raw:
        The ``usage`` object from the upstream JSON body.
    protocol:
        ``"openai"`` or ``"anthropic"`` — selects the normalisation path.

    Returns
    -------
    CanonicalUsage
        A protocol-agnostic usage representation.
    """
    if protocol == "anthropic":
        return _canonicalise_anthropic(raw)
    return _canonicalise_openai(raw)


def _canonicalise_openai(raw: dict[str, Any]) -> CanonicalUsage:
    prompt = token_count_from(raw, "prompt_tokens")
    completion = token_count_from(raw, "completion_tokens")
    total = token_count_from(raw, "total_tokens")
    if total == 0 and "total_tokens" not in raw:
        total = prompt + completion
    return CanonicalUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


def _canonicalise_anthropic(raw: dict[str, Any]) -> CanonicalUsage:
    input_tokens = token_count_from(raw, "input_tokens")
    output_tokens = token_count_from(raw, "output_tokens")
    total = input_tokens + output_tokens
    cache_creation = token_count_from(raw, "cache_creation_input_tokens")
    cache_read = token_count_from(raw, "cache_read_input_tokens")
    return CanonicalUsage(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )


def openai_usage_from_anthropic_usage(
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Render Anthropic usage as an OpenAI-compatible usage envelope.

    Anthropic separates fresh input, cache-read input, and cache-write
    input. OpenAI-compatible clients generally treat ``prompt_tokens`` as
    total prompt context, with cache details as a breakdown. Preserve that
    convention so clients display long cached-context sessions correctly.
    """
    input_tokens = token_count_from(raw, "input_tokens")
    output_tokens = token_count_from(raw, "output_tokens")
    cache_read_tokens = token_count_from(raw, "cache_read_input_tokens")
    cache_creation_tokens = token_count_from(raw, "cache_creation_input_tokens")
    prompt_tokens = input_tokens + cache_read_tokens + cache_creation_tokens

    usage: dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
    }
    if cache_read_tokens > 0 or cache_creation_tokens > 0:
        prompt_tokens_details: dict[str, int] = {}
        if cache_read_tokens > 0:
            prompt_tokens_details["cached_tokens"] = cache_read_tokens
        if cache_creation_tokens > 0:
            prompt_tokens_details["cache_creation_tokens"] = cache_creation_tokens
        usage["prompt_tokens_details"] = prompt_tokens_details
    return usage


def merge_anthropic_usage(
    *raw_values: dict[str, Any] | None,
) -> dict[str, int]:
    """Merge partial Anthropic usage blobs from separate stream events."""
    merged: dict[str, int] = {}
    for raw in raw_values:
        for field in _ANTHROPIC_USAGE_TOKEN_FIELDS:
            value = token_count_from(raw, field)
            if value > 0:
                merged[field] = max(merged.get(field, 0), value)
    return merged
