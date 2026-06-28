"""Phase 3 streaming SSE translation between OpenAI and Anthropic protocols.

Provides ``StreamingTranscoder`` implementations that translate upstream SSE
byte streams into client-format bytes on a chunk-by-chunk basis.  Each
transcoder owns an ``IncrementalSSEObserver`` for usage extraction and
maintains its own incremental SSE frame parser for translation.
"""

from __future__ import annotations

import codecs
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

from eggpool.proxy.sse_observer import IncrementalSSEObserver

if TYPE_CHECKING:
    from eggpool.proxy.usage import StreamUsageResult

logger = logging.getLogger(__name__)

# Reversed from openai_to_anthropic.py STOP_REASON_MAP — maps OpenAI
# finish_reason values to Anthropic stop_reason values.
_FINISH_TO_STOP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
}

# Reversed from anthropic_to_openai.py FINISH_REASON_MAP — maps
# Anthropic stop_reason values to OpenAI finish_reason values.
_STOP_TO_FINISH: dict[str, str] = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "pause_turn": "tool_calls",
    "model_context_window_exceeded": "length",
}

_MAX_INCOMPLETE_FRAME_BYTES = 64 * 1024


def _utf8_len(value: str) -> int:
    """Return the UTF-8 byte length without allocating for ASCII strings."""
    return len(value) if value.isascii() else len(value.encode("utf-8"))


class StreamingTranscoder(Protocol):
    """Translate an upstream SSE stream into client-format bytes."""

    client_protocol: str
    upstream_protocol: str

    async def feed(self, chunk: bytes) -> list[bytes]: ...
    async def flush(self) -> list[bytes]: ...

    @property
    def usage(self) -> StreamUsageResult: ...


