"""Stream usage extraction for OpenAI and Anthropic protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from eggpool.catalog.pricing import coerce_token_count
from eggpool.proxy.cost_reporting import (
    ProviderReportedCost,
    extract_provider_reported_cost,
)


def safe_dict(value: Any) -> dict[str, Any] | None:
    """Return value if it is a dict, else None."""
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return None


@dataclass(slots=True)
class StreamUsageResult:
    """Usage information extracted from a streaming response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    thinking_characters: int = 0
    is_complete: bool = False
    # Authoritative provider-reported billed cost, in microdollars.
    # ``None`` when the upstream did not expose an unambiguous value.
    # The finalizer prefers this over locally derived cost whenever it
    # is set so the dashboard reflects actual spend rather than a local
    # estimate.
    reported_cost_microdollars: int | None = None
    # Short label identifying the response field that produced
    # ``reported_cost_microdollars`` (e.g. ``usage.cost_usd`` or the
    # provider-prefixed alias). ``None`` when no provider cost was found.
    reported_cost_source: str | None = None


def _reported_cost(
    data: Any,
    *,
    provider_id: str | None,
    protocol: str,
) -> ProviderReportedCost | None:
    """Extract provider-reported cost from a usage payload.

    The parser is defensive: any unparseable structure returns ``None``
    and never raises, so a malformed cost field cannot break stream
    finalization.
    """
    return extract_provider_reported_cost(
        data,
        provider_id=provider_id,
        protocol=protocol,
    )


# Field names that map to input token counts in Anthropic-style payloads,
# in the order the extractor should try them. The first non-zero value
# wins so vendors that emit an authoritative canonical field override
# alias-style fields. ``prompt_tokens`` is the OpenAI convention that
# some Anthropic-compatible vendors adopt; ``input_tokens`` is the
# Anthropic convention; the cache fields are included because they
# sometimes appear without an accompanying ``input_tokens`` (a
# particularly common shape in MiniMax-style Anthropic-compatible
# vendors) and we want at least the cache component rather than
# silently dropping it.
_ANTHROPIC_INPUT_TOKEN_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "input_tokens_total",
    "prompt_token_count",
)


def _extract_anthropic_input_tokens(usage: dict[str, Any]) -> int:
    """Extract input tokens from an Anthropic-style usage dict.

    Tries the canonical ``input_tokens`` field first, then common alias
    keys, and finally sums cache read/creation fields as a last resort
    when no total is exposed. The alias keys are common across the
    Anthropic-compatible vendor ecosystem (MiniMax, OpenAI-compat-via-
    Anthropic, third-party gateways) and are not specific to any one
    provider.
    """
    for key in _ANTHROPIC_INPUT_TOKEN_KEYS:
        value = usage.get(key)
        if value is not None:
            coerced = coerce_token_count(value)
            if coerced > 0:
                return coerced
    return 0


def extract_openai_response_usage(
    data: dict[str, Any],
    *,
    provider_id: str | None = None,
) -> StreamUsageResult | None:
    """Extract usage from an OpenAI-compatible response JSON object."""
    usage_data = safe_dict(data.get("usage"))
    if not usage_data:
        return None

    prompt_details = safe_dict(usage_data.get("prompt_tokens_details"))
    completion_details = safe_dict(usage_data.get("completion_tokens_details"))
    reported = _reported_cost(data, provider_id=provider_id, protocol="openai")

    return StreamUsageResult(
        input_tokens=coerce_token_count(usage_data.get("prompt_tokens", 0)),
        output_tokens=coerce_token_count(usage_data.get("completion_tokens", 0)),
        cache_read_tokens=coerce_token_count(
            prompt_details.get("cached_tokens", 0) if prompt_details is not None else 0
        ),
        reasoning_tokens=coerce_token_count(
            completion_details.get("reasoning_tokens", 0)
            if completion_details is not None
            else 0
        ),
        is_complete=True,
        reported_cost_microdollars=(
            reported.microdollars if reported is not None else None
        ),
        reported_cost_source=reported.source if reported is not None else None,
    )


def extract_anthropic_response_usage(
    data: dict[str, Any],
    *,
    provider_id: str | None = None,
) -> StreamUsageResult | None:
    """Extract usage from an Anthropic-compatible response JSON object."""
    usage_data = safe_dict(data.get("usage"))
    if usage_data is None:
        return None

    reported = _reported_cost(data, provider_id=provider_id, protocol="anthropic")

    return StreamUsageResult(
        input_tokens=_extract_anthropic_input_tokens(usage_data),
        output_tokens=coerce_token_count(usage_data.get("output_tokens", 0)),
        cache_read_tokens=coerce_token_count(
            usage_data.get("cache_read_input_tokens", 0)
        ),
        cache_creation_tokens=coerce_token_count(
            usage_data.get("cache_creation_input_tokens", 0)
        ),
        is_complete=True,
        reported_cost_microdollars=(
            reported.microdollars if reported is not None else None
        ),
        reported_cost_source=reported.source if reported is not None else None,
    )


