"""SSE streaming relay for OpenAI and Anthropic protocols."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from go_aggregator.proxy.usage import (
    AnthropicStreamUsageExtractor,
    OpenAIStreamUsageExtractor,
    StreamUsageResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

logger = logging.getLogger(__name__)


@dataclass
class StreamMetrics:
    """Metrics collected during a streaming response."""

    first_byte_ms: float = 0.0
    total_bytes: int = 0
    chunk_count: int = 0
    start_time: float = field(default_factory=time.time)
    usage: StreamUsageResult | None = None


async def relay_streaming_response(
    upstream_response: httpx.Response,
    protocol: str,
    request_id: str,
) -> AsyncIterator[tuple[str, StreamMetrics]]:
    """Relay an SSE streaming response from upstream to downstream.

    Yields tuples of (chunk_line, current_metrics) after each chunk is processed.
    The final yield includes the completed metrics with usage data.

    This function:
    1. Forwards chunks as they arrive (byte-preserving)
    2. Tracks first-byte timing
    3. Extracts usage from stream events
    4. Handles client disconnection
    """
    metrics = StreamMetrics()

    # Select the appropriate usage extractor
    if protocol == "anthropic":
        extractor = AnthropicStreamUsageExtractor()
    else:
        extractor = OpenAIStreamUsageExtractor()

    first_byte_received = False

    try:
        async for chunk in upstream_response.aiter_lines():
            if not first_byte_received:
                metrics.first_byte_ms = (time.time() - metrics.start_time) * 1000
                first_byte_received = True

            metrics.chunk_count += 1
            metrics.total_bytes += len(chunk.encode("utf-8"))

            # Try to extract usage from the chunk
            try:
                if chunk.startswith("data: "):
                    data_str = chunk[6:]
                    if data_str.strip() == "[DONE]":
                        # Final message
                        yield chunk, metrics
                        continue
                    data = json.loads(data_str)
                    usage = extractor.extract(data)
                    if usage:
                        # Merge usage results
                        if metrics.usage is None:
                            metrics.usage = usage
                        else:
                            metrics.usage.input_tokens += usage.input_tokens
                            metrics.usage.output_tokens += usage.output_tokens
                            metrics.usage.cache_read_tokens += usage.cache_read_tokens
                            metrics.usage.cache_creation_tokens += (
                                usage.cache_creation_tokens
                            )
                            metrics.usage.reasoning_tokens += usage.reasoning_tokens
                            if usage.is_complete:
                                metrics.usage.is_complete = True
            except (json.JSONDecodeError, ValueError):
                pass

            # Forward the chunk as-is
            yield chunk, metrics

    except asyncio.CancelledError:
        logger.info("Stream cancelled for request %s", request_id)
        raise
    except Exception:
        logger.exception("Error streaming for request %s", request_id)
        raise
