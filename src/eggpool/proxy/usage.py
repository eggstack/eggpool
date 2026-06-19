"""Stream usage extraction for OpenAI and Anthropic protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from eggpool.catalog.pricing import coerce_token_count


def safe_dict(value: Any) -> dict[str, Any] | None:
    """Return value if it is a dict, else None."""
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return None


@dataclass
class StreamUsageResult:
    """Usage information extracted from a streaming response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    thinking_characters: int = 0
    is_complete: bool = False


class OpenAIStreamUsageExtractor:
    """Extracts usage from OpenAI SSE stream events."""

    def extract(self, data: dict[str, Any]) -> StreamUsageResult | None:
        """Extract usage from an OpenAI streaming chunk.

        OpenAI sends usage in the final chunk when
        stream_options.include_usage is set.
        """
        usage_data = safe_dict(data.get("usage"))
        if not usage_data:
            return None

        prompt_details = safe_dict(usage_data.get("prompt_tokens_details"))
        completion_details = safe_dict(usage_data.get("completion_tokens_details"))

        return StreamUsageResult(
            input_tokens=coerce_token_count(usage_data.get("prompt_tokens", 0)),
            output_tokens=coerce_token_count(usage_data.get("completion_tokens", 0)),
            cache_read_tokens=coerce_token_count(
                prompt_details.get("cached_tokens", 0)
                if prompt_details is not None
                else 0
            ),
            reasoning_tokens=coerce_token_count(
                completion_details.get("reasoning_tokens", 0)
                if completion_details is not None
                else 0
            ),
            is_complete=True,
        )


class AnthropicStreamUsageExtractor:
    """Extracts usage from Anthropic SSE stream events."""

    def extract(self, data: dict[str, Any]) -> StreamUsageResult | None:
        """Extract usage from an Anthropic streaming event.

        Anthropic sends a message_delta event with usage at the end of the stream.
        Format: {"type": "message_delta", "usage": {"output_tokens": ...}}
        And a message_start event with input usage.
        Format: {"type": "message_start", "message": {"usage": {"input_tokens": ...}}}
        """
        event_type = data.get("type")

        if event_type == "message_start":
            message = safe_dict(data.get("message"))
            if message is None:
                return None
            usage = safe_dict(message.get("usage"))
            if usage is None:
                return None
            return StreamUsageResult(
                input_tokens=coerce_token_count(usage.get("input_tokens", 0)),
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
            return StreamUsageResult(
                output_tokens=coerce_token_count(usage.get("output_tokens", 0)),
                is_complete=True,
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