class _BaseStreamingTranscoder:
    """Shared incremental SSE frame parser and observer wiring."""

    client_protocol: str
    upstream_protocol: str

    def __init__(
        self,
        client_protocol: str,
        upstream_protocol: str,
    ) -> None:
        self.client_protocol = client_protocol
        self.upstream_protocol = upstream_protocol
        self._observer = IncrementalSSEObserver(upstream_protocol)
        self._decoder = codecs.getincrementaldecoder("utf-8")(
            errors="replace",
        )
        self._buffer = ""
        self._pending_cr = False
        self._current_data_lines: list[str] = []
        self._current_event = ""
        self._current_event_bytes = 0
        self._discarding = False

    # ------------------------------------------------------------------
    # Incremental SSE frame parser
    # ------------------------------------------------------------------

    def _parse_chunk(
        self,
        chunk: bytes,
    ) -> list[tuple[str, str]]:
        """Decode *chunk* and return ``(event_type, data)`` tuples.

        Handles arbitrary chunk boundaries, CRLF/LF delimiters, and
        buffers incomplete frames across calls.
        """
        text = self._decoder.decode(chunk)
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

        frames: list[tuple[str, str]] = []
        for line in lines:
            if self._discarding:
                self._discarding = False
                continue
            if not line:
                frame = self._emit_frame()
                if frame is not None:
                    frames.append(frame)
                continue
            self._process_line(line)

        self._buffer = "" if self._discarding else incomplete_line
        if _utf8_len(self._buffer) > _MAX_INCOMPLETE_FRAME_BYTES:
            logger.warning(
                "SSE line exceeded %d bytes, discarding",
                _MAX_INCOMPLETE_FRAME_BYTES,
            )
            self._buffer = ""
            self._discarding = True
            self._reset_frame(discarding=True)
        return frames

    def _process_line(self, line: str) -> None:
        """Parse a single SSE line into the current frame buffer."""
        if self._discarding:
            return
        if line.startswith(":"):
            return
        if ":" not in line:
            return
        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "event":
            self._current_event = value
        elif field == "data":
            sep = 1 if self._current_data_lines else 0
            ev_bytes = self._current_event_bytes + sep + _utf8_len(value)
            if ev_bytes > _MAX_INCOMPLETE_FRAME_BYTES:
                logger.warning(
                    "SSE event exceeded %d bytes, discarding",
                    _MAX_INCOMPLETE_FRAME_BYTES,
                )
                self._discarding = True
                self._reset_frame(discarding=True)
                return
            self._current_data_lines.append(value)
            self._current_event_bytes = ev_bytes

    def _emit_frame(self) -> tuple[str, str] | None:
        """Return the accumulated frame and reset state."""
        if self._discarding:
            self._reset_frame()
            return None
        if not self._current_data_lines:
            return None
        event = self._current_event
        data = "\n".join(self._current_data_lines)
        self._reset_frame()
        return (event, data)

    def _reset_frame(
        self,
        *,
        discarding: bool = False,
    ) -> None:
        self._current_data_lines.clear()
        self._current_event_bytes = 0
        self._current_event = ""
        self._discarding = discarding

    def _drain_buffer(self) -> list[tuple[str, str]]:
        """Flush the incremental decoder and emit any remaining frame."""
        try:
            remainder = self._decoder.decode(b"", True)
        except UnicodeDecodeError:
            remainder = ""
        if remainder:
            self._buffer += remainder.replace(
                "\r\n",
                "\n",
            ).replace("\r", "\n")
        if self._pending_cr:
            self._buffer += "\n"
            self._pending_cr = False

        frames: list[tuple[str, str]] = []
        if self._buffer and not self._discarding:
            lines = self._buffer.split("\n")
            for line in lines:
                if not line:
                    frame = self._emit_frame()
                    if frame is not None:
                        frames.append(frame)
                    continue
                self._process_line(line)
            frame = self._emit_frame()
            if frame is not None:
                frames.append(frame)
        self._buffer = ""
        self._discarding = False
        return frames

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_json(data: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            logger.debug("Malformed SSE data, skipping")
            return None
        if not isinstance(obj, dict):
            return None
        return cast("dict[str, Any]", obj)

    @staticmethod
    def _anthropic_frame(
        event: str,
        data: dict[str, Any],
    ) -> bytes:
        return (f"event: {event}\ndata: {json.dumps(data)}\n\n").encode()

    @staticmethod
    def _openai_frame(data: dict[str, Any]) -> bytes:
        return f"data: {json.dumps(data)}\n\n".encode()

    @staticmethod
    def _openai_done() -> bytes:
        return b"data: [DONE]\n\n"

    @property
    def usage(self) -> StreamUsageResult:
        return self._observer.usage


class OpenAIToAnthropicStreaming(_BaseStreamingTranscoder):
    """State machine converting OpenAI SSE chunks to Anthropic SSE."""

    def __init__(self) -> None:
        super().__init__("anthropic", "openai")
        self._started = False
        self._id = ""
        self._model = ""

    async def feed(self, chunk: bytes) -> list[bytes]:
        self._observer.observe(chunk)
        frames = self._parse_chunk(chunk)
        out: list[bytes] = []
        for event_type, data in frames:
            out.extend(self._translate(event_type, data))
        return out

    async def flush(self) -> list[bytes]:
        self._observer.flush()
        frames = self._drain_buffer()
        out: list[bytes] = []
        for event_type, data in frames:
            out.extend(self._translate(event_type, data))
        return out

    def _translate(
        self,
        event_type: str,
        data: str,
    ) -> list[bytes]:
        if event_type == "error":
            return self._handle_error(data)
        if data.strip() == "[DONE]":
            return []
        parsed = self._safe_json(data)
        if parsed is None:
            return []
        return self._dispatch(parsed)

    def _handle_error(self, data: str) -> list[bytes]:
        parsed = self._safe_json(data)
        if parsed is None:
            return []
        err = parsed.get("error", {})
        if isinstance(err, dict):
            err_typed = cast("dict[str, Any]", err)
            msg = str(err_typed.get("message", str(err_typed)))
        else:
            msg = str(err)
        return [
            self._anthropic_frame(
                "error",
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": msg},
                },
            ),
            self._anthropic_frame(
                "message_stop",
                {"type": "message_stop"},
            ),
        ]

    def _dispatch(
        self,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        choices = parsed.get("choices")
        usage_only = parsed.get("usage") is not None and not choices
        if usage_only:
            return self._handle_usage_only(parsed)
        if not choices:
            return []
        choice = choices[0]
        delta = choice.get("delta", {})
        finish = choice.get("finish_reason")
        text = delta.get("content")
        if text and not self._started:
            self._started = True
            self._id = str(parsed.get("id", ""))
            self._model = str(parsed.get("model", ""))
            return self._start_message(text)
        if text:
            return self._content_delta(text)
        if finish:
            return self._finish(parsed, finish)
        return []

    def _start_message(self, text: str) -> list[bytes]:
        return [
            self._anthropic_frame(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self._id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": self._model,
                        "stop_reason": None,
                        "usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                        },
                    },
                },
            ),
            self._anthropic_frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "text",
                        "text": "",
                    },
                },
            ),
            self._anthropic_frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "text_delta",
                        "text": text,
                    },
                },
            ),
        ]

    def _content_delta(self, text: str) -> list[bytes]:
        return [
            self._anthropic_frame(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "text_delta",
                        "text": text,
                    },
                },
            ),
        ]

    def _finish(
        self,
        parsed: dict[str, Any],
        finish_reason: str,
    ) -> list[bytes]:
        out: list[bytes] = [
            self._anthropic_frame(
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            ),
        ]
        stop_reason = _FINISH_TO_STOP.get(
            finish_reason,
            "end_turn",
        )
        usage = parsed.get("usage")
        delta_payload: dict[str, Any] = {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
        }
        if isinstance(usage, dict):
            usage_typed = cast("dict[str, Any]", usage)
            delta_payload["usage"] = {
                "output_tokens": usage_typed.get(
                    "completion_tokens",
                    0,
                ),
            }
        out.append(self._anthropic_frame("message_delta", delta_payload))
        out.append(
            self._anthropic_frame(
                "message_stop",
                {"type": "message_stop"},
            ),
        )
        return out

    def _handle_usage_only(
        self,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            return []
        usage_typed = cast("dict[str, Any]", usage)
        return [
            self._anthropic_frame(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": None},
                    "usage": {
                        "output_tokens": usage_typed.get(
                            "completion_tokens",
                            0,
                        ),
                    },
                },
            ),
        ]