class OpenAIStreamUsageExtractor:
    """Extracts usage from OpenAI SSE stream events."""

    def __init__(self, *, provider_id: str | None = None) -> None:
        self._provider_id = provider_id

    def extract(self, data: dict[str, Any]) -> StreamUsageResult | None:
        """Extract usage from an OpenAI streaming chunk.

        OpenAI sends usage in the final chunk when
        stream_options.include_usage is set.
        """
        return extract_openai_response_usage(
            data,
            provider_id=self._provider_id,
        )


class AnthropicStreamUsageExtractor:
    """Extracts usage from Anthropic SSE stream events."""

    def __init__(self, *, provider_id: str | None = None) -> None:
        self._provider_id = provider_id
        # Tracks whether the most recent message_start yielded a
        # non-zero input token count. When the stream later emits
        # message_delta (or a vendor-equivalent end-of-stream event),
        # this flag tells us whether the canonical Anthropic input
        # token field was already accounted for. Vendors that omit
        # ``message_start.message.usage.input_tokens`` — but still
        # expose input tokens somewhere reachable from the event —
        # cause us to fall back to alternative paths on the next
        # usage-bearing event.
        self._saw_input_tokens = False

    def extract(self, data: dict[str, Any]) -> StreamUsageResult | None:
        """Extract usage from an Anthropic streaming event.

        Anthropic canonical events:
          * ``message_start``: ``{message: {usage: {input_tokens: ...}}}``
          * ``message_delta``: ``{usage: {output_tokens: ...}}``

        Many Anthropic-compatible vendors (MiniMax, gateway providers,
        and self-hosted Anthropic-shaped endpoints) diverge from the
        canonical shape in one of these ways:

          * ``message_start`` carries no ``message.usage`` block.
          * Input tokens appear on ``message_delta`` instead, sometimes
            nested under ``delta.usage`` or ``usage``.
          * Input tokens use OpenAI-style alias keys such as
            ``prompt_tokens`` or ``input_tokens_total``.

        The extractor falls back through every reachable usage payload
        in the current event so any of these shapes produce a populated
        ``input_tokens`` rather than a silently-zero dashboard cell.
        """
        event_type = data.get("type")

        if event_type == "message_start":
            message = safe_dict(data.get("message"))
            usage = safe_dict(message.get("usage")) if message is not None else None
            if usage is None:
                # No canonical usage block on message_start. Mark
                # input_tokens as still-unseen so the next usage-bearing
                # event (typically message_delta) is given the chance
                # to populate it from alternative paths.
                self._saw_input_tokens = False
                return None
            input_tokens = _extract_anthropic_input_tokens(usage)
            self._saw_input_tokens = input_tokens > 0
            return StreamUsageResult(
                input_tokens=input_tokens,
                cache_read_tokens=coerce_token_count(
                    usage.get("cache_read_input_tokens", 0)
                ),
                cache_creation_tokens=coerce_token_count(
                    usage.get("cache_creation_input_tokens", 0)
                ),
            )

        if event_type == "message_delta":
            usage = safe_dict(data.get("usage"))
            if usage is None:
                return None
            # message_delta is the canonical Anthropic location for
            # end-of-stream cost.  Inspect the parent event payload too,
            # because Anthropic vendors may place billing fields under
            # ``usage.billing`` rather than ``usage`` directly.
            reported = _reported_cost(
                data,
                provider_id=self._provider_id,
                protocol="anthropic",
            )
            output_tokens = coerce_token_count(usage.get("output_tokens", 0))
            input_tokens = 0
            if not self._saw_input_tokens:
                # Vendor fallback: input tokens did not arrive on
                # ``message_start`` (or the event was absent). Try to
                # recover them from the message_delta usage block itself
                # — both Anthropic-native and OpenAI-style alias keys
                # are inspected, so any shape that surfaces input on
                # the closing event produces a populated result.
                input_tokens = _extract_anthropic_input_tokens(usage)
                self._saw_input_tokens = input_tokens > 0
            return StreamUsageResult(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                is_complete=True,
                reported_cost_microdollars=(
                    reported.microdollars if reported is not None else None
                ),
                reported_cost_source=reported.source if reported is not None else None,
            )

        if event_type == "content_block_delta":
            delta = safe_dict(data.get("delta"))
            if delta is None:
                return None
            if delta.get("type") == "thinking":
                thinking = delta.get("thinking")
                chars = len(thinking) if isinstance(thinking, str) else 0
                return StreamUsageResult(
                    thinking_characters=chars,
                )

        return None
