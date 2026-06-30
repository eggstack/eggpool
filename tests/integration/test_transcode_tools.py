"""Integration tests for tool-use transcoding (non-streaming round-trips).

Exercises the full encode_request → decode_response pipeline for both
directions, verifying that tool schemas, tool_choice, multi-turn tool
history, and tool-call IDs survive the round trip.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.protocol import select_transcoder

FIXTURES = (
    pathlib.Path(__file__).parent.parent / "unit" / "test_transcoder" / "fixtures"
)


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


# ── OpenAI client → Anthropic upstream ────────────────────────────────


@pytest.mark.asyncio
async def test_openai_tools_translated_to_anthropic_shape() -> None:
    """OpenAI tools/tool_choice reach Anthropic in the correct shape."""
    ctx = TranscodeContext(
        request_id="integ-tools-1",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    transcoder = select_transcoder(
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    assert transcoder is not None
    assert isinstance(transcoder, OpenAIToAnthropic)

    openai_request = _load_fixture("tool_call_openai_request.json")
    upstream_payload, warnings = transcoder.encode_request(openai_request, ctx)

    # Tools translated to Anthropic shape
    assert "tools" in upstream_payload
    assert len(upstream_payload["tools"]) == 2
    tool_names = {t["name"] for t in upstream_payload["tools"]}
    assert tool_names == {"get_weather", "get_time"}
    for tool in upstream_payload["tools"]:
        assert "input_schema" in tool
        assert "name" in tool

    # tool_choice translated
    assert upstream_payload["tool_choice"] == {"type": "tool", "name": "get_weather"}

    # parallel_tool_calls omitted (Anthropic allows parallel by default)
    assert "parallel_tool_calls" not in upstream_payload

    # No tool-related loss warnings
    tool_warnings = [w for w in warnings if w.get("field") in ("tools", "tool_choice")]
    assert tool_warnings == []


@pytest.mark.asyncio
async def test_anthropic_tool_use_response_decoded_to_tool_calls() -> None:
    """Anthropic tool_use blocks become OpenAI tool_calls on the response."""
    ctx = TranscodeContext(
        request_id="integ-tools-2",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    transcoder = OpenAIToAnthropic()

    anthropic_response = _load_fixture("tool_call_anthropic_response.json")
    client_response, warnings = transcoder.decode_response(anthropic_response, ctx)

    assert client_response["object"] == "chat.completion"
    assert client_response["choices"][0]["finish_reason"] == "tool_calls"

    tool_calls = client_response["choices"][0]["message"]["tool_calls"]
    assert len(tool_calls) == 1
    tc = tool_calls[0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert json.loads(tc["function"]["arguments"]) == {"city": "San Francisco"}
    # ID is call_ shaped (translated from toolu_)
    assert tc["id"].startswith("call_")


@pytest.mark.asyncio
async def test_full_round_trip_openai_tools() -> None:
    """Full round-trip: OpenAI request → Anthropic upstream → OpenAI response."""
    ctx = TranscodeContext(
        request_id="integ-tools-3",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    transcoder = OpenAIToAnthropic()

    # 1. Encode
    openai_request = _load_fixture("tool_call_openai_request.json")
    upstream_body, encode_warnings = transcoder.encode_request(openai_request, ctx)

    # Verify upstream body is valid Anthropic format
    assert "tools" in upstream_body
    assert all(t["name"] for t in upstream_body["tools"])

    # 2. Decode response
    anthropic_response = _load_fixture("tool_call_anthropic_response.json")
    client_response, decode_warnings = transcoder.decode_response(
        anthropic_response, ctx
    )

    # Verify client response is valid OpenAI format
    assert client_response["object"] == "chat.completion"
    assert client_response["choices"][0]["finish_reason"] == "tool_calls"
    assert "tool_calls" in client_response["choices"][0]["message"]


# ── Anthropic client → OpenAI upstream ────────────────────────────────


@pytest.mark.asyncio
async def test_anthropic_tools_translated_to_openai_shape() -> None:
    """Anthropic tools/tool_choice reach OpenAI in the correct shape."""
    ctx = TranscodeContext(
        request_id="integ-tools-4",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    transcoder = select_transcoder(
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    assert transcoder is not None
    assert isinstance(transcoder, AnthropicToOpenAI)

    anthropic_request = _load_fixture("tool_call_anthropic_request.json")
    upstream_payload, warnings = transcoder.encode_request(anthropic_request, ctx)

    # Tools translated to OpenAI function shape
    assert "tools" in upstream_payload
    assert len(upstream_payload["tools"]) == 1
    tool = upstream_payload["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "get_weather"
    assert "parameters" in tool["function"]

    # tool_choice translated
    assert upstream_payload["tool_choice"] == {
        "type": "function",
        "function": {"name": "get_weather"},
    }


@pytest.mark.asyncio
async def test_openai_tool_calls_decoded_to_tool_use() -> None:
    """OpenAI tool_calls become Anthropic tool_use blocks on the response."""
    ctx = TranscodeContext(
        request_id="integ-tools-5",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    transcoder = AnthropicToOpenAI()

    openai_response = _load_fixture("tool_call_openai_response.json")
    client_response, warnings = transcoder.decode_response(openai_response, ctx)

    assert client_response["type"] == "message"
    assert client_response["stop_reason"] == "tool_use"

    tool_use_blocks = [b for b in client_response["content"] if b["type"] == "tool_use"]
    assert len(tool_use_blocks) == 1
    block = tool_use_blocks[0]
    assert block["name"] == "get_weather"
    assert block["input"] == {"city": "San Francisco"}
    # ID is toolu_ shaped (translated from call_)
    assert block["id"].startswith("toolu_")


@pytest.mark.asyncio
async def test_full_round_trip_anthropic_tools() -> None:
    """Full round-trip: Anthropic request → OpenAI upstream → Anthropic response."""
    ctx = TranscodeContext(
        request_id="integ-tools-6",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    transcoder = AnthropicToOpenAI()

    # 1. Encode
    anthropic_request = _load_fixture("tool_call_anthropic_request.json")
    upstream_body, encode_warnings = transcoder.encode_request(anthropic_request, ctx)

    # Verify upstream body is valid OpenAI format
    assert "tools" in upstream_body
    assert upstream_body["tools"][0]["type"] == "function"

    # 2. Decode response
    openai_response = _load_fixture("tool_call_openai_response.json")
    client_response, decode_warnings = transcoder.decode_response(openai_response, ctx)

    # Verify client response is valid Anthropic format
    assert client_response["type"] == "message"
    assert client_response["stop_reason"] == "tool_use"
    assert isinstance(client_response["content"], list)


# ── Multi-turn tool history ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_openai_tool_history_round_trip() -> None:
    """OpenAI assistant tool_calls + role=tool messages translate correctly."""
    ctx = TranscodeContext(
        request_id="integ-tools-7",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    transcoder = OpenAIToAnthropic()

    history = _load_fixture("tool_call_history_messages.json")
    openai_request: dict[str, Any] = {
        "model": "gpt-4",
        "messages": history,
    }
    upstream_payload, warnings = transcoder.encode_request(openai_request, ctx)

    messages = upstream_payload["messages"]
    assert (
        len(messages) == 4
    )  # user → assistant(tool_use) → user(tool_result) → assistant(text)

    # Assistant message with tool_use block
    assistant_msg = messages[1]
    assert assistant_msg["role"] == "assistant"
    tool_use_blocks = [b for b in assistant_msg["content"] if b["type"] == "tool_use"]
    assert len(tool_use_blocks) == 1
    assert tool_use_blocks[0]["name"] == "get_weather"
    assert tool_use_blocks[0]["id"].startswith("toolu_")

    # User message with tool_result block
    tool_result_msg = messages[2]
    assert tool_result_msg["role"] == "user"
    result_blocks = [
        b for b in tool_result_msg["content"] if b["type"] == "tool_result"
    ]
    assert len(result_blocks) == 1
    assert result_blocks[0]["tool_use_id"] == tool_use_blocks[0]["id"]


@pytest.mark.asyncio
async def test_anthropic_tool_history_round_trip() -> None:
    """Anthropic tool_use/tool_result history translates to OpenAI format."""
    ctx = TranscodeContext(
        request_id="integ-tools-8",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    transcoder = AnthropicToOpenAI()

    anthropic_request: dict[str, Any] = {
        "model": "claude-3-opus",
        "messages": [
            {"role": "user", "content": "Weather in SF?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {
                        "type": "tool_use",
                        "id": "toolu_abc123",
                        "name": "get_weather",
                        "input": {"city": "San Francisco"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_abc123",
                        "content": '{"temperature": 68}',
                    }
                ],
            },
        ],
    }
    upstream_payload, warnings = transcoder.encode_request(anthropic_request, ctx)

    messages = upstream_payload["messages"]
    assert len(messages) == 3  # user → assistant(tool_calls) → tool

    # Assistant message with tool_calls
    assistant_msg = messages[1]
    assert assistant_msg["role"] == "assistant"
    assert "tool_calls" in assistant_msg
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "get_weather"
    assert assistant_msg["tool_calls"][0]["id"].startswith("call_")

    # Tool result message
    tool_msg = messages[2]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == assistant_msg["tool_calls"][0]["id"]
    assert "temperature" in tool_msg["content"]


# ── Preflight token padding ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_token_padding_computed() -> None:
    """TranscodePreflightResult carries tool_token_padding when tools present."""
    from eggpool.api.proxy_request import _tool_token_padding

    # Payload with tools → positive padding
    payload_with_tools: dict[str, Any] = {
        "tools": [
            {
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            }
        ]
    }
    padding = _tool_token_padding(payload_with_tools)
    assert padding >= 64

    # Payload without tools → zero padding
    payload_no_tools: dict[str, Any] = {"messages": []}
    assert _tool_token_padding(payload_no_tools) == 0

    # Payload with empty tools → zero padding
    payload_empty_tools: dict[str, Any] = {"tools": []}
    assert _tool_token_padding(payload_empty_tools) == 0
