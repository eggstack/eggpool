"""Contract tests for the built-in wire-surface codecs."""

from __future__ import annotations

import json

from eggpool.proxy.sse_observer import IncrementalSSEObserver
from eggpool.request.stream_completion import classify_stream_eof
from eggpool.wire.codecs import (
    AnthropicMessagesCodec,
    GeminiGenerateContentCodec,
    GeminiInteractionsCodec,
    OpenAIChatCodec,
    OpenAIResponsesCodec,
)
from eggpool.wire.ir import (
    CanonicalEvent,
    CanonicalRequest,
    CanonicalUsage,
    ReasoningIntent,
    canonical_request_from_mapping,
)
from eggpool.wire.registry import build_wire_codec
from eggpool.wire.types import ResolvedAuthShape, WireProfile


def _profile(surface: str) -> WireProfile:
    return WireProfile(
        surface=surface,  # type: ignore[arg-type]
        request_codec=surface,
        response_codec=surface,
        stream_codec=surface,
        path_template="/wire",
        stream_path_template=None,
        auth=ResolvedAuthShape("none", "", ""),
    )


def _request(surface: str = "chat_completions") -> CanonicalRequest:
    return canonical_request_from_mapping(
        {
            "model": "model-a",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Call lookup."},
            ],
            "stream": True,
            "max_tokens": 256,
            "temperature": 0.2,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Find a value",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "reasoning_effort": "high",
        },
        client_surface=surface,  # type: ignore[arg-type]
    )


def test_registry_builds_all_five_concrete_codecs() -> None:
    expected = {
        "openai_chat": OpenAIChatCodec,
        "openai_responses": OpenAIResponsesCodec,
        "anthropic_messages": AnthropicMessagesCodec,
        "gemini_interactions": GeminiInteractionsCodec,
        "gemini_generate_content": GeminiGenerateContentCodec,
    }
    for codec_id, codec_type in expected.items():
        assert isinstance(build_wire_codec(codec_id), codec_type)
        assert build_wire_codec(f"{codec_id}_sse").codec_id == f"{codec_id}_sse"


def test_responses_is_a_distinct_stateless_request_and_stream_grammar() -> None:
    codec = OpenAIResponsesCodec()
    request = codec.encode_request(
        _request(), profile=_profile("openai_responses"), capability=None
    )
    assert request["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Call lookup."}],
        }
    ]
    assert request["store"] is False
    assert request["max_output_tokens"] == 256
    assert request["tools"][0]["type"] == "function"  # type: ignore[index]

    events = codec.decode_stream_frame(
        {
            "event": "response.output_text.delta",
            "data": {"type": "response.output_text.delta", "delta": "hello"},
        }
    )
    assert events == (CanonicalEvent("text_delta", delta="hello"),)
    terminal = codec.decode_stream_frame(
        {
            "event": "response.completed",
            "data": {
                "type": "response.completed",
                "response": {
                    "id": "resp-1",
                    "status": "completed",
                    "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                },
            },
        }
    )
    assert terminal[0] == CanonicalEvent(
        "usage",
        usage=CanonicalUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
    )
    assert terminal[-1] == CanonicalEvent(
        "response_complete", finish_reason="completed"
    )
    assert b"response.completed" in codec.encode_event(
        CanonicalEvent("response_complete")
    )


def test_messages_keeps_named_events_and_tool_arguments() -> None:
    codec = AnthropicMessagesCodec()
    response = codec.decode_response(
        {
            "id": "msg-1",
            "model": "claude-test",
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "lookup",
                    "input": {"q": "egg"},
                }
            ],
            "usage": {"input_tokens": 4, "output_tokens": 3},
        }
    )
    encoded = codec.encode_response(response)
    assert encoded["content"][0]["input"] == {"q": "egg"}  # type: ignore[index]
    assert codec.decode_stream_frame({"event": "message_stop"}) == (
        CanonicalEvent("response_complete"),
    )
    assert b"message_stop" in codec.encode_event(CanonicalEvent("response_complete"))


def test_gemini_interactions_is_stateless_and_decodes_step_stream() -> None:
    codec = GeminiInteractionsCodec()
    request = codec.encode_request(
        _request(),
        profile=_profile("gemini_interactions"),
        capability={"thinking_levels": ["high"]},
    )
    assert request["store"] is False
    assert request["system_instruction"] == "Be concise."
    assert request["generation_config"]["thinking_level"] == "high"  # type: ignore[index]
    assert request["tools"][0]["name"] == "lookup"  # type: ignore[index]

    assert codec.decode_stream_frame(
        {
            "event": "step.delta",
            "data": {
                "event_type": "step.delta",
                "index": 1,
                "delta": {"type": "text", "text": "hello"},
            },
        }
    ) == (CanonicalEvent("text_delta", index=1, delta="hello"),)
    completed = codec.decode_stream_frame(
        {
            "event": "interaction.completed",
            "data": {
                "event_type": "interaction.completed",
                "interaction": {
                    "id": "int-1",
                    "model": "gemini-test",
                    "status": "completed",
                    "usage": {
                        "total_input_tokens": 4,
                        "total_output_tokens": 5,
                        "total_tokens": 9,
                    },
                },
            },
        }
    )
    assert completed[-1] == CanonicalEvent(
        "response_complete", finish_reason="completed"
    )


def test_gemini_generate_content_maps_native_parts_and_finish_reasons() -> None:
    codec = GeminiGenerateContentCodec()
    request = codec.encode_request(
        _request(), profile=_profile("gemini_generate_content")
    )
    assert request["contents"][0]["role"] == "user"  # type: ignore[index]
    assert request["generationConfig"]["maxOutputTokens"] == 256  # type: ignore[index]
    assert request["tools"][0]["function_declarations"][0]["name"] == "lookup"  # type: ignore[index]

    events = codec.decode_stream_frame(
        {
            "data": {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "hello"}],
                            "role": "model",
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 2,
                    "candidatesTokenCount": 3,
                    "totalTokenCount": 5,
                },
            }
        }
    )
    assert events[0] == CanonicalEvent("text_delta", delta="hello")
    assert events[-1] == CanonicalEvent("response_complete", finish_reason="STOP")
    encoded = codec.encode_event(CanonicalEvent("text_delta", delta="hello"))
    assert (
        json.loads(encoded.split(b"data: ", 1)[1])["candidates"][0]["content"]["parts"][
            0
        ]["text"]
        == "hello"
    )


def test_reasoning_disable_never_becomes_enable() -> None:
    request = CanonicalRequest(model="model-a", reasoning=ReasoningIntent.disabled())
    assert "reasoning_effort" not in OpenAIChatCodec().encode_request(
        request, profile=_profile("openai_chat_completions")
    )
    assert AnthropicMessagesCodec().encode_request(
        request, profile=_profile("anthropic_messages")
    )["thinking"] == {"type": "disabled"}
    assert GeminiInteractionsCodec().encode_request(
        request, profile=_profile("gemini_interactions")
    )["generation_config"] == {"thinking_summaries": "none"}


def test_native_gemini_stream_usage_and_terminal_are_observed() -> None:
    observer = IncrementalSSEObserver("openai", wire_surface="gemini_generate_content")
    usage_frame = (
        b'data: {"usageMetadata":{"promptTokenCount":2,'
        b'"candidatesTokenCount":3,"totalTokenCount":5}}\n\n'
    )
    observer.observe(usage_frame)
    observer.observe(b'data: {"candidates":[{"finishReason":"STOP"}]}\n\n')
    observer.finish()
    decision = classify_stream_eof(
        protocol="openai",
        policy="strict",
        snapshot=observer.completion_snapshot,
        downstream_started=True,
    )
    assert decision.classification == "complete"
    assert observer.usage.input_tokens == 2
    assert observer.usage.output_tokens == 3


def test_native_interactions_markerless_eof_is_not_completed() -> None:
    observer = IncrementalSSEObserver("openai", wire_surface="gemini_interactions")
    observer.observe(
        b'event: step.delta\ndata: {"delta":{"type":"text","text":"hi"}}\n\n'
    )
    observer.finish()
    decision = classify_stream_eof(
        protocol="openai",
        policy="strict",
        snapshot=observer.completion_snapshot,
        downstream_started=True,
    )
    assert decision.classification == "premature_eof"
