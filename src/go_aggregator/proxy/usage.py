"""Stream usage extraction for OpenAI and Anthropic protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class StreamUsageResult:
    """Usage information extracted from a streaming response."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    is_complete: bool = False


class OpenAIStreamUsageExtractor:
    """Extracts usage from OpenAI SSE stream events."""

    def extract(self, data: dict[str, Any]) -> StreamUsageResult | None:
        """Extract usage from an OpenAI streaming chunk.

        OpenAI sends usage in the final chunk when
        stream_options.include_usage is set.
        """
        usage_data = data.get("usage")
        if not usage_data:
            return None

        return StreamUsageResult(
            input_tokens=usage_data.get("prompt_tokens", 0),
            output_tokens=usage_data.get("completion_tokens", 0),
            cache_read_tokens=usage_data.get("prompt_tokens_details", {}).get(
                "cached_tokens", 0
            ),
            reasoning_tokens=usage_data.get("completion_tokens_details", {}).get(
                "reasoning_tokens", 0
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
            message = data.get("message", {})
            usage = message.get("usage", {})
            return StreamUsageResult(
                input_tokens=usage.get("input_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            )

        if event_type == "message_delta":
            usage = data.get("usage", {})
            return StreamUsageResult(
                output_tokens=usage.get("output_tokens", 0),
                is_complete=True,
            )

        if event_type == "content_block_delta":
            delta = data.get("delta", {})
            if delta.get("type") == "thinking":
                # Reasoning tokens are in thinking blocks
                return StreamUsageResult(
                    reasoning_tokens=len(delta.get("thinking", "")),
                )

        return None
