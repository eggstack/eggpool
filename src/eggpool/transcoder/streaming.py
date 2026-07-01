"""Phase 3 streaming SSE translation between OpenAI and Anthropic protocols.

Provides ``StreamingTranscoder`` implementations that translate upstream SSE
byte streams into client-format bytes on a chunk-by-chunk basis.  Each
transcoder owns an ``IncrementalSSEObserver`` for usage extraction and
maintains its own incremental SSE frame parser for translation.

Phase 6.1 adds tool-call delta support: ``AnthropicToOpenAIStreaming`` emits
``delta.tool_calls`` entries for every ``content_block_start`` /
``input_json_delta`` / ``content_block_stop`` triple carrying a
``tool_use`` block; ``OpenAIToAnthropicStreaming`` buffers incremental
``tool_calls[*].function.arguments`` strings and emits an Anthropic
``content_block_start`` + ``content_block_stop`` pair per call when the
upstream signals ``finish_reason: "tool_calls"``.
"""

from __future__ import annotations

import codecs
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from eggpool.proxy.sse_observer import IncrementalSSEObserver

if TYPE_CHECKING:
    from eggpool.proxy.usage import StreamUsageResult
    from eggpool.transcoder.context import TranscodeContext
    from eggpool.transcoder.ids import ToolCallIdMap
    from eggpool.transcoder.policy import TranscoderFeatures

from eggpool.transcoder.policy import build_reasoning_fields

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

_PAUSE_TURN_FUNCTION_NAME = "__eggpool_pause_turn__"


@dataclass(slots=True)
class _OpenAIToolCall:
    """Per-slot tool-call state for the Anthropic → OpenAI streaming transcoder."""

    index: int
    openai_index: int
    id: str
    name: str
    arguments: str = ""
    finalised: bool = False


