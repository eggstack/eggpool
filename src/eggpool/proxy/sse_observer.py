"""Frame-level stream completion and usage observation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from eggpool.jsonx import loads as jsonx_loads  # compatibility patch surface
from eggpool.proxy.sse import (
    MAX_SSE_FRAME_BYTES,
    DecodedSSEFrame,
    SSEDecoder,
    SSEDecodeResult,
)
from eggpool.proxy.sse import (
    SSEFrame as FramedSSEFrame,
)
from eggpool.proxy.usage import (
    AnthropicStreamUsageExtractor,
    OpenAIStreamUsageExtractor,
    StreamUsageResult,
    safe_dict,
)

logger = logging.getLogger(__name__)
MAX_INCOMPLETE_FRAME_BYTES = MAX_SSE_FRAME_BYTES


@dataclass(slots=True)
class SSEFrame:
    """Legacy frame helper retained for callers importing the observer."""

    event: str = ""
    data_lines: list[str] = field(default_factory=list[str])

    @property
    def data(self) -> str:
        return "\n".join(self.data_lines)

    @property
    def is_done(self) -> bool:
        return self.data.strip() == "[DONE]"


class StreamCompletionSnapshot:
    __slots__ = (
        "saw_payload",
        "saw_terminal_event",
        "terminal_kind",
        "saw_usage_completion",
        "incomplete_frame_at_eof",
        "parser_error_count",
        "bytes_observed",
    )

    def __init__(
        self,
        *,
        saw_payload: bool,
        saw_terminal_event: bool,
        terminal_kind: str | None,
        saw_usage_completion: bool,
        incomplete_frame_at_eof: bool,
        parser_error_count: int,
        bytes_observed: int,
    ) -> None:
        self.saw_payload = saw_payload
        self.saw_terminal_event = saw_terminal_event
        self.terminal_kind = terminal_kind
        self.saw_usage_completion = saw_usage_completion
        self.incomplete_frame_at_eof = incomplete_frame_at_eof
        self.parser_error_count = parser_error_count
        self.bytes_observed = bytes_observed


class IncrementalSSEObserver:
    """Observe already-framed SSE events without owning byte framing."""

    def __init__(
        self,
        protocol: str,
        *,
        provider_id: str | None = None,
        request_surface: str = "chat_completions",
    ) -> None:
        self._protocol = protocol
        # Plan 143: ``request_surface == "responses"`` swaps the
        # terminal-event vocabulary. The Responses surface uses
        # ``response.completed`` (success) and ``response.failed``
        # (terminal provider failure) instead of Chat's ``[DONE]``
        # marker. Stream completion classification downstream treats
        # ``response.completed`` exactly like ``openai_done``.
        self._request_surface = request_surface
        self._usage_result = StreamUsageResult()
        self._extractor = (
            AnthropicStreamUsageExtractor(provider_id=provider_id)
            if protocol == "anthropic"
            else OpenAIStreamUsageExtractor(provider_id=provider_id)
        )
        self._compat_decoder = SSEDecoder()
        self._bytes_observed = 0
        self._frame_count = 0
        self._error_count = 0
        self._structural_error_count = 0
        self._saw_payload = False
        self._saw_terminal_event = False
        self._terminal_kind: str | None = None
        self._post_terminal_data = False
        self._incomplete_frame_at_eof = False

    def observe_bytes(self, chunk: bytes) -> None:
        """Record transport bytes; framing is performed by the coordinator."""
        self._bytes_observed += len(chunk)

    def observe(self, chunk: bytes) -> None:
        """Compatibility adapter for callers that still provide raw bytes."""
        self.observe_bytes(chunk)
        for decoded in self._compat_decoder.feed(chunk):
            self.observe_frame(decoded)
        self._structural_error_count = self._compat_decoder.structural_error_count
        self._error_count = max(self._error_count, self._structural_error_count)

    def observe_frame(self, decoded: DecodedSSEFrame | FramedSSEFrame) -> None:
        """Consume one shared decoded frame."""
        frame = (
            decoded
            if isinstance(decoded, DecodedSSEFrame)
            else DecodedSSEFrame(decoded)
        )
        payload = frame.frame.data
        self._frame_count += len(frame.frame.fields or ())
        if frame.frame.is_comment_only:
            return
        if not any(name == "data" for name, _ in (frame.frame.fields or ())):
            return
        if payload:
            self._saw_payload = True
        if self._saw_terminal_event and payload:
            self._post_terminal_data = True
        if payload.strip() == "[DONE]":
            if self._saw_terminal_event:
                self._post_terminal_data = True
            else:
                self._saw_terminal_event = True
                self._terminal_kind = "openai_done"
            return
        if frame.frame.event == "message_stop":
            if self._saw_terminal_event:
                self._post_terminal_data = True
            else:
                self._saw_terminal_event = True
                self._terminal_kind = "anthropic_message_stop"
            return
        if self._request_surface == "responses" and frame.frame.event in (
            "response.completed",
            "response.incomplete",
        ):
            if self._saw_terminal_event:
                self._post_terminal_data = True
            else:
                self._saw_terminal_event = True
                self._terminal_kind = "openai_done"
            return
        if (
            self._request_surface == "responses"
            and frame.frame.event == "response.failed"
        ):
            if not self._saw_terminal_event:
                self._saw_terminal_event = True
                self._terminal_kind = "openai_responses_failed"
            else:
                self._post_terminal_data = True
            return

        if (
            self._protocol == "openai"
            and '"usage"' not in payload
            and '"choices"' in payload
        ):
            return
        parsed = frame.json_object(jsonx_loads)
        if parsed is None:
            self._error_count += 1
            return
        try:
            usage = self._extractor.extract(safe_dict(parsed) or {})
        except (ValueError, TypeError, AttributeError):
            self._error_count += 1
            logger.debug("Malformed SSE usage frame, ignoring")
            return
        if usage:
            self._merge_usage(usage)

    def finish(self, eof: SSEDecodeResult | None = None) -> None:
        """Finish observation after the shared decoder has reached EOF."""
        if eof is None:
            eof = self._compat_decoder.finish()
            for decoded in eof.frames:
                self.observe_frame(decoded)
        else:
            for decoded in eof.frames:
                self.observe_frame(decoded)
        self._incomplete_frame_at_eof = eof.incomplete_frame
        self._structural_error_count += (
            eof.invalid_utf8_replacements + eof.discarded_frame_count
        )

    def flush(self) -> None:
        """Compatibility alias for :meth:`finish`."""
        self.finish()

    @property
    def _buffer(self) -> str:
        """Compatibility view retained for bounded-buffer tests."""
        return self._compat_decoder.line_buffer

    def _merge_usage(self, incoming: StreamUsageResult) -> None:
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
            self._usage_result.reported_cost_microdollars = (
                incoming.reported_cost_microdollars
            )
            self._usage_result.reported_cost_source = incoming.reported_cost_source

    @property
    def usage(self) -> StreamUsageResult:
        return self._usage_result

    @property
    def bytes_emitted(self) -> int:
        return self._bytes_observed

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def completion_snapshot(self) -> StreamCompletionSnapshot:
        return StreamCompletionSnapshot(
            saw_payload=self._saw_payload,
            saw_terminal_event=self._saw_terminal_event,
            terminal_kind=self._terminal_kind,
            saw_usage_completion=self._usage_result.is_complete,
            incomplete_frame_at_eof=self._incomplete_frame_at_eof,
            parser_error_count=self._structural_error_count
            + (1 if self._post_terminal_data else 0),
            bytes_observed=self._bytes_observed,
        )
