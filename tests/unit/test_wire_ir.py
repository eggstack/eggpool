"""Focused contracts for the canonical wire-independent IR."""

from __future__ import annotations

import pytest

from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.usage import CanonicalUsage
from eggpool.wire.codecs import (
    AnthropicMessagesCodec,
    CodecRegistry,
    OpenAIChatCodec,
    PassthroughCodec,
    WireCodecError,
)
from eggpool.wire.ir import (
    CanonicalEvent,
    CanonicalTool,
    ReasoningIntent,
    canonical_request_from_mapping,
    canonical_request_to_mapping,
)


def test_reasoning_intent_keeps_effort_separate_from_budget() -> None:
    effort = ReasoningIntent.from_openai_effort("xhigh")
    assert effort.requested is True
    assert effort.mode == "effort"
    assert effort.effort == "xhigh"
    assert effort.budget_tokens is None

    disabled = ReasoningIntent.from_openai_effort("none")
    assert disabled == ReasoningIntent.disabled()


def test_fixed_budget_is_not_accepted_as_effort() -> None:
    request = canonical_request_from_mapping(
        {
            "model": "model-a",
            "messages": [{"role": "user", "content": "hello"}],
            "thinking": {"type": "enabled", "budget_tokens": 2048},
        },
        client_surface="messages",
        protocol="anthropic",
    )
    assert request.reasoning == ReasoningIntent.fixed(2048)
    assert request.reasoning.effort is None


def test_chat_request_round_trips_common_messages_tools_and_controls() -> None:
    request = canonical_request_from_mapping(
        {
            "model": "model-a",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Call the tool."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"q":"egg"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": "result",
                },
            ],
            "max_completion_tokens": 300,
            "temperature": 0.2,
            "top_p": 0.8,
            "stop": ["END"],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Look up a value",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
            "reasoning_effort": "high",
        }
    )

    assert (
        next(
            block for block in request.messages[2].content if block.kind == "tool_call"
        ).call_id
        == "call-1"
    )
    assert request.tools == (
        CanonicalTool(
            "lookup",
            "Look up a value",
            {"type": "object"},
        ),
    )
    assert request.reasoning.effort == "high"
    rendered = canonical_request_to_mapping(request, surface="chat_completions")
    assert rendered["max_completion_tokens"] == 300
    assert rendered["messages"][2]["tool_calls"][0]["id"] == "call-1"  # type: ignore[index]


def test_anthropic_request_normalizes_system_and_tool_blocks() -> None:
    request = canonical_request_from_mapping(
        {
            "model": "claude-test",
            "system": "Be useful.",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu-1",
                            "name": "lookup",
                            "input": {"q": "egg"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu-1",
                            "content": "result",
                        }
                    ],
                },
            ],
            "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
        },
        client_surface="messages",
        protocol="anthropic",
    )
    assert request.messages[0].role == "system"
    assert request.messages[1].content[0].kind == "tool_call"
    assert request.messages[2].content[0].kind == "tool_result"
    rendered = canonical_request_to_mapping(request, surface="messages")
    assert rendered["system"] == "Be useful."
    assert rendered["tools"][0]["input_schema"] == {"type": "object"}  # type: ignore[index]


def test_canonical_events_cover_terminal_and_tool_streaming_without_buffering() -> None:
    events = (
        CanonicalEvent("response_start", response_id="r-1"),
        CanonicalEvent("text_delta", delta="hello"),
        CanonicalEvent("tool_call_start", call_id="call-1", name="lookup"),
        CanonicalEvent("tool_call_arguments_delta", call_id="call-1", delta='{"q"'),
        CanonicalEvent("tool_call_stop", call_id="call-1"),
        CanonicalEvent("usage", usage=CanonicalUsage(prompt_tokens=2)),
        CanonicalEvent("response_complete", finish_reason="tool_calls"),
    )
    assert [event.kind for event in events] == [
        "response_start",
        "text_delta",
        "tool_call_start",
        "tool_call_arguments_delta",
        "tool_call_stop",
        "usage",
        "response_complete",
    ]
    assert events[3].delta == '{"q"'


def test_same_surface_passthrough_is_explicit_and_codec_registry_is_closed() -> None:
    passthrough = PassthroughCodec()
    assert passthrough.should_passthrough(
        client_surface="chat_completions",
        selected_surface="chat_completions",
    )
    assert not passthrough.should_passthrough(
        client_surface="chat_completions",
        selected_surface="messages",
    )

    registry = CodecRegistry()
    registry.register(passthrough)  # type: ignore[arg-type]
    with pytest.raises(WireCodecError, match="Duplicate"):
        registry.register(passthrough)  # type: ignore[arg-type]


def test_transcode_context_captures_canonical_source_once() -> None:
    context = TranscodeContext(
        request_id="r-1",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    first = context.ensure_canonical_request(payload)
    payload["model"] = "changed"
    second = context.ensure_canonical_request(payload)
    assert first is second
    assert first.model == "m"
    assert context.reasoning_intent == first.reasoning


def test_compat_codecs_decode_incremental_chat_and_messages_events() -> None:
    chat = OpenAIChatCodec()
    assert chat.decode_stream_frame(
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}
    ) == (CanonicalEvent("text_delta", delta="hi"),)
    assert chat.decode_stream_frame({"data": "[DONE]"}) == (
        CanonicalEvent("response_complete"),
    )

    messages = AnthropicMessagesCodec()
    events = messages.decode_stream_frame(
        {
            "event": "content_block_delta",
            "data": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"q"'},
            },
        }
    )
    assert events == (
        CanonicalEvent("tool_call_arguments_delta", index=0, delta='{"q"'),
    )
    assert messages.decode_stream_frame({"event": "message_stop"}) == (
        CanonicalEvent("response_complete"),
    )
