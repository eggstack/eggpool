"""Request token estimation and effective model-limit enforcement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from eggpool.errors import ContextLimitExceededError

if TYPE_CHECKING:
    from collections.abc import Mapping

ProtocolName = Literal["openai", "anthropic"]

MIN_ESTIMATED_INPUT_TOKENS = 1_000
MAX_ESTIMATED_INPUT_TOKENS = 128_000
ESTIMATED_BYTES_PER_TOKEN = 3


class ModelLimitCatalog(Protocol):
    """Catalog behavior needed to enforce request context limits."""

    def get_model_for_provider(
        self,
        model_id: str,
        provider_id: str | None,
    ) -> dict[str, Any] | None: ...


def estimate_input_tokens(body: bytes) -> int:
    """Estimate input tokens from serialized request bytes.

    The estimate is deliberately conservative and bounded so it is useful
    for both context-limit preflight and quota reservation sizing.
    """
    if not body:
        return MIN_ESTIMATED_INPUT_TOKENS
    estimate = max(MIN_ESTIMATED_INPUT_TOKENS, len(body) // ESTIMATED_BYTES_PER_TOKEN)
    return min(estimate, MAX_ESTIMATED_INPUT_TOKENS)


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
    model_info = catalog_cache.get_model_for_provider(model_id, provider_id)
    if model_info is None:
        return

    effective_value = model_info.get("effective_limits")
    if not isinstance(effective_value, dict) or not effective_value:
        return
    effective = cast("dict[str, Any]", effective_value)
    if not effective.get("enforce", True):
        return

    max_context = _positive_limit(effective.get("context_tokens"))
    max_input = _positive_limit(effective.get("input_tokens"))
    max_output = _positive_limit(effective.get("output_tokens"))
    if max_context is None and max_input is None and max_output is None:
        return

    estimated_input = estimate_input_tokens(body)
    requested_output = requested_output_tokens(payload, protocol)

    if max_input is not None and estimated_input > max_input:
        _raise_limit_error(
            model_id, estimated_input, requested_output, max_context, max_input
        )
    if (
        max_output is not None
        and requested_output is not None
        and requested_output > max_output
    ):
        _raise_limit_error(
            model_id, estimated_input, requested_output, max_context, max_input
        )
    if (
        max_context is not None
        and estimated_input + (requested_output or 0) > max_context
    ):
        _raise_limit_error(
            model_id, estimated_input, requested_output, max_context, max_input
        )


def _positive_limit(value: Any) -> int | None:
    """Normalize a positive integer limit from catalog metadata."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _raise_limit_error(
    model_id: str,
    estimated_input: int,
    requested_output: int | None,
    max_context: int | None,
    max_input: int | None,
) -> None:
    """Raise a consistently populated context-limit error."""
    raise ContextLimitExceededError(
        model_id=model_id,
        estimated_input_tokens=estimated_input,
        requested_output_tokens=requested_output,
        max_context_tokens=max_context,
        max_input_tokens=max_input,
    )
