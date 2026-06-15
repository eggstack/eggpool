"""Incremental SSE observer for streaming proxy responses.

Handles arbitrary chunk boundaries, CRLF/LF delimiters, unknown events,
bounded incomplete-frame memory, and usage extraction.
"""

from __future__ import annotations

import codecs
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
    data_lines: list[str] = field(default_factory=list[str])

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
    - Proper SSE event assembly: accumulates data lines across blank lines
    """

    def __init__(self, protocol: str) -> None:
        self._buffer = ""
        self._bytes_emitted = 0
        self._first_byte_ms: float = 0.0
        self._usage_result = StreamUsageResult()
        self._frame_count = 0
        self._error_count = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")()

        # Current event state (for assembling multi-line data)
        self._current_event = ""
        self._current_data_lines: list[str] = []

        if protocol == "anthropic":
            self._extractor = AnthropicStreamUsageExtractor()
        else:
            self._extractor = OpenAIStreamUsageExtractor()

    def observe(self, chunk: bytes) -> None:
        """Process a chunk of bytes from the upstream stream.

        Appends to the internal buffer, splits on line boundaries,
        and processes complete SSE frames.
        """
        self._bytes_emitted += len(chunk)

        # Decode and normalize line endings using incremental decoder
        text = self._decoder.decode(chunk).replace("\r\n", "\n")
        # Also normalize lone CR to LF
        text = text.replace("\r", "\n")
        self._buffer += text

        # Process complete lines
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)

            if not line:
                # Blank line: terminate the current SSE event
                self._flush_event()
                continue

            self._process_line(line)

        # Bound the buffer to prevent memory growth
        if len(self._buffer) > MAX_INCOMPLETE_FRAME_BYTES:
            logger.warning(
                "SSE buffer exceeded %d bytes, discarding oldest data",
                MAX_INCOMPLETE_FRAME_BYTES,
            )
            self._error_count += 1
            # Discard the current event state for the oversized frame
            self._current_event = ""
            self._current_data_lines.clear()
            self._buffer = self._buffer[-MAX_INCOMPLETE_FRAME_BYTES:]

    def _process_line(self, line: str) -> None:
        """Process a single SSE line."""
        self._frame_count += 1

        # Ignore comments beginning with ':'
        if line.startswith(":"):
            return

        # Parse field and optional value
        if ":" in line:
            field_name, _, value = line.partition(":")
            # Accept both "data:value" and "data: value"
            value = value.lstrip(" ") if field_name != "data" else value
            if field_name == "data":
                self._current_data_lines.append(value)
            elif field_name == "event":
                self._current_event = value
            # Ignore unknown fields (id:, retry:, etc.)
        else:
            # Line with no colon - treat as field with empty value
            pass

    def _flush_event(self) -> None:
        """Flush the current accumulated event for processing."""
        if not self._current_data_lines:
            return

        data = "\n".join(self._current_data_lines)
        self._current_data_lines.clear()
        self._current_event = ""

        if data.strip() == "[DONE]":
            return

        try:
            parsed = json.loads(data)
            usage = self._extractor.extract(parsed)
            if usage:
                self._merge_usage(usage)
        except (json.JSONDecodeError, ValueError):
            self._error_count += 1
            logger.debug("Malformed SSE data frame, ignoring")

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
        """Process any remaining buffered data.

        Flushes any pending event. Also flushes the incremental decoder
        for any incomplete multi-byte sequences.
        """
        # Flush the decoder for incomplete multi-byte sequences
        try:
            remainder = self._decoder.decode(b"", True)
        except UnicodeDecodeError:
            remainder = ""
        if remainder:
            self._buffer += remainder.replace("\r\n", "\n").replace("\r", "\n")

        # Process any remaining complete lines
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if not line:
                self._flush_event()
                continue
            self._process_line(line)

        # Flush any remaining partial event (no trailing blank line)
        self._flush_event()
        self._buffer = ""
