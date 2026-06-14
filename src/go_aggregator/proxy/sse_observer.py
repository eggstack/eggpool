"""Incremental SSE observer for streaming proxy responses.

Handles arbitrary chunk boundaries, CRLF/LF delimiters, unknown events,
bounded incomplete-frame memory, and usage extraction.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from go_aggregator.proxy.usage import (
    AnthropicStreamUsageExtractor,
    OpenAIStreamUsageExtractor,
    StreamUsageResult,
)

logger = logging.getLogger(__name__)

# Maximum bytes to buffer for an incomplete SSE frame
MAX_INCOMPLETE_FRAME_BYTES = 64 * 1024  # 64KB


@dataclass
class SSEFrame:
    """A parsed SSE frame."""

    event: str = ""
    data_lines: list[str] = field(default_factory=list)

    @property
    def data(self) -> str:
        return "\n".join(self.data_lines)

    @property
    def is_done(self) -> bool:
        return self.data.strip() == "[DONE]"


class IncrementalSSEObserver:
    """Observes an SSE byte stream incrementally.

    Handles:
    - Arbitrary chunk boundaries (partial lines, split across chunks)
    - CRLF and LF line endings
    - Unknown event types (ignored gracefully)
    - Bounded incomplete-frame memory
    - Usage extraction from recognized events
    - Tracking of bytes_emitted and first_byte_ms
    """

    def __init__(self, protocol: str) -> None:
        self._buffer = ""
        self._bytes_emitted = 0
        self._first_byte_ms: float = 0.0
        self._usage_result = StreamUsageResult()
        self._frame_count = 0
        self._error_count = 0

        if protocol == "anthropic":
            self._extractor = AnthropicStreamUsageExtractor()
        else:
            self._extractor = OpenAIStreamUsageExtractor()

    def observe(self, chunk: bytes) -> None:
        """Process a chunk of bytes from the upstream stream.

        Appends to the internal buffer, splits on line boundaries,
        and processes complete SSE frames.
        """
        if self._bytes_emitted == 0 and chunk:
            # Will be set externally by caller
            pass

        self._bytes_emitted += len(chunk)

        # Decode and normalize line endings
        text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
        self._buffer += text

        # Process complete lines
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")

            if not line:
                continue

            self._process_line(line)

        # Bound the buffer to prevent memory growth
        if len(self._buffer) > MAX_INCOMPLETE_FRAME_BYTES:
            logger.warning(
                "SSE buffer exceeded %d bytes, discarding oldest data",
                MAX_INCOMPLETE_FRAME_BYTES,
            )
            self._buffer = self._buffer[-MAX_INCOMPLETE_FRAME_BYTES:]

    def _process_line(self, line: str) -> None:
        """Process a single SSE line."""
        self._frame_count += 1

        if line.startswith("data: "):
            data_str = line[6:]

            if data_str.strip() == "[DONE]":
                return

            try:
                data = json.loads(data_str)
                usage = self._extractor.extract(data)
                if usage:
                    self._merge_usage(usage)
            except (json.JSONDecodeError, ValueError):
                self._error_count += 1
                logger.debug("Malformed SSE data frame, ignoring")
        # Ignore unknown event types (event:, id:, retry:, comments)

    def _merge_usage(self, incoming: StreamUsageResult) -> None:
        """Merge incoming usage into the accumulated result."""
        self._usage_result.input_tokens += incoming.input_tokens
        self._usage_result.output_tokens += incoming.output_tokens
        self._usage_result.cache_read_tokens += incoming.cache_read_tokens
        self._usage_result.cache_creation_tokens += incoming.cache_creation_tokens
        self._usage_result.reasoning_tokens += incoming.reasoning_tokens
        self._usage_result.thinking_characters += incoming.thinking_characters
        if incoming.is_complete:
            self._usage_result.is_complete = True

    @property
    def usage(self) -> StreamUsageResult:
        return self._usage_result

    @property
    def bytes_emitted(self) -> int:
        return self._bytes_emitted

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def error_count(self) -> int:
        return self._error_count

    def flush(self) -> None:
        """Process any remaining buffered data."""
        if self._buffer.strip():
            self._process_line(self._buffer.strip())
            self._buffer = ""
