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

# Maximum UTF-8 bytes to retain for an incomplete SSE line or event.
MAX_INCOMPLETE_FRAME_BYTES = 64 * 1024  # 64KB


def _utf8_len(value: str) -> int:
    """Return the UTF-8 byte length without allocating for ASCII strings."""
    return len(value) if value.isascii() else len(value.encode("utf-8"))


@dataclass(slots=True)
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

    def __init__(self, protocol: str, *, provider_id: str | None = None) -> None:
        self._protocol = protocol
        self._provider_id = provider_id
        self._buffer = ""
        self._bytes_emitted = 0
        self._usage_result = StreamUsageResult()
        self._frame_count = 0
        self._error_count = 0
        # Telemetry must never terminate an otherwise valid byte stream.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending_cr = False
        self._discarding_incomplete_line = False

        # Current event state (for assembling multi-line data)
        self._current_data_lines: list[str] = []
        self._current_event_bytes = 0
        self._discarding_event = False

        if protocol == "anthropic":
            self._extractor = AnthropicStreamUsageExtractor(provider_id=provider_id)
        else:
            self._extractor = OpenAIStreamUsageExtractor(provider_id=provider_id)

    def observe(self, chunk: bytes) -> None:
        """Process a chunk of bytes from the upstream stream.

        Appends to the internal buffer, splits on line boundaries,
        and processes complete SSE frames.
        """
        self._bytes_emitted += len(chunk)

        # Decode and normalize line endings using incremental decoder
        text = self._decoder.decode(chunk)
        # Preserve a trailing CR until the next chunk so a CRLF split at the
        # transport boundary is normalized to one newline, not two.
        if self._pending_cr:
            if text.startswith("\n"):
                text = text[1:]
            text = "\n" + text
            self._pending_cr = False
        if text.endswith("\r"):
            text = text[:-1]
            self._pending_cr = True
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = (self._buffer + text).split("\n")
        incomplete_line = lines.pop()

        # Process complete lines in one linear pass. Repeatedly splitting the
        # remaining buffer copied the tail once per SSE line.
        for line in lines:
            if self._discarding_incomplete_line:
                # The beginning of this line was discarded after exceeding the
                # memory limit. Ignore its remainder rather than interpreting a
                # truncated suffix as a new SSE field.
                self._discarding_incomplete_line = False
                continue
            if not line:
                # Blank line: terminate the current SSE event
                self._flush_event()
                continue

            self._process_line(line)

        # While discarding an oversized line, retain nothing until its newline
        # arrives. Otherwise retain the one incomplete line for the next chunk.
        self._buffer = "" if self._discarding_incomplete_line else incomplete_line

        # Bound by encoded bytes, not Python characters. A non-ASCII character
        # can occupy up to four UTF-8 bytes.
        if _utf8_len(self._buffer) > MAX_INCOMPLETE_FRAME_BYTES:
            logger.warning(
                "SSE line exceeded %d bytes, discarding it",
                MAX_INCOMPLETE_FRAME_BYTES,
            )
            self._error_count += 1
            self._buffer = ""
            self._discarding_incomplete_line = True
            self._reset_event(discarding=True)

    def _process_line(self, line: str) -> None:
        """Process a single SSE line."""
        self._frame_count += 1

        if self._discarding_event:
            return

        # Ignore comments beginning with ':'
        if line.startswith(":"):
            return

        # Parse field and optional value
        if ":" in line:
            field_name, _, value = line.partition(":")
            # Accept both "data:value" and "data: value"
            value = value.lstrip(" ")
            if field_name == "data":
                separator_bytes = 1 if self._current_data_lines else 0
                event_bytes = (
                    self._current_event_bytes + separator_bytes + _utf8_len(value)
                )
                if event_bytes > MAX_INCOMPLETE_FRAME_BYTES:
                    logger.warning(
                        "SSE event exceeded %d bytes, discarding telemetry",
                        MAX_INCOMPLETE_FRAME_BYTES,
                    )
                    self._error_count += 1
                    self._reset_event(discarding=True)
                    return
                self._current_data_lines.append(value)
                self._current_event_bytes = event_bytes
            # Ignore unknown fields (id:, retry:, event:, etc.)
        else:
            # Line with no colon - treat as field with empty value
            pass

    def _flush_event(self) -> None:
        """Flush the current accumulated event for processing."""
        if self._discarding_event:
            self._reset_event()
            return
        if not self._current_data_lines:
            return

        data = "\n".join(self._current_data_lines)
        self._reset_event()

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

    def _reset_event(self, *, discarding: bool = False) -> None:
        """Release accumulated event data and set its discard state."""
        self._current_data_lines.clear()
        self._current_event_bytes = 0
        self._discarding_event = discarding

    def _merge_usage(self, incoming: StreamUsageResult) -> None:
        """Merge incoming usage into the accumulated result.

        Token counters are additive. Provider-reported cost is replaced
        with the latest non-null value when the incoming event is
        marked ``is_complete``; otherwise the existing authoritative
        value is preserved. This matches the convention that final
        stream events supersede intermediate deltas — most providers
        publish a single authoritative cost figure in the final usage
        event rather than emitting incremental deltas across chunks.
        """
        self._usage_result.input_tokens += incoming.input_tokens
        self._usage_result.output_tokens += incoming.output_tokens
        self._usage_result.cache_read_tokens += incoming.cache_read_tokens
        self._usage_result.cache_creation_tokens += incoming.cache_creation_tokens
        self._usage_result.reasoning_tokens += incoming.reasoning_tokens
        self._usage_result.thinking_characters += incoming.thinking_characters
        if incoming.is_complete:
            self._usage_result.is_complete = True
            if incoming.reported_cost_microdollars is not None:
                self._usage_result.reported_cost_microdollars = (
                    incoming.reported_cost_microdollars
                )
                self._usage_result.reported_cost_source = incoming.reported_cost_source
        elif incoming.reported_cost_microdollars is not None:
            # Intermediate chunk surfaced a cost. Take it but keep
            # tracking; a later ``is_complete`` chunk may supersede it.
            self._usage_result.reported_cost_microdollars = (
                incoming.reported_cost_microdollars
            )
            self._usage_result.reported_cost_source = incoming.reported_cost_source

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
        if self._pending_cr:
            self._buffer += "\n"
            self._pending_cr = False

        # At EOF, the final partial line is complete even without a newline.
        # If an oversized partial line was being discarded, none of its
        # truncated contents are parseable.
        lines = [] if self._discarding_incomplete_line else self._buffer.split("\n")
        self._discarding_incomplete_line = False
        if not lines:
            self._flush_event()
            self._buffer = ""
            return
        for line in lines:
            if not line:
                self._flush_event()
                continue
            self._process_line(line)

        # Flush any remaining partial event (no trailing blank line)
        self._flush_event()
        self._buffer = ""
