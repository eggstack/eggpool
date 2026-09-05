# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false

"""Canonical adapters for the OpenAI Chat and Anthropic Messages surfaces.

These adapters cover the common semantic subset and deliberately accept
already-decoded frames. They are the public/client-side Chat and Messages
codecs used by the canonical wire bridge; the mature field-level transcoders
remain responsible for their richer legacy compatibility behavior.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from eggpool.jsonx import dumps_bytes
from eggpool.wire.ir import (
    CanonicalEvent,
    CanonicalOutputBlock,
    CanonicalRequest,
    CanonicalResponse,
    CanonicalUsage,
    canonical_request_to_mapping,
)

if TYPE_CHECKING:
    from eggpool.wire.types import WireProfile


class OpenAIChatCodec:
    """Canonical request/response/event adapter for Chat Completions."""

    codec_id = "openai_chat"

    def encode_request(
        self,
        request: CanonicalRequest,
        *,
        profile: WireProfile,
        capability: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        del profile, capability
        result = canonical_request_to_mapping(request, surface="chat_completions")
        if request.reasoning.requested is True and request.reasoning.effort:
            result["reasoning_effort"] = request.reasoning.effort
        elif request.reasoning.requested is False:
            result.pop("reasoning_effort", None)
        return result

    def decode_response(self, payload: Mapping[str, object]) -> CanonicalResponse:
        choices = payload.get("choices")
        output: list[CanonicalOutputBlock] = []
        finish_reason: str | None = None
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            choice = choices[0]
            finish_reason = _string(choice.get("finish_reason"))
            message = choice.get("message")
            if isinstance(message, Mapping):
                content = message.get("content")
                if isinstance(content, str) and content:
                    output.append(CanonicalOutputBlock("text", text=content))
                reasoning = message.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    output.append(CanonicalOutputBlock("reasoning", text=reasoning))
                _append_openai_tool_calls(output, message.get("tool_calls"))
        return CanonicalResponse(
            model=_string(payload.get("model")),
            output=tuple(output),
            finish_reason=finish_reason,
            usage=_usage(payload.get("usage"), protocol="openai"),
            request_id=_string(payload.get("id")),
        )

    def decode_stream_frame(
        self,
        frame: Mapping[str, object],
    ) -> tuple[CanonicalEvent, ...]:
        if frame.get("data") == "[DONE]" or frame.get("done") is True:
            return (CanonicalEvent("response_complete"),)
        payload = _frame_payload(frame)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            usage = _usage(payload.get("usage"), protocol="openai")
            return (CanonicalEvent("usage", usage=usage),) if usage else ()
        choice = choices[0]
        if not isinstance(choice, Mapping):
            return ()
        delta = choice.get("delta")
        events: list[CanonicalEvent] = []
        if isinstance(delta, Mapping):
            text = delta.get("content")
            if isinstance(text, str) and text:
                events.append(CanonicalEvent("text_delta", delta=text))
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                events.append(CanonicalEvent("reasoning_delta", delta=reasoning))
            _append_openai_tool_events(events, delta.get("tool_calls"))
        finish_reason = _string(choice.get("finish_reason"))
        if finish_reason is not None:
            events.append(
                CanonicalEvent("response_complete", finish_reason=finish_reason)
            )
        return tuple(events)

    def encode_response(self, response: CanonicalResponse) -> Mapping[str, object]:
        message: dict[str, object] = {"role": "assistant", "content": None}
        text = "".join(
            block.text or "" for block in response.output if block.kind == "text"
        )
        message["content"] = text
        reasoning = "".join(
            block.text or "" for block in response.output if block.kind == "reasoning"
        )
        if reasoning:
            message["reasoning_content"] = reasoning
        tool_calls = [block for block in response.output if block.kind == "tool_call"]
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": block.call_id or "",
                    "type": "function",
                    "function": {
                        "name": block.name or "",
                        "arguments": block.arguments or "",
                    },
                }
                for block in tool_calls
            ]
        result: dict[str, object] = {
            "id": response.request_id or "",
            "object": "chat.completion",
            "model": response.model or "",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": _translate_reason(
                        response.finish_reason,
                        {
                            "end_turn": "stop",
                            "stop_sequence": "stop",
                            "max_tokens": "length",
                            "tool_use": "tool_calls",
                            "pause_turn": "tool_calls",
                            "refusal": "content_filter",
                        },
                    ),
                }
            ],
        }
        if response.usage is not None:
            result["usage"] = response.usage.to_dict()
        return result

    def encode_event(self, event: CanonicalEvent) -> bytes:
        if event.type == "response_complete":
            return b"data: [DONE]\n\n"
        delta: dict[str, object] = {}
        if event.type in {"text_delta", "reasoning_delta"}:
            delta["content" if event.type == "text_delta" else "reasoning_content"] = (
                event.delta or ""
            )
        elif event.type == "tool_call_start":
            delta["tool_calls"] = [
                {
                    "index": event.index or 0,
                    "id": event.call_id or "",
                    "type": "function",
                    "function": {"name": event.name or "", "arguments": ""},
                }
            ]
        elif event.type == "tool_call_arguments_delta":
            delta["tool_calls"] = [
                {
                    "index": event.index or 0,
                    "function": {"arguments": event.delta or ""},
                }
            ]
        else:
            delta = {}
        payload: dict[str, object] = {
            "id": event.response_id or "",
            "object": "chat.completion.chunk",
            "model": event.model or "",
            "choices": [
                {
                    "index": event.index or 0,
                    "delta": delta,
                    "finish_reason": event.finish_reason,
                }
            ],
        }
        if event.type == "usage" and event.usage is not None:
            payload["usage"] = event.usage.to_dict()
        return b"data: " + dumps_bytes(payload) + b"\n\n"


class AnthropicMessagesCodec:
    """Canonical request/response/event adapter for Messages."""

    codec_id = "anthropic_messages"

    def encode_request(
        self,
        request: CanonicalRequest,
        *,
        profile: WireProfile,
        capability: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        del profile, capability
        result = canonical_request_to_mapping(request, surface="messages")
        if request.reasoning.requested is False:
            result["thinking"] = {"type": "disabled"}
        return result

    def decode_response(self, payload: Mapping[str, object]) -> CanonicalResponse:
        output: list[CanonicalOutputBlock] = []
        content = payload.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                kind = block.get("type")
                if kind == "text":
                    output.append(
                        CanonicalOutputBlock("text", text=_string(block.get("text")))
                    )
                elif kind == "thinking":
                    output.append(
                        CanonicalOutputBlock(
                            "reasoning", text=_string(block.get("thinking"))
                        )
                    )
                elif kind == "tool_use":
                    output.append(
                        CanonicalOutputBlock(
                            "tool_call",
                            call_id=_string(block.get("id")),
                            name=_string(block.get("name")),
                            arguments=_json_arguments(block.get("input")),
                        )
                    )
        stop = payload.get("stop_reason")
        return CanonicalResponse(
            model=_string(payload.get("model")),
            output=tuple(output),
            finish_reason=_string(stop),
            usage=_usage(payload.get("usage"), protocol="anthropic"),
            request_id=_string(payload.get("id")),
        )

    def decode_stream_frame(
        self,
        frame: Mapping[str, object],
    ) -> tuple[CanonicalEvent, ...]:
        event = _string(frame.get("event")) or _string(frame.get("type"))
        payload = _frame_payload(frame)
        if event == "message_start":
            message = payload.get("message")
            return (
                CanonicalEvent(
                    "response_start",
                    response_id=_string(message.get("id"))
                    if isinstance(message, Mapping)
                    else None,
                    model=_string(message.get("model"))
                    if isinstance(message, Mapping)
                    else None,
                ),
            )
        if event == "content_block_start":
            block = payload.get("content_block")
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                return (
                    CanonicalEvent(
                        "tool_call_start",
                        index=_int(payload.get("index")),
                        call_id=_string(block.get("id")),
                        name=_string(block.get("name")),
                    ),
                )
            return (CanonicalEvent("content_start", index=_int(payload.get("index"))),)
        if event == "content_block_delta":
            delta = payload.get("delta")
            if not isinstance(delta, Mapping):
                return ()
            delta_type = delta.get("type")
            if delta_type in {"text_delta", "thinking_delta"}:
                return (
                    CanonicalEvent(
                        "reasoning_delta"
                        if delta_type == "thinking_delta"
                        else "text_delta",
                        index=_int(payload.get("index")),
                        delta=_string(delta.get("text"))
                        or _string(delta.get("thinking")),
                    ),
                )
            if delta_type == "input_json_delta":
                return (
                    CanonicalEvent(
                        "tool_call_arguments_delta",
                        index=_int(payload.get("index")),
                        delta=_string(delta.get("partial_json")),
                    ),
                )
        if event == "content_block_stop":
            return (CanonicalEvent("content_stop", index=_int(payload.get("index"))),)
        if event == "message_delta":
            delta = payload.get("delta")
            stop = (
                _string(delta.get("stop_reason"))
                if isinstance(delta, Mapping)
                else None
            )
            usage = _usage(payload.get("usage"), protocol="anthropic")
            return tuple(
                event
                for event in (
                    CanonicalEvent("usage", usage=usage) if usage else None,
                    CanonicalEvent("response_complete", finish_reason=stop)
                    if stop
                    else None,
                )
                if event is not None
            )
        if event == "message_stop":
            return (CanonicalEvent("response_complete"),)
        if event == "error":
            error = payload.get("error")
            return (
                CanonicalEvent(
                    "error",
                    error_type=_string(error.get("type"))
                    if isinstance(error, Mapping)
                    else None,
                    error_message=_string(error.get("message"))
                    if isinstance(error, Mapping)
                    else None,
                ),
            )
        return ()

    def encode_response(self, response: CanonicalResponse) -> Mapping[str, object]:
        content: list[dict[str, object]] = []
        for block in response.output:
            if block.kind == "text":
                content.append({"type": "text", "text": block.text or ""})
            elif block.kind == "reasoning":
                content.append({"type": "thinking", "thinking": block.text or ""})
            elif block.kind == "tool_call":
                content.append(
                    {
                        "type": "tool_use",
                        "id": block.call_id or "",
                        "name": block.name or "",
                        "input": _json_input(block.arguments),
                    }
                )
        result: dict[str, object] = {
            "id": response.request_id or "",
            "type": "message",
            "role": "assistant",
            "model": response.model or "",
            "content": content,
            "stop_reason": _translate_reason(
                response.finish_reason,
                {
                    "stop": "end_turn",
                    "length": "max_tokens",
                    "tool_calls": "tool_use",
                    "content_filter": "refusal",
                },
            ),
        }
        if response.usage is not None:
            result["usage"] = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "cache_read_input_tokens": response.usage.cache_read_tokens,
                "cache_creation_input_tokens": response.usage.cache_creation_tokens,
            }
        return result

    def encode_event(self, event: CanonicalEvent) -> bytes:
        if event.type == "response_complete":
            return _anthropic_sse("message_stop", {})
        if event.type == "text_delta":
            return _anthropic_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": event.index or 0,
                    "delta": {"type": "text_delta", "text": event.delta or ""},
                },
            )
        if event.type == "reasoning_delta":
            delta = {"type": "thinking_delta", "thinking": event.delta or ""}
        elif event.type == "tool_call_arguments_delta":
            delta = {"type": "input_json_delta", "partial_json": event.delta or ""}
        else:
            delta = None
        if delta is not None:
            return _anthropic_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": event.index or 0,
                    "delta": delta,
                },
            )
        if event.type == "usage" and event.usage is not None:
            return _anthropic_sse(
                "message_delta",
                {
                    "usage": {
                        "input_tokens": event.usage.prompt_tokens,
                        "output_tokens": event.usage.completion_tokens,
                        "cache_read_input_tokens": event.usage.cache_read_tokens,
                        "cache_creation_input_tokens": (
                            event.usage.cache_creation_tokens
                        ),
                    }
                },
            )
        return _anthropic_sse(event.type, {})


def _append_openai_tool_calls(
    output: list[CanonicalOutputBlock],
    value: object,
) -> None:
    if not isinstance(value, list):
        return
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        function = raw.get("function")
        if not isinstance(function, Mapping):
            continue
        output.append(
            CanonicalOutputBlock(
                "tool_call",
                call_id=_string(raw.get("id")),
                name=_string(function.get("name")),
                arguments=_string(function.get("arguments")),
            )
        )


def _append_openai_tool_events(events: list[CanonicalEvent], value: object) -> None:
    if not isinstance(value, list):
        return
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        function = raw.get("function")
        if not isinstance(function, Mapping):
            continue
        index = _int(raw.get("index"))
        call_id = _string(raw.get("id"))
        name = _string(function.get("name"))
        arguments = _string(function.get("arguments"))
        if call_id is not None or name is not None:
            events.append(
                CanonicalEvent(
                    "tool_call_start", index=index, call_id=call_id, name=name
                )
            )
        if arguments:
            events.append(
                CanonicalEvent(
                    "tool_call_arguments_delta",
                    index=index,
                    call_id=call_id,
                    delta=arguments,
                )
            )


def _frame_payload(frame: Mapping[str, object]) -> Mapping[str, object]:
    data = frame.get("data")
    return data if isinstance(data, Mapping) else frame


def _usage(value: object, *, protocol: str):
    if not isinstance(value, Mapping):
        return None
    raw = cast("Mapping[str, Any]", value)
    if protocol == "anthropic":
        prompt = _token_count(raw.get("input_tokens"))
        completion = _token_count(raw.get("output_tokens"))
        return CanonicalUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cache_creation_tokens=_token_count(raw.get("cache_creation_input_tokens")),
            cache_read_tokens=_token_count(raw.get("cache_read_input_tokens")),
        )
    prompt = _token_count(raw.get("prompt_tokens"))
    completion = _token_count(raw.get("completion_tokens"))
    total = _token_count(raw.get("total_tokens")) or prompt + completion
    details = raw.get("prompt_tokens_details")
    details_mapping = details if isinstance(details, Mapping) else {}
    return CanonicalUsage(
        prompt,
        completion,
        total,
        cache_creation_tokens=_token_count(
            details_mapping.get("cache_write_tokens")
            or raw.get("cache_creation_tokens")
        ),
        cache_read_tokens=_token_count(
            details_mapping.get("cached_tokens") or raw.get("cache_read_tokens")
        ),
    )


def _token_count(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _translate_reason(value: str | None, translations: Mapping[str, str]) -> str | None:
    return None if value is None else translations.get(value, value)


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _json_arguments(value: object) -> str:
    if isinstance(value, Mapping):
        return dumps_bytes(dict(value)).decode("utf-8")
    return "" if value is None else str(value)


def _json_input(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    import json

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


def _anthropic_sse(event: str, data: Mapping[str, object]) -> bytes:
    payload = {"type": event, **data}
    return (
        b"event: "
        + event.encode("ascii")
        + b"\ndata: "
        + dumps_bytes(payload)
        + b"\n\n"
    )


__all__ = ["AnthropicMessagesCodec", "OpenAIChatCodec"]
