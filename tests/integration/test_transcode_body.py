"""Integration tests for the body transcode pipeline.

Since coordinator-level wiring for transcoding may not be complete yet,
these tests exercise the transcoder selection, body translation, and
error re-rendering end-to-end without going through the full HTTP path.
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


@pytest.mark.asyncio
async def test_openai_to_anthropic_non_streaming() -> None:
    """OpenAI client request → Anthropic upstream → response back to OpenAI."""
    ctx = TranscodeContext(
        request_id="integ-test-1",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    transcoder = select_transcoder(
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    assert transcoder is not None
    assert isinstance(transcoder, OpenAIToAnthropic)

    # 1. Encode the client request to upstream format
    openai_request = _load_fixture("openai_text_request.json")
    upstream_payload, encode_warnings = transcoder.encode_request(openai_request, ctx)

    assert upstream_payload["system"] == "You are a helpful assistant."
    assert upstream_payload["messages"][0]["role"] == "user"
    assert upstream_payload["max_tokens"] == 1024
    assert upstream_payload["temperature"] == 0.7

    # 2. Decode the upstream response back to client format
    anthropic_response = _load_fixture("anthropic_text_response.json")
    client_payload, decode_warnings = transcoder.decode_response(
        anthropic_response, ctx
    )

    assert client_payload["id"] == "msg-xyz789"
    assert client_payload["model"] == "claude-3-opus"
    assert client_payload["object"] == "chat.completion"
    assert (
        client_payload["choices"][0]["message"]["content"] == "I'm doing well, thanks!"
    )
    assert client_payload["choices"][0]["finish_reason"] == "stop"
    assert client_payload["usage"]["prompt_tokens"] == 25
    assert client_payload["usage"]["completion_tokens"] == 10
    assert client_payload["usage"]["total_tokens"] == 35


@pytest.mark.asyncio
async def test_anthropic_to_openai_non_streaming() -> None:
    """Anthropic client request → OpenAI upstream → response back to Anthropic."""
    ctx = TranscodeContext(
        request_id="integ-test-2",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    transcoder = select_transcoder(
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    assert transcoder is not None
    assert isinstance(transcoder, AnthropicToOpenAI)

    # 1. Encode the client request to upstream format
    anthropic_request = _load_fixture("anthropic_text_request.json")
    upstream_payload, encode_warnings = transcoder.encode_request(
        anthropic_request, ctx
    )

    assert upstream_payload["messages"][0]["role"] == "system"
    assert upstream_payload["messages"][0]["content"] == "You are a helpful assistant."
    assert upstream_payload["messages"][1]["role"] == "user"
    assert upstream_payload["messages"][1]["content"] == "Hello!"
    assert upstream_payload["max_tokens"] == 1024
    assert upstream_payload["temperature"] == 0.7

    # 2. Decode the upstream response back to client format
    openai_response = _load_fixture("openai_text_response.json")
    client_payload, decode_warnings = transcoder.decode_response(openai_response, ctx)

    assert client_payload["id"] == "chatcmpl-abc123"
    assert client_payload["model"] == "gpt-4"
    assert client_payload["type"] == "message"
    assert client_payload["role"] == "assistant"
    assert client_payload["content"][0]["text"] == "I'm doing well, thanks!"
    assert client_payload["stop_reason"] == "end_turn"
    assert client_payload["usage"]["input_tokens"] == 25
    assert client_payload["usage"]["output_tokens"] == 10


@pytest.mark.asyncio
async def test_openai_to_anthropic_error() -> None:
    """OpenAI client → Anthropic upstream 401 → error re-rendered in OpenAI format."""
    ctx = TranscodeContext(
        request_id="integ-test-3",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    transcoder = select_transcoder(
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    assert transcoder is not None

    anthropic_error = _load_fixture("anthropic_error_response.json")
    status, body, warnings = transcoder.reencode_error(401, anthropic_error, ctx)

    assert status == 401
    assert body["error"]["message"] == "Invalid API key"
    assert body["error"]["type"] == "invalid_api_key"
    assert body["error"]["code"] == "authentication_error"


@pytest.mark.asyncio
async def test_anthropic_to_openai_error() -> None:
    """Anthropic client -> OpenAI upstream 401 -> error re-rendered."""
    ctx = TranscodeContext(
        request_id="integ-test-4",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    transcoder = select_transcoder(
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    assert transcoder is not None

    openai_error = _load_fixture("openai_error_response.json")
    status, body, warnings = transcoder.reencode_error(401, openai_error, ctx)

    assert status == 401
    assert body["type"] == "authentication_error"
    assert body["error"]["message"] == "Invalid API key"


@pytest.mark.asyncio
async def test_native_protocol_returns_none() -> None:
    """Same-protocol pair returns None (no transcoding needed)."""
    assert (
        select_transcoder(client_protocol="openai", upstream_protocol="openai") is None
    )
    assert (
        select_transcoder(client_protocol="anthropic", upstream_protocol="anthropic")
        is None
    )


@pytest.mark.asyncio
async def test_full_round_trip_openai_to_anthropic() -> None:
    """Full round-trip: OpenAI request -> Anthropic upstream -> OpenAI response."""
    ctx = TranscodeContext(
        request_id="roundtrip-1",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )
    transcoder = OpenAIToAnthropic()

    # Client sends OpenAI request
    openai_request = _load_fixture("openai_text_request.json")

    # Transcoder encodes for Anthropic upstream
    upstream_body, encode_warnings = transcoder.encode_request(openai_request, ctx)

    # Verify upstream body is valid Anthropic format
    assert "system" in upstream_body
    assert all(m["role"] in ("user", "assistant") for m in upstream_body["messages"])
    assert "top_p" not in upstream_body  # dropped

    # Simulate Anthropic upstream processing and response
    anthropic_response = _load_fixture("anthropic_text_response.json")

    # Transcoder decodes back to OpenAI format
    client_response, decode_warnings = transcoder.decode_response(
        anthropic_response, ctx
    )

    # Verify client response is valid OpenAI format
    assert client_response["object"] == "chat.completion"
    assert len(client_response["choices"]) == 1
    assert "prompt_tokens" in client_response["usage"]
    assert "completion_tokens" in client_response["usage"]
    assert "total_tokens" in client_response["usage"]


@pytest.mark.asyncio
async def test_full_round_trip_anthropic_to_openai() -> None:
    """Full round-trip: Anthropic request -> OpenAI upstream -> Anthropic response."""
    ctx = TranscodeContext(
        request_id="roundtrip-2",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )
    transcoder = AnthropicToOpenAI()

    # Client sends Anthropic request
    anthropic_request = _load_fixture("anthropic_text_request.json")

    # Transcoder encodes for OpenAI upstream
    upstream_body, encode_warnings = transcoder.encode_request(anthropic_request, ctx)

    # Verify upstream body is valid OpenAI format
    assert upstream_body["messages"][0]["role"] == "system"
    assert all(
        m["role"] in ("system", "user", "assistant") for m in upstream_body["messages"]
    )
    assert "stop_sequences" not in upstream_body  # converted to stop

    # Simulate OpenAI upstream processing and response
    openai_response = _load_fixture("openai_text_response.json")

    # Transcoder decodes back to Anthropic format
    client_response, decode_warnings = transcoder.decode_response(openai_response, ctx)

    # Verify client response is valid Anthropic format
    assert client_response["type"] == "message"
    assert client_response["role"] == "assistant"
    assert isinstance(client_response["content"], list)
    assert "input_tokens" in client_response["usage"]
    assert "output_tokens" in client_response["usage"]
