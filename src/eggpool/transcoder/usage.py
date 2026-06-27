"""Usage-blob canonicalisation for cross-protocol cost attribution.

Both OpenAI and Anthropic return usage in slightly different shapes.
Canonicalisation normalises to a common internal representation so the
cost calculator and quota tracker can operate uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    prompt = int(raw.get("prompt_tokens", 0))
    completion = int(raw.get("completion_tokens", 0))
    total = int(raw.get("total_tokens", prompt + completion))
    return CanonicalUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


def _canonicalise_anthropic(raw: dict[str, Any]) -> CanonicalUsage:
    input_tokens = int(raw.get("input_tokens", 0))
    output_tokens = int(raw.get("output_tokens", 0))
    total = input_tokens + output_tokens
    cache_creation = int(raw.get("cache_creation_input_tokens", 0))
    cache_read = int(raw.get("cache_read_input_tokens", 0))
    return CanonicalUsage(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=total,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )
