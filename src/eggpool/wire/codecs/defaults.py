"""Concrete codecs for the five built-in upstream wire surfaces.

The codecs deliberately share only the small canonical IR.  In particular,
Responses is not implemented as Chat with a different URL, and Gemini does
not depend on an SDK or a stateful interaction session.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

from eggpool.jsonx import dumps_bytes
from eggpool.wire.codecs.base import WireCodecError
from eggpool.wire.ir import (
    CanonicalContentBlock,
    CanonicalEvent,
    CanonicalOutputBlock,
    CanonicalRequest,
    CanonicalResponse,
    CanonicalUsage,
    ReasoningIntent,
)

if TYPE_CHECKING:
    from eggpool.wire.types import WireProfile


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return cast("Mapping[str, object]", value)


def _sequence(value: object) -> Sequence[object] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    return cast("Sequence[object]", value)


def _payload(frame: Mapping[str, object]) -> Mapping[str, object]:
    data = frame.get("data")
    return _mapping(data) or frame


def _event_name(frame: Mapping[str, object]) -> str | None:
    return (
        _string(frame.get("event"))
        or _string(frame.get("event_type"))
        or _string(frame.get("type"))
    )


def _usage(value: object, *, protocol: str) -> CanonicalUsage | None:
    raw = _mapping(value)
    if raw is None:
        return None
    if protocol == "anthropic":
        prompt = _nonnegative_int(raw.get("input_tokens"))
        completion = _nonnegative_int(raw.get("output_tokens"))
        return CanonicalUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cache_creation_tokens=_nonnegative_int(
                raw.get("cache_creation_input_tokens")
            ),
            cache_read_tokens=_nonnegative_int(raw.get("cache_read_input_tokens")),
        )
    prompt = _nonnegative_int(raw.get("prompt_tokens", raw.get("input_tokens")))
    completion = _nonnegative_int(
        raw.get("completion_tokens", raw.get("output_tokens"))
    )
    total = _nonnegative_int(raw.get("total_tokens")) or prompt + completion
    details = _mapping(raw.get("prompt_tokens_details")) or {}
    return CanonicalUsage(
        prompt,
        completion,
        total,
        cache_creation_tokens=_nonnegative_int(
            details.get("cache_write_tokens") or raw.get("cache_creation_tokens")
        ),
        cache_read_tokens=_nonnegative_int(
            details.get("cached_tokens") or raw.get("cache_read_tokens")
        ),
    )


def _gemini_usage(value: object) -> CanonicalUsage | None:
    raw = _mapping(value)
    if raw is None:
        return None
    prompt = _nonnegative_int(
        raw.get("total_input_tokens", raw.get("promptTokenCount"))
    )
    completion = _nonnegative_int(
        raw.get("total_output_tokens", raw.get("candidatesTokenCount"))
    )
    total = _nonnegative_int(raw.get("total_tokens", raw.get("totalTokenCount")))
    return CanonicalUsage(prompt, completion, total or prompt + completion)


def _nonnegative_int(value: object) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else 0
    )


def _json_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return dumps_bytes(dict(cast("Mapping[str, object]", value))).decode("utf-8")
    return "" if value is None else str(value)


def _sse(event: str, data: Mapping[str, object]) -> bytes:
    return (
        b"event: "
        + event.encode("ascii")
        + b"\ndata: "
        + dumps_bytes(dict(data))
        + b"\n\n"
    )


def _reasoning_mapping(
    intent: ReasoningIntent,
    capability: Mapping[str, object] | None,
    *,
    target: str,
) -> Mapping[str, object] | None:
    """Render only explicit capability mappings; never infer budgets."""
    if intent.requested is False:
        return {"enabled": False} if target == "responses" else None
    if intent.requested is not True:
        return None
    if intent.effort is None:
        return None
    allowed = _sequence(capability.get("reasoning_efforts")) if capability else None
    if allowed is not None and intent.effort not in allowed:
        raise WireCodecError(f"Reasoning effort {intent.effort!r} is not supported")
    if target == "responses":
        return {"effort": intent.effort}
    return None


def _responses_content(
    blocks: Sequence[CanonicalContentBlock], role: str
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    content_type = "output_text" if role == "assistant" else "input_text"
    for block in blocks:
        if block.kind == "text":
            result.append({"type": content_type, "text": block.text or ""})
        elif block.kind == "image" and (block.uri or block.data):
            result.append(
                {
                    "type": "input_image",
                    "image_url": block.uri
                    or (
                        f"data:{block.media_type or 'application/octet-stream'};"
                        f"base64,{block.data}"
                    ),
                }
            )
        elif block.kind == "refusal":
            result.append({"type": "refusal", "refusal": block.text or ""})
    return result


def _response_tools(request: CanonicalRequest) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description or "",
            "parameters": dict(tool.parameters),
            "strict": False,
        }
        for tool in request.tools
    ]


def _openai_tool_choice(request: CanonicalRequest) -> object | None:
    choice = request.tool_choice
    if choice is None:
        return None
    if choice.mode == "function":
        return {"type": "function", "name": choice.function_name or ""}
    return choice.mode


class OpenAIResponsesCodec:
    """Native OpenAI Responses request, response, and event codec."""

    codec_id = "openai_responses"

    def encode_request(
        self,
        request: CanonicalRequest,
        *,
        profile: WireProfile,
        capability: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        del profile
        if request.client_surface == "responses" and request.metadata:
            raise WireCodecError("Stateful Responses controls are not portable")
        result: dict[str, object] = {
            "model": request.model,
            "input": [],
            "stream": request.stream,
            "store": False,
        }
        input_items = cast("list[object]", result["input"])
        for message in request.messages:
            if message.role == "system":
                continue
            tool_results = [
                block for block in message.content if block.kind == "tool_result"
            ]
            tool_calls = [
                block for block in message.content if block.kind == "tool_call"
            ]
            if tool_results:
                for block in tool_results:
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": block.call_id or message.tool_call_id or "",
                            "output": block.text or "",
                        }
                    )
                continue
            for block in tool_calls:
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": block.call_id or "",
                        "name": block.name or "",
                        "arguments": block.arguments or _json_text(block.tool_input),
                    }
                )
            if any(block.kind != "tool_call" for block in message.content):
                input_items.append(
                    {
                        "type": "message",
                        "role": message.role,
                        "content": _responses_content(message.content, message.role),
                    }
                )
        system = next(
            (message for message in request.messages if message.role == "system"), None
        )
        if system is not None:
            result["instructions"] = system.text()
        _add_openai_controls(result, request, max_key="max_output_tokens")
        if request.tools:
            result["tools"] = _response_tools(request)
        tool_choice = _openai_tool_choice(request)
        if tool_choice is not None:
            result["tool_choice"] = tool_choice
        if request.response_format is not None:
            result["text"] = {"format": dict(request.response_format)}
            result.pop("response_format", None)
        reasoning = _reasoning_mapping(
            request.reasoning, capability, target="responses"
        )
        if reasoning is not None:
            result["reasoning"] = dict(reasoning)
        return result

    def decode_response(self, payload: Mapping[str, object]) -> CanonicalResponse:
        response = _mapping(payload.get("response")) or payload
        output: list[CanonicalOutputBlock] = []
        raw_output = _sequence(response.get("output"))
        if raw_output is not None:
            for item in raw_output:
                raw_item = _mapping(item)
                if raw_item is None:
                    continue
                item_type = _string(raw_item.get("type"))
                if item_type == "function_call":
                    output.append(
                        CanonicalOutputBlock(
                            "tool_call",
                            call_id=_string(raw_item.get("call_id")),
                            name=_string(raw_item.get("name")),
                            arguments=_string(raw_item.get("arguments")),
                        )
                    )
                elif item_type == "reasoning":
                    output.extend(_responses_reasoning_blocks(raw_item))
                elif item_type == "message":
                    content = _sequence(raw_item.get("content"))
                    if content is not None:
                        for raw_block in content:
                            block = _mapping(raw_block)
                            if block is None:
                                continue
                            if block.get("type") == "output_text":
                                output.append(
                                    CanonicalOutputBlock(
                                        "text", text=_string(block.get("text"))
                                    )
                                )
                            elif block.get("type") == "refusal":
                                output.append(
                                    CanonicalOutputBlock(
                                        "refusal", text=_string(block.get("refusal"))
                                    )
                                )
        status = _string(response.get("status"))
        return CanonicalResponse(
            model=_string(response.get("model")),
            output=tuple(output),
            finish_reason=status,
            usage=_usage(response.get("usage"), protocol="openai"),
            request_id=_string(response.get("id")),
        )

    def decode_stream_frame(
        self, frame: Mapping[str, object]
    ) -> tuple[CanonicalEvent, ...]:
        if frame.get("data") == "[DONE]" or frame.get("type") == "done":
            return ()
        event = _event_name(frame)
        payload = _payload(frame)
        if event == "response.created":
            response = _mapping(payload.get("response")) or payload
            return (
                CanonicalEvent(
                    "response_start",
                    response_id=_string(response.get("id")),
                    model=_string(response.get("model")),
                ),
            )
        if event in {
            "response.output_text.delta",
            "response.reasoning_summary_text.delta",
        }:
            delta = _string(payload.get("delta"))
            if delta is None:
                return ()
            return (
                CanonicalEvent(
                    "reasoning_delta" if "reasoning" in event else "text_delta",
                    delta=delta,
                ),
            )
        if event == "response.output_item.added":
            item = _mapping(payload.get("item"))
            if item is not None and item.get("type") == "function_call":
                return (
                    CanonicalEvent(
                        "tool_call_start",
                        call_id=_string(item.get("call_id")) or _string(item.get("id")),
                        name=_string(item.get("name")),
                    ),
                )
        if event == "response.function_call_arguments.delta":
            return (
                CanonicalEvent(
                    "tool_call_arguments_delta",
                    call_id=_string(payload.get("item_id")),
                    delta=_string(payload.get("delta")),
                ),
            )
        if event == "response.output_item.done":
            item = _mapping(payload.get("item"))
            if item is not None and item.get("type") == "function_call":
                return (
                    CanonicalEvent(
                        "tool_call_stop",
                        call_id=_string(item.get("call_id")) or _string(item.get("id")),
                    ),
                )
        if event == "response.completed":
            response = _mapping(payload.get("response")) or payload
            usage = _usage(response.get("usage"), protocol="openai")
            return tuple(
                event_value
                for event_value in (
                    CanonicalEvent("usage", usage=usage) if usage else None,
                    CanonicalEvent("response_complete", finish_reason="completed"),
                )
                if event_value is not None
            )
        if event in {"response.failed", "response.incomplete"}:
            response = _mapping(payload.get("response")) or payload
            usage = _usage(response.get("usage"), protocol="openai")
            return tuple(
                event_value
                for event_value in (
                    CanonicalEvent("usage", usage=usage) if usage else None,
                    CanonicalEvent(
                        "response_incomplete",
                        finish_reason=event.removeprefix("response."),
                    ),
                )
                if event_value is not None
            )
        if event == "error":
            error = _mapping(payload.get("error")) or payload
            return (
                CanonicalEvent(
                    "error",
                    error_type=_string(error.get("type")),
                    error_message=_string(error.get("message")),
                ),
            )
        return ()

    def encode_response(self, response: CanonicalResponse) -> Mapping[str, object]:
        output: list[dict[str, object]] = []
        text_blocks = [block for block in response.output if block.kind == "text"]
        if text_blocks:
            output.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "".join(block.text or "" for block in text_blocks),
                        }
                    ],
                }
            )
        for block in response.output:
            if block.kind == "tool_call":
                output.append(
                    {
                        "type": "function_call",
                        "call_id": block.call_id or "",
                        "name": block.name or "",
                        "arguments": block.arguments or "",
                    }
                )
            elif block.kind == "reasoning":
                output.append(
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": block.text or ""}],
                    }
                )
        result: dict[str, object] = {
            "id": response.request_id or "",
            "object": "response",
            "status": response.finish_reason or "completed",
            "model": response.model or "",
            "output": output,
        }
        if response.usage is not None:
            result["usage"] = {
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return result

    def encode_event(self, event: CanonicalEvent) -> bytes:
        if event.type == "response_complete":
            return _sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": event.response_id or "",
                        "status": "completed",
                        "usage": _openai_usage(event.usage),
                    },
                },
            )
        if event.type == "response_incomplete":
            return _sse(
                "response.incomplete",
                {
                    "type": "response.incomplete",
                    "response": {"id": event.response_id or "", "status": "incomplete"},
                },
            )
        if event.type == "error":
            return _sse(
                "error",
                {
                    "type": "error",
                    "error": {
                        "type": event.error_type or "error",
                        "message": event.error_message or "",
                    },
                },
            )
        if event.type == "text_delta":
            return _sse(
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": event.delta or ""},
            )
        if event.type == "reasoning_delta":
            return _sse(
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "delta": event.delta or "",
                },
            )
        if event.type == "tool_call_arguments_delta":
            return _sse(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": event.call_id or "",
                    "delta": event.delta or "",
                },
            )
        if event.type == "tool_call_start":
            return _sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "function_call",
                        "call_id": event.call_id or "",
                        "name": event.name or "",
                        "arguments": "",
                    },
                },
            )
        return b""


def _responses_reasoning_blocks(
    item: Mapping[str, object],
) -> list[CanonicalOutputBlock]:
    summary = _sequence(item.get("summary"))
    if summary is None:
        return []
    return [
        CanonicalOutputBlock("reasoning", text=_string(block.get("text")))
        for raw in summary
        if (block := _mapping(raw)) is not None and block.get("type") == "summary_text"
    ]


def _openai_usage(usage: CanonicalUsage | None) -> dict[str, object] | None:
    if usage is None:
        return None
    result: dict[str, object] = {
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    if usage.cache_read_tokens or usage.cache_creation_tokens:
        result["prompt_tokens_details"] = {
            "cached_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_creation_tokens,
        }
    return result


class GeminiInteractionsCodec:
    """Stateless Gemini Interactions codec using the documented step stream."""

    codec_id = "gemini_interactions"

    def encode_request(
        self,
        request: CanonicalRequest,
        *,
        profile: WireProfile,
        capability: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        del profile
        if request.metadata:
            raise WireCodecError(
                "Stateful Gemini interaction controls are not portable"
            )
        result: dict[str, object] = {
            "model": request.model,
            "input": _gemini_input(request),
            "stream": request.stream,
            "store": False,
        }
        system = next(
            (message for message in request.messages if message.role == "system"), None
        )
        if system is not None:
            result["system_instruction"] = system.text()
        if request.tools:
            result["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": dict(tool.parameters),
                }
                for tool in request.tools
            ]
        generation: dict[str, object] = {}
        if request.max_output_tokens is not None:
            generation["max_output_tokens"] = request.max_output_tokens
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.top_p is not None:
            generation["top_p"] = request.top_p
        if request.stop is not None:
            generation["stop_sequences"] = list(request.stop)
        if request.reasoning.requested is True and request.reasoning.effort is not None:
            allowed = (
                _sequence(capability.get("thinking_levels")) if capability else None
            )
            if allowed is not None and request.reasoning.effort not in allowed:
                raise WireCodecError(
                    f"Thinking level {request.reasoning.effort!r} is not supported"
                )
            generation["thinking_level"] = request.reasoning.effort
            generation["thinking_summaries"] = "auto"
        elif request.reasoning.requested is False:
            generation["thinking_summaries"] = "none"
        if generation:
            result["generation_config"] = generation
        if request.response_format is not None:
            result["response_format"] = dict(request.response_format)
        return result

    def decode_response(self, payload: Mapping[str, object]) -> CanonicalResponse:
        interaction = _mapping(payload.get("interaction")) or payload
        output: list[CanonicalOutputBlock] = []
        steps = _sequence(interaction.get("steps"))
        if steps is not None:
            for raw_step in steps:
                step = _mapping(raw_step)
                if step is None:
                    continue
                step_type = _string(step.get("type"))
                if step_type in {"model_output", "thought"}:
                    for raw_block in _sequence(step.get("content")) or ():
                        block = _mapping(raw_block)
                        if block is None:
                            continue
                        text = _string(block.get("text"))
                        if text:
                            output.append(
                                CanonicalOutputBlock(
                                    "reasoning" if step_type == "thought" else "text",
                                    text=text,
                                )
                            )
                elif step_type == "function_call":
                    output.append(
                        CanonicalOutputBlock(
                            "tool_call",
                            call_id=_string(step.get("id")),
                            name=_string(step.get("name")),
                            arguments=_json_text(step.get("arguments")),
                        )
                    )
        return CanonicalResponse(
            model=_string(interaction.get("model")),
            output=tuple(output),
            finish_reason=_string(interaction.get("status")),
            usage=_gemini_usage(interaction.get("usage")),
            request_id=_string(interaction.get("id")),
        )

    def decode_stream_frame(
        self, frame: Mapping[str, object]
    ) -> tuple[CanonicalEvent, ...]:
        event = _event_name(frame)
        payload = _payload(frame)
        if event == "interaction.created":
            interaction = _mapping(payload.get("interaction")) or payload
            return (
                CanonicalEvent(
                    "response_start",
                    response_id=_string(interaction.get("id")),
                    model=_string(interaction.get("model")),
                ),
            )
        if event == "step.start":
            step = _mapping(payload.get("step"))
            if step is not None and step.get("type") == "function_call":
                return (
                    CanonicalEvent(
                        "tool_call_start",
                        index=_int(payload.get("index")),
                        call_id=_string(step.get("id")),
                        name=_string(step.get("name")),
                    ),
                )
            return (CanonicalEvent("content_start", index=_int(payload.get("index"))),)
        if event == "step.delta":
            delta = _mapping(payload.get("delta"))
            if delta is None:
                return ()
            kind = _string(delta.get("type"))
            if kind == "text":
                return (
                    CanonicalEvent(
                        "text_delta",
                        index=_int(payload.get("index")),
                        delta=_string(delta.get("text")),
                    ),
                )
            if kind == "thought_summary":
                content = _mapping(delta.get("content"))
                return (
                    CanonicalEvent(
                        "reasoning_delta",
                        index=_int(payload.get("index")),
                        delta=_string(content.get("text")) if content else None,
                    ),
                )
            if kind == "arguments_delta":
                return (
                    CanonicalEvent(
                        "tool_call_arguments_delta",
                        index=_int(payload.get("index")),
                        delta=_string(delta.get("arguments")),
                    ),
                )
        if event == "step.stop":
            return (CanonicalEvent("content_stop", index=_int(payload.get("index"))),)
        if event == "interaction.completed":
            interaction = _mapping(payload.get("interaction")) or payload
            usage = _gemini_usage(interaction.get("usage"))
            status = _string(interaction.get("status"))
            terminal = (
                "response_complete"
                if status in {"completed", "requires_action"}
                else "response_incomplete"
            )
            return tuple(
                event_value
                for event_value in (
                    CanonicalEvent("usage", usage=usage) if usage else None,
                    CanonicalEvent(terminal, finish_reason=status),
                )
                if event_value is not None
            )
        if event == "error":
            error = _mapping(payload.get("error")) or payload
            return (
                CanonicalEvent(
                    "error",
                    error_type=_string(error.get("type")),
                    error_message=_string(error.get("message")),
                ),
            )
        return ()

    def encode_response(self, response: CanonicalResponse) -> Mapping[str, object]:
        steps: list[dict[str, object]] = []
        for block in response.output:
            if block.kind == "text":
                steps.append(
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": block.text or ""}],
                    }
                )
            elif block.kind == "reasoning":
                steps.append(
                    {
                        "type": "thought",
                        "content": [{"type": "text", "text": block.text or ""}],
                    }
                )
            elif block.kind == "tool_call":
                steps.append(
                    {
                        "type": "function_call",
                        "id": block.call_id or "",
                        "name": block.name or "",
                        "arguments": _json_value(block.arguments),
                    }
                )
        result: dict[str, object] = {
            "id": response.request_id or "",
            "object": "interaction",
            "model": response.model or "",
            "status": response.finish_reason or "completed",
            "steps": steps,
        }
        if response.usage is not None:
            result["usage"] = {
                "total_input_tokens": response.usage.prompt_tokens,
                "total_output_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return result

    def encode_event(self, event: CanonicalEvent) -> bytes:
        if event.type == "response_complete":
            return _sse(
                "interaction.completed",
                {
                    "event_type": "interaction.completed",
                    "interaction": {
                        "id": event.response_id or "",
                        "status": "completed",
                        "usage": _gemini_event_usage(event.usage),
                    },
                },
            )
        if event.type == "response_incomplete":
            return _sse(
                "interaction.completed",
                {
                    "event_type": "interaction.completed",
                    "interaction": {
                        "id": event.response_id or "",
                        "status": event.finish_reason or "incomplete",
                    },
                },
            )
        if event.type == "text_delta":
            return _sse(
                "step.delta",
                {
                    "event_type": "step.delta",
                    "index": event.index or 0,
                    "delta": {"type": "text", "text": event.delta or ""},
                },
            )
        if event.type == "reasoning_delta":
            return _sse(
                "step.delta",
                {
                    "event_type": "step.delta",
                    "index": event.index or 0,
                    "delta": {
                        "type": "thought_summary",
                        "content": {"type": "text", "text": event.delta or ""},
                    },
                },
            )
        if event.type == "tool_call_arguments_delta":
            return _sse(
                "step.delta",
                {
                    "event_type": "step.delta",
                    "index": event.index or 0,
                    "delta": {
                        "type": "arguments_delta",
                        "arguments": event.delta or "",
                    },
                },
            )
        return b""


def _gemini_input(request: CanonicalRequest) -> object:
    messages = [message for message in request.messages if message.role != "system"]
    if (
        len(messages) == 1
        and len(messages[0].content) == 1
        and messages[0].content[0].kind == "text"
    ):
        return messages[0].content[0].text or ""
    return [
        {
            "role": "model" if message.role == "assistant" else "user",
            "parts": _gemini_parts(message.content),
        }
        for message in messages
    ]


def _gemini_parts(blocks: Sequence[CanonicalContentBlock]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for block in blocks:
        if block.kind == "text":
            result.append({"text": block.text or ""})
        elif block.kind == "reasoning":
            result.append({"text": block.text or "", "thought": True})
        elif block.kind == "image" and block.data:
            result.append(
                {
                    "inline_data": {
                        "mime_type": block.media_type or "application/octet-stream",
                        "data": block.data,
                    }
                }
            )
        elif block.kind == "image" and block.uri:
            result.append(
                {
                    "file_data": {
                        "mime_type": block.media_type or "application/octet-stream",
                        "file_uri": block.uri,
                    }
                }
            )
        elif block.kind == "tool_call":
            function_call: dict[str, object] = {
                "name": block.name or "",
                "args": _json_value(block.arguments),
            }
            if block.call_id:
                function_call["id"] = block.call_id
            result.append({"functionCall": function_call})
        elif block.kind == "tool_result":
            response: dict[str, object] = {"result": block.text or ""}
            if block.call_id:
                response["id"] = block.call_id
            result.append(
                {
                    "functionResponse": {
                        "name": block.name or "",
                        "response": response,
                    }
                }
            )
    return result


def _json_value(value: str | None) -> object:
    if not value:
        return {}
    try:
        import json

        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _gemini_event_usage(usage: CanonicalUsage | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "total_input_tokens": usage.prompt_tokens,
        "total_output_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


class GeminiGenerateContentCodec:
    """Native Gemini generateContent codec for unary and SSE responses."""

    codec_id = "gemini_generate_content"

    def encode_request(
        self,
        request: CanonicalRequest,
        *,
        profile: WireProfile,
        capability: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        del profile, capability
        result: dict[str, object] = {"contents": []}
        contents = cast("list[object]", result["contents"])
        system = next(
            (message for message in request.messages if message.role == "system"), None
        )
        if system is not None:
            result["systemInstruction"] = {"parts": [{"text": system.text()}]}
        for message in request.messages:
            if message.role != "system":
                contents.append(
                    {
                        "role": "model" if message.role == "assistant" else "user",
                        "parts": _gemini_parts(message.content),
                    }
                )
        generation: dict[str, object] = {}
        if request.max_output_tokens is not None:
            generation["maxOutputTokens"] = request.max_output_tokens
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.top_p is not None:
            generation["topP"] = request.top_p
        if request.stop is not None:
            generation["stopSequences"] = list(request.stop)
        if request.response_format is not None:
            fmt = dict(request.response_format)
            if fmt.get("type") in {"json_object", "json_schema"}:
                generation["responseMimeType"] = "application/json"
                schema = _mapping(fmt.get("json_schema"))
                nested_schema = _mapping(schema.get("schema")) if schema else None
                if nested_schema is not None:
                    generation["responseSchema"] = dict(nested_schema)
        if request.reasoning.requested is False:
            generation["thinkingConfig"] = {"thinkingBudget": 0}
        elif (
            request.reasoning.mode == "fixed_budget"
            and request.reasoning.budget_tokens is not None
        ):
            generation["thinkingConfig"] = {
                "thinkingBudget": request.reasoning.budget_tokens
            }
        if generation:
            result["generationConfig"] = generation
        if request.tools:
            result["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": dict(tool.parameters),
                        }
                        for tool in request.tools
                    ]
                }
            ]
        if request.tool_choice is not None:
            mode = {
                "auto": "AUTO",
                "none": "NONE",
                "required": "ANY",
                "function": "ANY",
            }[request.tool_choice.mode]
            function_names = (
                [request.tool_choice.function_name]
                if request.tool_choice.mode == "function"
                and request.tool_choice.function_name
                else None
            )
            function_calling: dict[str, object] = {"mode": mode}
            if function_names:
                function_calling["allowedFunctionNames"] = function_names
            result["toolConfig"] = {"functionCallingConfig": function_calling}
        return result

    def decode_response(self, payload: Mapping[str, object]) -> CanonicalResponse:
        output, finish = _generate_output(payload)
        return CanonicalResponse(
            model=_string(payload.get("modelVersion")),
            output=tuple(output),
            finish_reason=finish,
            usage=_gemini_usage(payload.get("usageMetadata")),
            request_id=_string(payload.get("responseId")),
        )

    def decode_stream_frame(
        self, frame: Mapping[str, object]
    ) -> tuple[CanonicalEvent, ...]:
        payload = _payload(frame)
        output, finish = _generate_output(payload)
        events: list[CanonicalEvent] = []
        for block in output:
            if block.kind == "text":
                events.append(CanonicalEvent("text_delta", delta=block.text))
            elif block.kind == "reasoning":
                events.append(CanonicalEvent("reasoning_delta", delta=block.text))
            elif block.kind == "tool_call":
                events.append(
                    CanonicalEvent(
                        "tool_call_start", call_id=block.call_id, name=block.name
                    )
                )
                events.append(
                    CanonicalEvent(
                        "tool_call_arguments_delta",
                        call_id=block.call_id,
                        delta=block.arguments,
                    )
                )
        usage = _gemini_usage(payload.get("usageMetadata"))
        if usage:
            events.append(CanonicalEvent("usage", usage=usage))
        if finish is not None:
            events.append(
                CanonicalEvent(
                    "response_complete" if finish == "STOP" else "response_incomplete",
                    finish_reason=finish,
                )
            )
        return tuple(events)

    def encode_response(self, response: CanonicalResponse) -> Mapping[str, object]:
        parts: list[dict[str, object]] = []
        for block in response.output:
            if block.kind == "text":
                parts.append({"text": block.text or ""})
            elif block.kind == "tool_call":
                parts.append(
                    {
                        "functionCall": {
                            "name": block.name or "",
                            "args": _json_value(block.arguments),
                        }
                    }
                )
        result: dict[str, object] = {
            "candidates": [
                {
                    "content": {"role": "model", "parts": parts},
                    "finishReason": response.finish_reason or "STOP",
                }
            ]
        }
        if response.usage is not None:
            result["usageMetadata"] = {
                "promptTokenCount": response.usage.prompt_tokens,
                "candidatesTokenCount": response.usage.completion_tokens,
                "totalTokenCount": response.usage.total_tokens,
            }
        return result

    def encode_event(self, event: CanonicalEvent) -> bytes:
        parts: list[dict[str, object]] = []
        candidate: dict[str, object] = {"content": {"role": "model", "parts": parts}}
        payload: dict[str, object] = {"candidates": [candidate]}
        if event.type == "text_delta":
            parts.append({"text": event.delta or ""})
        elif event.type == "tool_call_arguments_delta":
            parts.append(
                {
                    "functionCall": {
                        "name": event.name or "",
                        "args": _json_value(event.delta),
                    }
                }
            )
        elif event.type == "response_complete":
            candidate["finishReason"] = "STOP"
        elif event.type == "response_incomplete":
            candidate["finishReason"] = event.finish_reason or "MAX_TOKENS"
        elif event.type == "usage" and event.usage is not None:
            payload["usageMetadata"] = {
                "promptTokenCount": event.usage.prompt_tokens,
                "candidatesTokenCount": event.usage.completion_tokens,
                "totalTokenCount": event.usage.total_tokens,
            }
        else:
            return b""
        return b"data: " + dumps_bytes(payload) + b"\n\n"


def _generate_output(
    payload: Mapping[str, object],
) -> tuple[list[CanonicalOutputBlock], str | None]:
    output: list[CanonicalOutputBlock] = []
    finish: str | None = None
    candidates = _sequence(payload.get("candidates"))
    if candidates:
        candidate = _mapping(candidates[0])
        if candidate is not None:
            finish = _string(candidate.get("finishReason"))
            content = _mapping(candidate.get("content"))
            parts = _sequence(content.get("parts")) if content else None
            if parts is not None:
                for raw_part in parts:
                    part = _mapping(raw_part)
                    if part is None:
                        continue
                    text = _string(part.get("text"))
                    if text:
                        output.append(
                            CanonicalOutputBlock(
                                "reasoning" if part.get("thought") is True else "text",
                                text=text,
                            )
                        )
                    call = _mapping(part.get("functionCall"))
                    if call is not None:
                        output.append(
                            CanonicalOutputBlock(
                                "tool_call",
                                call_id=_string(call.get("id")),
                                name=_string(call.get("name")),
                                arguments=_json_text(call.get("args")),
                            )
                        )
    return output, finish


def _add_openai_controls(
    result: dict[str, object], request: CanonicalRequest, *, max_key: str
) -> None:
    if request.max_output_tokens is not None:
        result[max_key] = request.max_output_tokens
    if request.temperature is not None:
        result["temperature"] = request.temperature
    if request.top_p is not None:
        result["top_p"] = request.top_p
    if request.stop is not None:
        result["stop"] = (
            request.stop[0] if len(request.stop) == 1 else list(request.stop)
        )


__all__ = [
    "GeminiGenerateContentCodec",
    "GeminiInteractionsCodec",
    "OpenAIResponsesCodec",
]