@dataclass(slots=True)
class _AnthropicToolUse:
    """Per-slot tool-use state for the OpenAI → Anthropic streaming transcoder."""

    openai_index: int
    anthropic_index: int
    id: str
    name: str
    arguments: str = ""


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
        *,
        transcode_context: TranscodeContext | None = None,
    ) -> None:
        self.client_protocol = client_protocol
        self.upstream_protocol = upstream_protocol
        self._observer = IncrementalSSEObserver(upstream_protocol)
        self._transcode_context = transcode_context
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
            self._warn(
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
                self._warn(
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

    def _warn(self, message: str, *args: object, **context: Any) -> None:
        """Log a warning and accumulate it in the transcode context."""
        logger.warning(message, *args)
        if self._transcode_context is not None:
            payload: dict[str, Any] = {"streaming_transcoder": message}
            payload.update(context)
            self._transcode_context.loss_warnings.append(payload)

    def _id_map(self) -> ToolCallIdMap | None:
        if self._transcode_context is None:
            return None
        return self._transcode_context.id_map

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _safe_json(self, data: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            self._warn("Malformed SSE data, skipping")
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

    def __init__(
        self,
        *,
        transcode_context: TranscodeContext | None = None,
    ) -> None:
        super().__init__("anthropic", "openai", transcode_context=transcode_context)
        self._started = False
        self._content_block_started = False
        self._finished = False
        self._stopped = False
        self._id = ""
        self._model = ""
        self._pending_stop_reason = "end_turn"
        self._pending_usage: dict[str, Any] | None = None
        self._usage_emitted = False
        self._anthropic_tool_blocks: dict[int, _AnthropicToolUse] = {}
        self._tool_blocks_emitted = False

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
        if (
            not self._finished
            and self._anthropic_tool_blocks
            and not self._tool_blocks_emitted
        ):
            self._finished = True
            self._pending_stop_reason = "tool_use"
            out.extend(self._flush_pending_tool_blocks())
        out.extend(self._stop_message())
        return out

    def _translate(
        self,
        event_type: str,
        data: str,
    ) -> list[bytes]:
        if event_type == "error":
            return self._handle_error(data)
        if data.strip() == "[DONE]":
            return self._stop_message()
        parsed = self._safe_json(data)
        if parsed is None:
            return []
        return self._dispatch(parsed)

    def _handle_error(self, data: str) -> list[bytes]:
        parsed = self._safe_json(data)
        if parsed is None:
            return []
        self._stopped = True
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
        tool_calls_delta = delta.get("tool_calls")
        if not self._started and (
            delta.get("role") == "assistant"
            or text is not None
            or tool_calls_delta
            or finish
        ):
            out = self._start_message(parsed)
        else:
            out = []
        if tool_calls_delta:
            out.extend(self._ingest_tool_calls(tool_calls_delta))
        if text:
            out.extend(self._content_delta(text))
            return out
        if finish:
            out.extend(self._finish(parsed, finish))
        return out

    def _ingest_tool_calls(
        self,
        tool_calls_delta: list[object],
    ) -> list[bytes]:
        """Buffer OpenAI ``tool_calls`` deltas for later Anthropic emission.

        The streaming transcoder cannot emit the Anthropic shape until the
        upstream signals ``finish_reason: "tool_calls"``; until then we
        accumulate id / name / arguments on the per-index slot.
        """
        out: list[bytes] = []
        id_map = self._id_map()
        for entry in tool_calls_delta:
            if not isinstance(entry, dict):
                continue
            entry_dict: dict[str, Any] = cast("dict[str, Any]", entry)
            raw_index = entry_dict.get("index")
            index = int(raw_index) if raw_index is not None else 0
            slot = self._anthropic_tool_blocks.get(index)
            call_id_raw = entry_dict.get("id")
            call_id = str(call_id_raw) if call_id_raw is not None else None
            function_raw = entry_dict.get("function")
            function = (
                cast("dict[str, Any]", function_raw)
                if isinstance(function_raw, dict)
                else None
            )
            if call_id:
                if slot is not None and slot.id and slot.id != call_id:
                    self._warn(
                        "tool_call_id_changed",
                        index=index,
                        from_id=slot.id,
                        to_id=call_id,
                    )
                if slot is None:
                    upstream_id = (
                        id_map.generate_anthropic_id()
                        if id_map is not None
                        else f"toolu_{call_id.removeprefix('call_') or 'x'}"
                    )
                    if id_map is not None and call_id:
                        id_map.register(call_id, upstream_id)
                    slot = _AnthropicToolUse(
                        openai_index=index,
                        anthropic_index=len(self._anthropic_tool_blocks),
                        id=upstream_id,
                        name="",
                        arguments="",
                    )
                    self._anthropic_tool_blocks[index] = slot
                elif id_map is not None and call_id:
                    id_map.register(call_id, slot.id)
            if slot is None and function is not None:
                upstream_id = (
                    id_map.generate_anthropic_id() if id_map is not None else None
                )
                slot = _AnthropicToolUse(
                    openai_index=index,
                    anthropic_index=len(self._anthropic_tool_blocks),
                    id=upstream_id or f"toolu_anon_{index}",
                    name="",
                    arguments="",
                )
                self._anthropic_tool_blocks[index] = slot
            if slot is None:
                continue
            if function is not None:
                name_val = function.get("name")
                if name_val is not None:
                    slot.name = str(name_val)
                arguments_val = function.get("arguments")
                if arguments_val is not None:
                    slot.arguments = (slot.arguments or "") + str(arguments_val)
        return out

    def _start_message(self, parsed: dict[str, Any]) -> list[bytes]:
        self._started = True
        self._id = str(parsed.get("id", ""))
        self._model = str(parsed.get("model", ""))
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
        ]

    def _start_content_block(self) -> list[bytes]:
        if self._content_block_started:
            return []
        self._content_block_started = True
        return [
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
        ]

    def _content_delta(self, text: str) -> list[bytes]:
        out = self._start_content_block()
        out.append(
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
            )
        )
        return out

    def _finish(
        self,
        parsed: dict[str, Any],
        finish_reason: str,
    ) -> list[bytes]:
        self._finished = True
        self._pending_stop_reason = _FINISH_TO_STOP.get(
            finish_reason,
            "end_turn",
        )
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            self._pending_usage = cast("dict[str, Any]", usage)
        out: list[bytes] = []
        if self._content_block_started:
            out.append(
                self._anthropic_frame(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": 0},
                )
            )
        out.extend(self._flush_pending_tool_blocks())
        return out

    def _flush_pending_tool_blocks(self) -> list[bytes]:
        """Emit one ``content_block_start`` + ``content_block_stop`` per slot."""
        if not self._anthropic_tool_blocks or self._tool_blocks_emitted:
            return []
        self._tool_blocks_emitted = True
        out: list[bytes] = []
        for anthropic_index, slot in enumerate(self._anthropic_tool_blocks.values()):
            parsed_input = self._parse_tool_arguments(slot.arguments)
            slot.anthropic_index = anthropic_index
            out.append(
                self._anthropic_frame(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": anthropic_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": slot.id,
                            "name": slot.name,
                            "input": parsed_input,
                        },
                    },
                )
            )
            out.append(
                self._anthropic_frame(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": anthropic_index},
                )
            )
        return out

    def _parse_tool_arguments(self, raw: str) -> dict[str, Any]:
        """Parse accumulated ``partial_json`` into an input object.

        Invalid JSON is wrapped as ``{"__raw_arguments__": "<raw>"}`` and a
        ``malformed_tool_arguments`` warning is appended to the transcode
        context (if available).
        """
        try:
            parsed_obj: object = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, ValueError):
            self._warn("malformed_tool_arguments", id=self._id)
            return {"__raw_arguments__": raw}
        if isinstance(parsed_obj, dict):
            return cast("dict[str, Any]", parsed_obj)
        self._warn("malformed_tool_arguments", id=self._id, reason="not_object")
        return {"__raw_arguments__": raw}

    def _handle_usage_only(
        self,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        usage = parsed.get("usage")
        if not isinstance(usage, dict):
            return []
        self._pending_usage = cast("dict[str, Any]", usage)
        if self._finished:
            return self._stop_message()
        if self._started:
            self._usage_emitted = True
            return [self._message_delta(stop_reason=None, usage=self._pending_usage)]
        return []

    def _stop_message(self) -> list[bytes]:
        if not self._started or not self._finished or self._stopped:
            return []
        self._stopped = True
        usage = None if self._usage_emitted else self._pending_usage
        return [
            self._message_delta(
                stop_reason=self._pending_stop_reason,
                usage=usage,
            ),
            self._anthropic_frame(
                "message_stop",
                {"type": "message_stop"},
            ),
        ]

    def _message_delta(
        self,
        *,
        stop_reason: str | None,
        usage: dict[str, Any] | None,
    ) -> bytes:
        delta_payload: dict[str, Any] = {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
        }
        if usage is not None:
            delta_payload["usage"] = {
                "output_tokens": usage.get("completion_tokens", 0),
            }
        return self._anthropic_frame("message_delta", delta_payload)


class AnthropicToOpenAIStreaming(_BaseStreamingTranscoder):
    """State machine converting Anthropic SSE events to OpenAI SSE."""

    def __init__(
        self,
        *,
        include_usage: bool = True,
        transcode_context: TranscodeContext | None = None,
        features: TranscoderFeatures | None = None,
        reasoning_field_names: list[str] | None = None,
        emit_compat_aliases: bool = False,
    ) -> None:
        super().__init__("openai", "anthropic", transcode_context=transcode_context)
        self._include_usage = include_usage
        self._features = features
        self._reasoning_field_names = reasoning_field_names or ["reasoning"]
        self._emit_compat_aliases = emit_compat_aliases
        self._started = False
        self._id = ""
        self._model = ""
        self._emitted_usage = False
        self._done_emitted = False
        self._tool_blocks: dict[int, _OpenAIToolCall] = {}
        self._openai_tool_index: dict[int, int] = {}
        self._next_openai_tool_index = 0

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
        done = self._emit_done()
        if done is not None:
            out.append(done)
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
        out = [
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
        ]
        done = self._emit_done()
        if done is not None:
            out.append(done)
        return out

    def _dispatch(
        self,
        event_type: str,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        t = parsed.get("type", event_type)
        if t == "message_start":
            return self._on_message_start(parsed)
        if t == "content_block_start":
            return self._on_content_block_start(parsed)
        if t == "content_block_delta":
            return self._on_content_block_delta(parsed)
        if t == "content_block_stop":
            return self._on_content_block_stop(parsed)
        if t == "message_delta":
            return self._on_message_delta(parsed)
        return []

    def _on_content_block_start(
        self,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        block_raw = parsed.get("content_block")
        if not isinstance(block_raw, dict):
            return []
        block = cast("dict[str, Any]", block_raw)
        if block.get("type") != "tool_use":
            return []
        raw_index = parsed.get("index", 0)
        upstream_index = int(raw_index) if raw_index is not None else 0
        id_raw = block.get("id", "")
        upstream_id = str(id_raw) if id_raw is not None else ""
        name_raw = block.get("name", "")
        name = str(name_raw) if name_raw is not None else ""
        id_map = self._id_map()
        openai_id: str | None = (
            id_map.to_client(upstream_id)
            if id_map is not None and upstream_id
            else None
        )
        if not openai_id:
            openai_id = (
                id_map.generate_openai_id()
                if id_map is not None
                else f"call_{upstream_id.removeprefix('toolu_') or 'x'}"
            )
        if id_map is not None and upstream_id:
            id_map.register(openai_id, upstream_id)
        openai_index = self._next_openai_tool_index
        self._next_openai_tool_index += 1
        self._tool_blocks[upstream_index] = _OpenAIToolCall(
            index=upstream_index,
            openai_index=openai_index,
            id=openai_id,
            name=name,
        )
        self._openai_tool_index[upstream_index] = openai_index
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
                                "tool_calls": [
                                    {
                                        "index": openai_index,
                                        "id": openai_id,
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": "",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                },
            ),
        ]

    def _on_content_block_stop(
        self,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        upstream_index = int(parsed.get("index", 0))
        slot = self._tool_blocks.get(upstream_index)
        if slot is not None:
            slot.finalised = True
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
        delta_type = delta.get("type", "")
        if delta_type == "input_json_delta":
            return self._on_tool_input_json_delta(parsed)
        if delta_type == "thinking_delta":
            thinking_text = delta.get("thinking", "")
            if not thinking_text:
                return []
            if self._features is not None and not self._features.thinking:
                return []
            delta_fields = build_reasoning_fields(
                self._reasoning_field_names,
                thinking_text,
                emit_compat_aliases=self._emit_compat_aliases,
            )
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
                                "delta": delta_fields,
                                "finish_reason": None,
                            }
                        ],
                    },
                ),
            ]
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

    def _on_tool_input_json_delta(
        self,
        parsed: dict[str, Any],
    ) -> list[bytes]:
        upstream_index = int(parsed.get("index", 0))
        slot = self._tool_blocks.get(upstream_index)
        if slot is None:
            return []
        delta = parsed.get("delta", {})
        partial = str(delta.get("partial_json", ""))
        slot.arguments = (slot.arguments or "") + partial
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
                                "tool_calls": [
                                    {
                                        "index": slot.openai_index,
                                        "function": {"arguments": partial},
                                    }
                                ]
                            },
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

        if stop == "pause_turn":
            out.extend(self._synthesise_pause_turn_sentinel())
            if self._transcode_context is not None:
                self._transcode_context.loss_warnings.append(
                    {
                        "kind": "pause_turn",
                        "field": "stop_reason",
                        "to": "tool_calls",
                    }
                )

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

        if stop:
            done = self._emit_done()
            if done is not None:
                out.append(done)
        return out

    def _synthesise_pause_turn_sentinel(self) -> list[bytes]:
        """Emit a synthetic ``__eggpool_pause_turn__`` tool_call for streaming.

        When Anthropic signals ``pause_turn`` as the stop reason, OpenAI
        clients expect ``finish_reason: "tool_calls"`` with at least one
        tool_call entry.  This method synthesises the sentinel tool_call
        deltas that the non-streaming path emits in
        ``openai_to_anthropic.decode_response``.
        """
        id_map = self._id_map()
        openai_id = (
            id_map.generate_openai_id() if id_map is not None else "call_pause_turn"
        )
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
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": openai_id,
                                        "type": "function",
                                        "function": {
                                            "name": _PAUSE_TURN_FUNCTION_NAME,
                                            "arguments": "",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                },
            ),
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
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": "{}"},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
            ),
        ]

    def _emit_done(self) -> bytes | None:
        """Emit the OpenAI terminal marker at most once."""
        if self._done_emitted:
            return None
        self._done_emitted = True
        return self._openai_done()


def select_streaming_transcoder(
    *,
    client_protocol: str,
    upstream_protocol: str,
    include_usage: bool = True,
    transcode_context: TranscodeContext | None = None,
    features: TranscoderFeatures | None = None,
    reasoning_field_names: list[str] | None = None,
    emit_compat_aliases: bool = False,
) -> StreamingTranscoder | None:
    """Return the streaming transcoder for a protocol pair.

    Returns ``None`` when the pair matches and no translation is needed.
    """
    if client_protocol == upstream_protocol:
        return None
    if client_protocol == "openai" and upstream_protocol == "anthropic":
        return AnthropicToOpenAIStreaming(
            include_usage=include_usage,
            transcode_context=transcode_context,
            features=features,
            reasoning_field_names=reasoning_field_names,
            emit_compat_aliases=emit_compat_aliases,
        )
    if client_protocol == "anthropic" and upstream_protocol == "openai":
        return OpenAIToAnthropicStreaming(
            transcode_context=transcode_context,
        )
    return None