class AnthropicToOpenAIStreaming(_BaseStreamingTranscoder):
    """State machine converting Anthropic SSE events to OpenAI SSE."""

    def __init__(
        self,
        *,
        include_usage: bool = True,
    ) -> None:
        super().__init__("openai", "anthropic")
        self._include_usage = include_usage
        self._started = False
        self._id = ""
        self._model = ""
        self._emitted_usage = False

    async def feed(self, chunk: bytes) -> list[bytes]:
        self._observer.observe(chunk)
        frames = self._parse_chunk(chunk)
        out: list[bytes] = []
        for event_type, data in frames:
            out.extend(self._translate(event_type, data))
        return out

    async def flush(self) -> list[bytes]:
        self._observer.flush()
        frames = self._drain_buffer()
        out: list[bytes] = []
        for event_type, data in frames:
            out.extend(self._translate(event_type, data))
        out.append(self._openai_done())
        return out

    def _translate(
        self,
        event_type: str,
        data: str,
    ) -> list[bytes]:
        if event_type == "error":
            return self._handle_error(data)
        parsed = self._safe_json(data)
        if parsed is None:
            return []
        return self._dispatch(event_type, parsed)

    def _handle_error(self, data: str) -> list[bytes]:
        parsed = self._safe_json(data)
        if parsed is None:
            return []
        err = parsed.get("error", {})
        if isinstance(err, dict):
            err_typed = cast("dict[str, Any]", err)
            msg = str(err_typed.get("message", str(err_typed)))
        else:
            msg = str(err)
        return [
            self._openai_frame(
                {
                    "error": {
                        "message": msg,
                        "type": "api_error",
                        "code": None,
                        "param": None,
                    },
                },
            ),
            self._openai_done(),
        ]

    def _dispatch(
        self,
        event_type: str,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        t = parsed.get("type", event_type)
        if t == "message_start":
            return self._on_message_start(parsed)
        if t == "content_block_start":
            return []
        if t == "content_block_delta":
            return self._on_content_block_delta(parsed)
        if t == "message_delta":
            return self._on_message_delta(parsed)
        return []

    def _on_message_start(
        self,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        msg_raw = parsed.get("message", {})
        msg = cast("dict[str, Any]", msg_raw) if isinstance(msg_raw, dict) else {}
        self._started = True
        self._id = str(msg.get("id", ""))
        self._model = str(msg.get("model", ""))
        return [
            self._openai_frame(
                {
                    "id": self._id,
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "",
                            },
                            "finish_reason": None,
                        }
                    ],
                },
            ),
        ]

    def _on_content_block_delta(
        self,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        delta = parsed.get("delta", {})
        text = delta.get("text", "")
        if not text:
            return []
        return [
            self._openai_frame(
                {
                    "id": self._id,
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": self._model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": text},
                            "finish_reason": None,
                        }
                    ],
                },
            ),
        ]

    def _on_message_delta(
        self,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        out: list[bytes] = []
        delta = parsed.get("delta", {})
        stop = delta.get("stop_reason")
        usage = parsed.get("usage")
        if stop:
            fr = _STOP_TO_FINISH.get(stop, "stop")
            frame: dict[str, Any] = {
                "id": self._id,
                "object": "chat.completion.chunk",
                "created": 0,
                "model": self._model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": fr,
                    }
                ],
            }
            if (
                self._include_usage
                and isinstance(usage, dict)
                and not self._emitted_usage
            ):
                self._emitted_usage = True
                usage_typed = cast("dict[str, Any]", usage)
                frame["usage"] = {
                    "prompt_tokens": usage_typed.get(
                        "input_tokens",
                        0,
                    ),
                    "completion_tokens": usage_typed.get(
                        "output_tokens",
                        0,
                    ),
                    "total_tokens": (
                        usage_typed.get("input_tokens", 0)
                        + usage_typed.get("output_tokens", 0)
                    ),
                }
            out.append(self._openai_frame(frame))
        elif (
            self._include_usage and isinstance(usage, dict) and not self._emitted_usage
        ):
            self._emitted_usage = True
            usage_typed = cast("dict[str, Any]", usage)
            out.append(
                self._openai_frame(
                    {
                        "id": self._id,
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": self._model,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": usage_typed.get(
                                "input_tokens",
                                0,
                            ),
                            "completion_tokens": usage_typed.get(
                                "output_tokens",
                                0,
                            ),
                            "total_tokens": (
                                usage_typed.get("input_tokens", 0)
                                + usage_typed.get(
                                    "output_tokens",
                                    0,
                                )
                            ),
                        },
                    },
                ),
            )
        out.append(self._openai_done())
        return out


def select_streaming_transcoder(
    *,
    client_protocol: str,
    upstream_protocol: str,
    include_usage: bool = True,
) -> StreamingTranscoder | None:
    """Return the streaming transcoder for a protocol pair.

    Returns ``None`` when the pair matches and no translation is needed.
    """
    if client_protocol == upstream_protocol:
        return None
    if client_protocol == "openai" and upstream_protocol == "anthropic":
        return AnthropicToOpenAIStreaming(
            include_usage=include_usage,
        )
    if client_protocol == "anthropic" and upstream_protocol == "openai":
        return OpenAIToAnthropicStreaming()
    return None
