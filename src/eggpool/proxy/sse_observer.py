"""Incremental SSE observer for streaming proxy responses.

Handles arbitrary chunk boundaries, CRLF/LF delimiters, unknown events,
bounded incomplete-frame memory, and usage extraction.
"""

from __future__ import annotations

import codecs
import json
import logging
from dataclasses import dataclass, field

from eggpool.proxy.usage import (
    AnthropicStreamUsageExtractor,
    OpenAIStreamUsageExtractor,
    StreamUsageResult,
    safe_dict,
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

    Instances are intended to be driven from a single coroutine
    (the streaming response generator). The asyncio event loop
    serializes access within that coroutine, so explicit
    synchronization is not required.
    """

    def __init__(self, protocol: str) -> None:
        self._protocol = protocol
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
        lines = (self._buffer + text).split("\n")
        self._buffer = lines.pop()

        # Process complete lines in one linear pass. Repeatedly splitting the
        # remaining buffer copied the tail once per SSE line.
        for line in lines:
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
            # Flush the decoder first so any incomplete multi-byte
            # UTF-8 sequence it is holding is appended to the buffer
            # before we truncate it. Otherwise resetting the decoder
            # alone would drop the partial bytes silently.
            try:
                remainder = self._decoder.decode(b"", True)
            except UnicodeDecodeError:
                self._error_count += 1
                logger.debug("Incremental decoder in error state at buffer truncation")
                remainder = ""
            if remainder:
                self._buffer += remainder.replace("\r\n", "\n").replace("\r", "\n")
            # Drop the oldest data from the front, advancing past the
            # next newline so we never split a multi-byte UTF-8
            # character at the new boundary. Reset the decoder so any
            # UTF-8 sequence split by the boundary does not leave the
            # decoder in an inconsistent state.
            drop_at = self._buffer.find(
                "\n", len(self._buffer) - MAX_INCOMPLETE_FRAME_BYTES
            )
            if drop_at != -1:
                self._buffer = self._buffer[drop_at + 1 :]
            else:
                self._buffer = self._buffer[-MAX_INCOMPLETE_FRAME_BYTES:]
            self._decoder = codecs.getincrementaldecoder("utf-8")()

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
            value = value.lstrip(" ")
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

        # OpenAI emits usage only in the final usage chunk. Ordinary content
        # chunks always carry choices, so avoid decoding their JSON solely for
        # telemetry. Malformed and unusual frames still take the validating path.
        if self._protocol == "openai" and '"usage"' not in data and '"choices"' in data:
            return

        try:
            parsed_raw = json.loads(data)
            parsed = safe_dict(parsed_raw)
            if parsed is None:
                self._error_count += 1
                logger.debug(
                    "SSE data frame is not a JSON object (type=%s), ignoring",
                    type(parsed_raw).__name__,
                )
                return
            usage = self._extractor.extract(parsed)
            if usage:
                self._merge_usage(usage)
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
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
            self._error_count += 1
            logger.debug("Incremental decoder in error state at flush")
            remainder = ""
        if remainder:
            self._buffer += remainder.replace("\r\n", "\n").replace("\r", "\n")

        # Process any remaining complete lines
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        for line in lines:
            if not line:
                self._flush_event()
                continue
            self._process_line(line)

        # Flush any remaining partial event (no trailing blank line)
        self._flush_event()
        self._buffer = ""
