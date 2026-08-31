"""Phase 3 streaming translation between Chat Completions and Messages protocols.

The coordinator owns byte framing in ``eggpool.proxy.sse.SSEDecoder`` and
passes shared decoded frames here. Raw-byte ``feed``/``flush`` methods are
compatibility adapters for older callers; production uses ``translate_frame``
and ``finish``.

Phase 6.1 adds tool-call delta support: ``AnthropicToOpenAIStreaming`` emits
``delta.tool_calls`` entries for every ``content_block_start`` /
``input_json_delta`` / ``content_block_stop`` triple carrying a
``tool_use`` block; ``OpenAIToAnthropicStreaming`` buffers incremental
``tool_calls[*].function.arguments`` strings and emits an Anthropic
``content_block_start`` + ``content_block_stop`` pair per call when the
upstream signals ``finish_reason: "tool_calls"``.

Phase 7 (transcoded-stream-dispatch-fixes): ``feed`` and ``flush`` are
synchronous; the protocol is no longer ``async``.  Frame emission uses
compact JSON separators to reduce output bytes and serialization work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from eggpool.proxy.usage import StreamUsageResult
    from eggpool.transcoder.context import TranscodeContext
    from eggpool.transcoder.ids import ToolCallIdMap
    from eggpool.transcoder.policy import TranscoderFeatures

from eggpool.catalog.pricing import coerce_token_count
from eggpool.jsonx import dumps_bytes, loads
from eggpool.proxy.sse import DecodedSSEFrame, SSEDecoder, SSEDecodeResult
from eggpool.transcoder.policy import build_reasoning_fields
from eggpool.transcoder.usage import (
    merge_anthropic_usage,
    openai_usage_from_anthropic_usage,
)

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


class StreamingTranscoder(Protocol):
    """Translate an upstream SSE stream into client-format bytes.

    ``feed`` and ``flush`` are synchronous because the implementations do
    not perform async I/O — the per-upstream-chunk work is incremental
    UTF-8 parsing, JSON decoding/encoding, and nested dict construction.
    Keeping these synchronous removes an unnecessary ``await`` per chunk
    on the hot streaming path.  Any future implementation that requires
    async I/O must not be placed in this per-chunk path without a
    separate design review.

    The ``usage`` property remains on the protocol for compatibility, but
    transcoders do not run their own ``IncrementalSSEObserver``; they
    return a default ``StreamUsageResult``.  Finalization in the
    coordinator uses the dedicated ``IncrementalSSEObserver`` it owns.
    """

    client_protocol: str
    upstream_protocol: str

    def translate_frame(self, frame: DecodedSSEFrame) -> list[bytes]: ...
    def finish(self, completion: SSEDecodeResult | None = None) -> list[bytes]: ...
    def feed(self, chunk: bytes) -> list[bytes]: ...
    def flush(self) -> list[bytes]: ...

    @property
    def saw_terminal_event(self) -> bool: ...

    @property
    def usage(self) -> StreamUsageResult: ...


class _BaseStreamingTranscoder:
    """Frame translator; raw-byte methods are compatibility adapters only."""

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
        self._transcode_context = transcode_context
        self._compat_decoder = SSEDecoder()
        self._saw_terminal_event = False

    @property
    def saw_terminal_event(self) -> bool:
        """Whether a canonical upstream terminal event was consumed."""
        return self._saw_terminal_event

    def feed(self, chunk: bytes) -> list[bytes]:
        """Deprecated raw-byte adapter; production uses ``translate_frame``."""
        out: list[bytes] = []
        for frame in self._compat_decoder.feed(chunk):
            out.extend(self.translate_frame(frame))
        return out

    def finish(self, completion: SSEDecodeResult | None = None) -> list[bytes]:
        del completion
        return []

    def flush(self) -> list[bytes]:
        """Deprecated raw-byte adapter for the frame-level ``finish`` API."""
        return self.finish(self._compat_decoder.finish())

    def translate_frame(self, frame: DecodedSSEFrame) -> list[bytes]:
        if frame.frame.is_comment_only or not any(
            name == "data" for name, _ in (frame.frame.fields or ())
        ):
            return []
        return self._translate(frame)

    def _translate(self, frame: DecodedSSEFrame) -> list[bytes]:
        raise NotImplementedError

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

    def _safe_json(self, frame: DecodedSSEFrame) -> dict[str, Any] | None:
        obj = frame.json_object()
        if obj is None:
            self._warn("Malformed SSE data, skipping")
        return obj

    @staticmethod
    def _anthropic_frame(
        event: str,
        data: dict[str, Any],
    ) -> bytes:
        return b"".join(
            (
                b"event: ",
                event.encode("ascii"),
                b"\ndata: ",
                dumps_bytes(data),
                b"\n\n",
            )
        )

    @staticmethod
    def _openai_frame(data: dict[str, Any]) -> bytes:
        return b"data: " + dumps_bytes(data) + b"\n\n"

    @staticmethod
    def _openai_done() -> bytes:
        return b"data: [DONE]\n\n"

    @property
    def usage(self) -> StreamUsageResult:
        # Transcoders no longer run their own IncrementalSSEObserver;
        # usage extraction lives in the coordinator's observer.  This
        # property returns a default result to preserve the protocol.
        from eggpool.proxy.usage import StreamUsageResult

        return StreamUsageResult()


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

    def finish(self, completion: SSEDecodeResult | None = None) -> list[bytes]:
        frames = completion.frames if completion is not None else ()
        out: list[bytes] = []
        for frame in frames:
            out.extend(self.translate_frame(frame))
        if (
            self._saw_terminal_event
            and not self._finished
            and self._anthropic_tool_blocks
            and not self._tool_blocks_emitted
        ):
            self._finished = True
            self._pending_stop_reason = "tool_use"
            out.extend(self._flush_pending_tool_blocks())
        if self._saw_terminal_event and not self._finished and not self._stopped:
            # Upstream ended its SSE without a finish_reason (truncation,
            # flaky provider): close any open content block and synthesize
            # the default stop so clients still receive the stop sequence.
            if self._content_block_started:
                out.append(
                    self._anthropic_frame(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": 0},
                    )
                )
                self._content_block_started = False
            self._finished = True
        if self._saw_terminal_event:
            out.extend(self._stop_message())
        return out

    def _translate(self, frame: DecodedSSEFrame) -> list[bytes]:
        event_type = frame.frame.event or ""
        data = frame.frame.data
        if event_type == "error":
            return self._handle_error(frame)
        if data.strip() == "[DONE]":
            self._saw_terminal_event = True
            # Without a finish reason, ``_stop_message`` waits for
            # ``finish`` to flush the decoder and synthesize one.
            return self._stop_message()
        parsed = self._safe_json(frame)
        if parsed is None:
            return []
        return self._dispatch(parsed)

    def _handle_error(self, frame: DecodedSSEFrame) -> list[bytes]:
        parsed = self._safe_json(frame)
        if parsed is None:
            return []
        self._stopped = True
        err = parsed.get("error", {})
        if isinstance(err, dict):
            err_typed = cast("dict[str, Any]", err)
            msg = str(err_typed.get("message", str(err_typed)))
        else:
            msg = str(err)
        self._saw_terminal_event = True
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
                # A tool_calls delta with neither an id nor a function
                # payload and no matching slot carries nothing we can
                # accumulate; surface the drop instead of swallowing it.
                self._warn(
                    "tool_call_delta_dropped",
                    id=self._id,
                    index=index,
                )
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
        # Tool blocks follow the optional text block (index 0); offset so
        # Anthropic block indices stay sequential and unique.
        base = 1 if self._content_block_started else 0
        for anthropic_index, slot in enumerate(
            self._anthropic_tool_blocks.values(), start=base
        ):
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

        Invalid JSON produces an empty object and a
        ``malformed_tool_arguments`` warning is appended to the transcode
        context (if available).  The Anthropic tool-use contract requires
        ``input`` to be an object, so raw argument text must not be invented
        as a schema field.
        """
        try:
            parsed_obj: object = loads(raw) if raw else {}
        except ValueError:
            self._warn("malformed_tool_arguments", id=self._id)
            return {}
        if isinstance(parsed_obj, dict):
            return cast("dict[str, Any]", parsed_obj)
        self._warn("malformed_tool_arguments", id=self._id, reason="not_object")
        return {}

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
            prompt_details = usage.get("prompt_tokens_details")
            completion_details = usage.get("completion_tokens_details")
            translated_usage: dict[str, Any] = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            }
            if isinstance(prompt_details, dict):
                if "cached_tokens" in prompt_details:
                    translated_usage["cache_read_input_tokens"] = prompt_details[
                        "cached_tokens"
                    ]
                if "cache_write_tokens" in prompt_details:
                    translated_usage["cache_creation_input_tokens"] = prompt_details[
                        "cache_write_tokens"
                    ]
            if "cached_tokens" in usage:
                translated_usage["cache_read_input_tokens"] = usage["cached_tokens"]
            if "cache_read_input_tokens" in usage:
                translated_usage["cache_read_input_tokens"] = usage[
                    "cache_read_input_tokens"
                ]
            if "cache_creation_input_tokens" in usage:
                translated_usage["cache_creation_input_tokens"] = usage[
                    "cache_creation_input_tokens"
                ]
            if "cache_write_input_tokens" in usage:
                translated_usage["cache_creation_input_tokens"] = usage[
                    "cache_write_input_tokens"
                ]
            if isinstance(completion_details, dict) and (
                "reasoning_tokens" in completion_details
            ):
                completion_details_dict = cast("dict[str, Any]", completion_details)
                translated_usage["output_tokens"] = max(
                    coerce_token_count(translated_usage["output_tokens"]),
                    coerce_token_count(completion_details_dict["reasoning_tokens"]),
                )
            delta_payload["usage"] = translated_usage
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
        self._anthropic_usage: dict[str, int] = {}
        self._tool_blocks: dict[int, _OpenAIToolCall] = {}
        self._next_openai_tool_index = 0
        self._thinking_delta_count: int = 0
        self._pending_stop_reason: str | None = None

    @property
    def thinking_delta_count(self) -> int:
        return self._thinking_delta_count

    def finish(self, completion: SSEDecodeResult | None = None) -> list[bytes]:
        frames = completion.frames if completion is not None else ()
        out: list[bytes] = []
        for frame in frames:
            out.extend(self.translate_frame(frame))
        if self._saw_terminal_event:
            done = self._emit_done()
            if done is not None:
                out.append(done)
        return out

    def _translate(self, frame: DecodedSSEFrame) -> list[bytes]:
        event_type = frame.frame.event or ""
        if event_type == "error":
            return self._handle_error(frame)
        if event_type == "message_stop":
            self._saw_terminal_event = True
            parsed = self._safe_json(frame)
            if parsed is None:
                return []
            return self._dispatch(event_type, parsed)
        parsed = self._safe_json(frame)
        if parsed is None:
            return []
        return self._dispatch(event_type, parsed)

    def _handle_error(self, frame: DecodedSSEFrame) -> list[bytes]:
        parsed = self._safe_json(frame)
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
        if t == "message_stop":
            done = self._emit_done()
            return [done] if done is not None else []
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
            # ``text`` / ``thinking`` blocks are intentionally handled
            # via their delta events; anything else is content this
            # translator cannot represent, so record the loss.
            block_type = str(block.get("type", ""))
            if block_type not in ("text", "thinking", "redacted_thinking"):
                self._warn(
                    "content_block_type_ignored",
                    id=self._id,
                    block_type=block_type,
                )
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
        raw_index = parsed.get("index", 0)
        upstream_index = int(raw_index) if raw_index is not None else 0
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
        usage = msg.get("usage")
        if isinstance(usage, dict):
            self._anthropic_usage = merge_anthropic_usage(
                self._anthropic_usage,
                cast("dict[str, Any]", usage),
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
            self._thinking_delta_count += 1
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
        raw_index = parsed.get("index", 0)
        upstream_index = int(raw_index) if raw_index is not None else 0
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
            self._pending_stop_reason = "tool_use"
            if self._transcode_context is not None:
                self._transcode_context.loss_warnings.append(
                    {
                        "kind": "pause_turn",
                        "field": "stop_reason",
                        "to": "tool_calls",
                    }
                )

        if stop:
            if self._pending_stop_reason is not None:
                fr = _STOP_TO_FINISH.get(self._pending_stop_reason, "stop")
            else:
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
                usage_typed = merge_anthropic_usage(
                    self._anthropic_usage,
                    cast("dict[str, Any]", usage),
                )
                self._anthropic_usage = usage_typed
                frame["usage"] = openai_usage_from_anthropic_usage(usage_typed)
            out.append(self._openai_frame(frame))
        elif (
            self._include_usage and isinstance(usage, dict) and not self._emitted_usage
        ):
            self._emitted_usage = True
            usage_typed = merge_anthropic_usage(
                self._anthropic_usage,
                cast("dict[str, Any]", usage),
            )
            self._anthropic_usage = usage_typed
            usage_payload = openai_usage_from_anthropic_usage(usage_typed)
            out.append(
                self._openai_frame(
                    {
                        "id": self._id,
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": self._model,
                        "choices": [],
                        "usage": usage_payload,
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
        sentinel_index = self._next_openai_tool_index
        self._next_openai_tool_index += 1
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
                            # No ``role`` here: the initial chunk already
                            # carried it and strict clients expect role
                            # exactly once per stream.
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": sentinel_index,
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
                                        "index": sentinel_index,
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
