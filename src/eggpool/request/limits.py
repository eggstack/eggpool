"""Request token estimation and effective model-limit enforcement."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, cast

from eggpool.errors import ContextLimitExceededError

if TYPE_CHECKING:
    from eggpool.catalog.limits import EffectiveModelLimits
    from eggpool.catalog.protocols import ProtocolName

MIN_ESTIMATED_INPUT_TOKENS = 1_000
MAX_ESTIMATED_INPUT_TOKENS = 128_000
ESTIMATED_BYTES_PER_TOKEN = 3
ESTIMATED_TEXT_CHARS_PER_TOKEN = 4
ESTIMATED_NON_ASCII_BYTES_PER_TOKEN = 2
ESTIMATED_CONTEXT_BYTES_PER_TOKEN_FLOOR = ESTIMATED_TEXT_CHARS_PER_TOKEN * 2


class ModelLimitCatalog(Protocol):
    """Catalog behavior needed to enforce request context limits."""

    def get_effective_limits(
        self,
        model_id: str,
        provider_id: str | None,
    ) -> EffectiveModelLimits | None: ...


def estimate_input_tokens(body: bytes) -> int:
    """Conservatively estimate input tokens from serialized request bytes.

    This estimate is intentionally not capped: context-limit enforcement must
    preserve the size relationship for arbitrarily large request bodies.
    """
    if not body:
        return MIN_ESTIMATED_INPUT_TOKENS
    byte_estimate = (len(body) + ESTIMATED_BYTES_PER_TOKEN - 1) // (
        ESTIMATED_BYTES_PER_TOKEN
    )
    return max(MIN_ESTIMATED_INPUT_TOKENS, byte_estimate)


def estimate_reservation_tokens(body: bytes) -> int:
    """Estimate input tokens while bounding speculative quota reservations."""
    return min(estimate_input_tokens(body), MAX_ESTIMATED_INPUT_TOKENS)


def estimate_context_input_tokens(
    body: bytes,
    payload: Mapping[str, Any],
) -> int:
    """Estimate request input tokens for context-limit enforcement.

    The wire JSON body materially overstates large text prompts because
    escaping and object syntax are counted as request bytes even though they
    are not model input tokens.  Use decoded JSON values for the context
    guardrail while keeping the older byte estimator for quota reservations.
    """
    payload_estimate = _estimate_json_value_tokens(payload)
    byte_floor = _ceil_div(len(body), ESTIMATED_CONTEXT_BYTES_PER_TOKEN_FLOOR)
    return max(MIN_ESTIMATED_INPUT_TOKENS, payload_estimate, byte_floor)


def requested_output_tokens(
    payload: Mapping[str, Any],
    protocol: ProtocolName,
) -> int | None:
    """Return the first valid requested output-token limit, if present.

    Token limits must be positive integers. In particular, booleans are not
    accepted even though ``bool`` is an ``int`` subclass in Python.
    """
    keys = (
        ("max_tokens",)
        if protocol == "anthropic"
        else ("max_completion_tokens", "max_tokens")
    )
    for key in keys:
        value = _positive_limit(payload.get(key))
        if value is not None:
            return value
    return None


def check_context_limits(
    *,
    model_id: str,
    provider_id: str | None,
    body: bytes,
    payload: Mapping[str, Any],
    protocol: ProtocolName,
    catalog_cache: ModelLimitCatalog,
) -> None:
    """Reject requests that exceed a model's configured effective limits."""
    effective = catalog_cache.get_effective_limits(model_id, provider_id)
    if effective is None or not effective.enforce:
        return

    max_context = _positive_limit(effective.context_tokens)
    max_input = _positive_limit(effective.input_tokens)
    max_output = _positive_limit(effective.output_tokens)
    if max_context is None and max_input is None and max_output is None:
        return

    estimated_input = estimate_context_input_tokens(body, payload)
    requested_output = requested_output_tokens(payload, protocol)

    if max_input is not None and estimated_input > max_input:
        _raise_limit_error(
            model_id,
            estimated_input,
            requested_output,
            max_context,
            max_input,
            max_output,
        )
    if (
        max_output is not None
        and requested_output is not None
        and requested_output > max_output
    ):
        _raise_limit_error(
            model_id,
            estimated_input,
            requested_output,
            max_context,
            max_input,
            max_output,
        )
    if (
        max_context is not None
        and estimated_input + (requested_output or 0) > max_context
    ):
        _raise_limit_error(
            model_id,
            estimated_input,
            requested_output,
            max_context,
            max_input,
            max_output,
        )


def _positive_limit(value: Any) -> int | None:
    """Normalize a positive integer limit from catalog metadata."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _estimate_json_value_tokens(value: object) -> int:
    """Estimate tokens represented by a decoded JSON-compatible value."""
    if isinstance(value, str):
        return _estimate_string_tokens(value)
    if isinstance(value, Mapping):
        mapping = cast("Mapping[object, object]", value)
        total = 4
        for key, child in mapping.items():
            total += _estimate_string_tokens(str(key)) + _estimate_json_value_tokens(
                child
            )
            total += 2
        return total
    if isinstance(value, list):
        items = cast("list[object]", value)
        return 2 + sum(_estimate_json_value_tokens(item) + 1 for item in items)
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, (int, float)):
        return max(1, _ceil_div(len(str(value)), ESTIMATED_TEXT_CHARS_PER_TOKEN))
    return max(1, _ceil_div(len(str(value)), ESTIMATED_TEXT_CHARS_PER_TOKEN))


def _estimate_string_tokens(value: str) -> int:
    """Estimate tokens for raw decoded text.

    ASCII-heavy code and prose average near four characters per token.  For
    non-ASCII text, use UTF-8 byte weight so compact scripts are not treated
    as four characters per token.
    """
    if not value:
        return 0
    ascii_chars = 0
    non_ascii_bytes = 0
    for char in value:
        if ord(char) < 128:
            ascii_chars += 1
        else:
            non_ascii_bytes += len(char.encode("utf-8"))
    return _ceil_div(
        ascii_chars,
        ESTIMATED_TEXT_CHARS_PER_TOKEN,
    ) + _ceil_div(non_ascii_bytes, ESTIMATED_NON_ASCII_BYTES_PER_TOKEN)


def _ceil_div(value: int, divisor: int) -> int:
    """Integer ceiling division for non-negative values."""
    return (value + divisor - 1) // divisor


def _raise_limit_error(
    model_id: str,
    estimated_input: int,
    requested_output: int | None,
    max_context: int | None,
    max_input: int | None,
    max_output: int | None,
) -> None:
    """Raise a consistently populated context-limit error."""
    raise ContextLimitExceededError(
        model_id=model_id,
        estimated_input_tokens=estimated_input,
        requested_output_tokens=requested_output,
        max_context_tokens=max_context,
        max_input_tokens=max_input,
        max_output_tokens=max_output,
    )
