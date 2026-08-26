"""Fixture-driven streaming transcoder tests.

Each test class loads a representative SSE fixture, drives the
appropriate streaming transcoder byte-by-byte (or event-by-event),
and asserts the output event sequence matches the expected shape.

Tests use the ``parse_sse_events`` / ``assert_event_sequence_equal``
helpers so they compare decoded SSE events rather than raw bytes.
This makes them resilient to harmless JSON whitespace changes (the
transcoders use compact separators ``(",", ":")``).
"""

from __future__ import annotations

from typing import Any

import pytest

from eggpool.transcoder.streaming import (
    AnthropicToOpenAIStreaming,
    OpenAIToAnthropicStreaming,
    select_streaming_transcoder,
)
from tests.helpers.streaming_fixtures import (
    assert_event_sequence_equal,
    fixture_to_sse_bytes,
    load_streaming_fixture,
    parse_sse_events,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_transcoder_bytes(
    transcoder: AnthropicToOpenAIStreaming | OpenAIToAnthropicStreaming,
    chunks: list[bytes],
) -> bytes:
    """Feed *chunks* to *transcoder* and flush, returning raw SSE bytes."""
    out: list[bytes] = []
    for chunk in chunks:
        out.extend(transcoder.feed(chunk))
    out.extend(transcoder.flush())
    return b"".join(out)


def _expected_openai_text_events() -> list[dict[str, Any]]:
    """Expected Anthropic event sequence for the OpenAI text streaming fixture."""
    return [
        {
            "event": "message_start",
            "data": {
                "type": "message_start",
                "message": {
                    "id": "chatcmpl-gpt4-001",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4",
                    "stop_reason": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        },
        {
            "event": "content_block_start",
            "data": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        },
        {
            "event": "content_block_delta",
            "data": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        },
        {
            "event": "content_block_delta",
            "data": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": " there"},
            },
        },
        {
            "event": "content_block_delta",
            "data": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "!"},
            },
        },
        {
            "event": "content_block_stop",
            "data": {"type": "content_block_stop", "index": 0},
        },
        {
            "event": "message_delta",
            "data": {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
        },
        {
            "event": "message_stop",
            "data": {"type": "message_stop"},
        },
    ]


def _expected_anthropic_text_events() -> list[dict[str, Any]]:
    """Expected OpenAI event sequence for the text streaming fixture.

    Note: ``content_block_stop`` is NOT emitted for text blocks by the
    transcoder (only for tool_use blocks via ``_flush_pending_tool_blocks``).

    All events use empty event type (OpenAI format: ``data: <json>\\n\\n``).
    """
    return [
        {
            "event": "",
            "data": {
                "id": "msg-haiku-text-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            },
        },
        {
            "event": "",
            "data": {
                "id": "msg-haiku-text-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hello"},
                        "finish_reason": None,
                    }
                ],
            },
        },
        {
            "event": "",
            "data": {
                "id": "msg-haiku-text-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " world"},
                        "finish_reason": None,
                    }
                ],
            },
        },
        {
            "event": "",
            "data": {
                "id": "msg-haiku-text-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "! I am"},
                        "finish_reason": None,
                    }
                ],
            },
        },
        {
            "event": "",
            "data": {
                "id": "msg-haiku-text-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " Claude."},
                        "finish_reason": None,
                    }
                ],
            },
        },
        {
            "event": "",
            "data": {
                "id": "msg-haiku-text-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            },
        },
        {
            "event": "",
            "data": "[DONE]",
        },
    ]


def _expected_anthropic_tool_use_events() -> list[dict[str, Any]]:
    """Expected OpenAI event sequence for the tool-use fixture.

    Tool calls are emitted as ``delta.tool_calls`` entries after the
    upstream signals ``finish_reason: "tool_calls"``.
    """
    return [
        # message_start
        {
            "event": "",
            "data": {
                "id": "msg-sonnet-tool-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-sonnet-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            },
        },
        # content_block_start (tool_use) → tool_calls start
        {
            "event": "",
            "data": {
                "id": "msg-sonnet-tool-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-sonnet-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc123",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": "",
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
        },
        # input_json_delta 1
        {
            "event": "",
            "data": {
                "id": "msg-sonnet-tool-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-sonnet-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"location":'},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
        },
        # input_json_delta 2
        {
            "event": "",
            "data": {
                "id": "msg-sonnet-tool-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-sonnet-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": ' "San Fran'},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
        },
        # input_json_delta 3
        {
            "event": "",
            "data": {
                "id": "msg-sonnet-tool-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-sonnet-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": 'cisco"}'},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
        },
        # message_delta with stop_reason=tool_use → finish
        {
            "event": "",
            "data": {
                "id": "msg-sonnet-tool-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-sonnet-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        },
        {
            "event": "",
            "data": "[DONE]",
        },
    ]


def _expected_anthropic_pause_turn_events() -> list[dict[str, Any]]:
    """Expected OpenAI events for the pause_turn fixture."""
    return [
        {
            "event": "",
            "data": {
                "id": "msg-pause-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            },
        },
        {
            "event": "",
            "data": {
                "id": "msg-pause-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "I need to pause"},
                        "finish_reason": None,
                    }
                ],
            },
        },
        # Synthetic __eggpool_pause_turn__ sentinel (tool_call start).
        # No ``role`` in the delta: the initial chunk already carried it.
        {
            "event": "",
            "data": {
                "id": "msg-pause-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_pause_turn",
                                    "type": "function",
                                    "function": {
                                        "name": "__eggpool_pause_turn__",
                                        "arguments": "",
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
        },
        # Synthetic tool_call arguments
        {
            "event": "",
            "data": {
                "id": "msg-pause-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
        },
        # Finish with tool_calls
        {
            "event": "",
            "data": {
                "id": "msg-pause-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-haiku-20240307",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        },
        {
            "event": "",
            "data": "[DONE]",
        },
    ]


def _expected_openai_tool_call_events() -> list[dict[str, Any]]:
    """Expected Anthropic event sequence for the OpenAI tool-call fixture."""
    return [
        {
            "event": "message_start",
            "data": {
                "type": "message_start",
                "message": {
                    "id": "chatcmpl-gpt4-tool-001",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "gpt-4",
                    "stop_reason": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        },
        # tool_use block emitted when finish_reason=tool_calls arrives
        {
            "event": "content_block_start",
            "data": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_weather001",
                    "name": "get_weather",
                    "input": {"location": "NYC"},
                },
            },
        },
        {
            "event": "content_block_stop",
            "data": {"type": "content_block_stop", "index": 0},
        },
        {
            "event": "message_delta",
            "data": {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
            },
        },
        {
            "event": "message_stop",
            "data": {"type": "message_stop"},
        },
    ]


def _expected_anthropic_thinking_events() -> list[dict[str, Any]]:
    """Expected OpenAI events for the thinking fixture.

    By default ``features=None`` which does NOT suppress thinking
    deltas (the guard is ``self._features is not None and not
    self._features.thinking``).  Thinking deltas are emitted as
    ``reasoning`` fields.
    """
    return [
        {
            "event": "",
            "data": {
                "id": "msg-opus-think-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-opus-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            },
        },
        # Thinking delta 1
        {
            "event": "",
            "data": {
                "id": "msg-opus-think-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-opus-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning": "Let me analyze"},
                        "finish_reason": None,
                    }
                ],
            },
        },
        # Thinking delta 2
        {
            "event": "",
            "data": {
                "id": "msg-opus-think-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-opus-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning": " this step by step."},
                        "finish_reason": None,
                    }
                ],
            },
        },
        # Thinking delta 3
        {
            "event": "",
            "data": {
                "id": "msg-opus-think-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-opus-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning": " The answer is 42."},
                        "finish_reason": None,
                    }
                ],
            },
        },
        # Text delta 1
        {
            "event": "",
            "data": {
                "id": "msg-opus-think-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-opus-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "The answer"},
                        "finish_reason": None,
                    }
                ],
            },
        },
        # Text delta 2
        {
            "event": "",
            "data": {
                "id": "msg-opus-think-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-opus-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": " is 42."},
                        "finish_reason": None,
                    }
                ],
            },
        },
        # Finish
        {
            "event": "",
            "data": {
                "id": "msg-opus-think-001",
                "object": "chat.completion.chunk",
                "model": "claude-3-opus-20240229",
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
            },
        },
        {
            "event": "",
            "data": "[DONE]",
        },
    ]


def _expected_anthropic_thinking_with_thinking_enabled_events() -> list[dict[str, Any]]:
    """Expected OpenAI events for the thinking fixture with thinking enabled.

    Same as the default (features=None) because the guard only
    suppresses when features is not None and thinking is False.
    """
    return _expected_anthropic_thinking_events()


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestSelectStreamingTranscoder:
    """Unit tests for the select_streaming_transcoder factory."""

    def test_same_protocol_returns_none(self) -> None:
        result = select_streaming_transcoder(
            client_protocol="openai",
            upstream_protocol="openai",
        )
        assert result is None

    def test_anthropic_to_openai(self) -> None:
        result = select_streaming_transcoder(
            client_protocol="openai",
            upstream_protocol="anthropic",
        )
        assert isinstance(result, AnthropicToOpenAIStreaming)

    def test_openai_to_anthropic(self) -> None:
        result = select_streaming_transcoder(
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        assert isinstance(result, OpenAIToAnthropicStreaming)

    def test_unknown_pair_returns_none(self) -> None:
        result = select_streaming_transcoder(
            client_protocol="unknown",
            upstream_protocol="anthropic",
        )
        assert result is None


class TestAnthropicTextStreaming:
    """Load anthropic_text_streaming fixture, drive AnthropicToOpenAIStreaming."""

    def test_transcoded_event_sequence(self) -> None:
        fixture = load_streaming_fixture("anthropic_text_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = AnthropicToOpenAIStreaming()
        raw = _run_transcoder_bytes(transcoder, chunks)
        actual = parse_sse_events(raw)
        expected = _expected_anthropic_text_events()
        assert_event_sequence_equal(actual, expected, protocol="openai")

    def test_byte_by_byte_chunking(self) -> None:
        """Feed one SSE event per chunk to exercise incremental parsing."""
        fixture = load_streaming_fixture("anthropic_text_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = AnthropicToOpenAIStreaming()
        # Each chunk is one event; feed individually
        all_out: list[bytes] = []
        for chunk in chunks:
            all_out.extend(transcoder.feed(chunk))
        all_out.extend(transcoder.flush())
        raw = b"".join(all_out)
        actual = parse_sse_events(raw)
        expected = _expected_anthropic_text_events()
        assert_event_sequence_equal(actual, expected, protocol="openai")


class TestAnthropicThinkingStreaming:
    """Load anthropic_thinking_streaming fixture, drive AnthropicToOpenAIStreaming."""

    def test_thinking_dropped_by_default(self) -> None:
        fixture = load_streaming_fixture("anthropic_thinking_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = AnthropicToOpenAIStreaming(
            features=None,
        )
        raw = _run_transcoder_bytes(transcoder, chunks)
        actual = parse_sse_events(raw)
        expected = _expected_anthropic_thinking_events()
        assert_event_sequence_equal(actual, expected, protocol="openai")

    def test_thinking_emitted_when_enabled(self) -> None:
        from eggpool.transcoder.policy import TranscoderFeatures

        fixture = load_streaming_fixture("anthropic_thinking_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = AnthropicToOpenAIStreaming(
            features=TranscoderFeatures(thinking=True),
        )
        raw = _run_transcoder_bytes(transcoder, chunks)
        actual = parse_sse_events(raw)
        expected = _expected_anthropic_thinking_with_thinking_enabled_events()
        assert_event_sequence_equal(actual, expected, protocol="openai")


class TestAnthropicToolUseStreaming:
    """Load anthropic_tool_use_streaming fixture, drive AnthropicToOpenAIStreaming."""

    def test_tool_call_delta_events(self) -> None:
        fixture = load_streaming_fixture("anthropic_tool_use_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = AnthropicToOpenAIStreaming()
        raw = _run_transcoder_bytes(transcoder, chunks)
        actual = parse_sse_events(raw)
        expected = _expected_anthropic_tool_use_events()
        assert_event_sequence_equal(actual, expected, protocol="openai")


class TestAnthropicPauseTurn:
    """Load anthropic_pause_turn fixture, drive AnthropicToOpenAIStreaming."""

    def test_pause_turn_sentinel_emitted(self) -> None:
        fixture = load_streaming_fixture("anthropic_pause_turn")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = AnthropicToOpenAIStreaming()
        raw = _run_transcoder_bytes(transcoder, chunks)
        actual = parse_sse_events(raw)
        expected = _expected_anthropic_pause_turn_events()
        assert_event_sequence_equal(actual, expected, protocol="openai")

    def test_sentinel_function_name(self) -> None:
        """The sentinel tool_call must use the correct function name."""
        fixture = load_streaming_fixture("anthropic_pause_turn")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = AnthropicToOpenAIStreaming()
        raw = _run_transcoder_bytes(transcoder, chunks)
        actual = parse_sse_events(raw)
        # Find the sentinel frame
        sentinel_found = False
        for frame in actual:
            data = frame.get("data")
            if not isinstance(data, dict):
                continue
            choices = data.get("choices", [])
            for choice in choices:
                delta = choice.get("delta", {})
                tool_calls = delta.get("tool_calls", [])
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    if fn.get("name") == "__eggpool_pause_turn__":
                        sentinel_found = True
        assert sentinel_found, (
            "Expected __eggpool_pause_turn__ sentinel not found in output"
        )


class TestOpenAITextStreaming:
    """Load openai_text_streaming fixture, drive OpenAIToAnthropicStreaming."""

    def test_transcoded_event_sequence(self) -> None:
        fixture = load_streaming_fixture("openai_text_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = OpenAIToAnthropicStreaming()
        raw = _run_transcoder_bytes(transcoder, chunks)
        actual = parse_sse_events(raw)
        expected = _expected_openai_text_events()
        assert_event_sequence_equal(actual, expected, protocol="openai")


class TestOpenAIToolCallStreaming:
    """Load openai_tool_call_streaming fixture, drive OpenAIToAnthropicStreaming."""

    def test_tool_use_content_blocks(self) -> None:
        fixture = load_streaming_fixture("openai_tool_call_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = OpenAIToAnthropicStreaming()
        raw = _run_transcoder_bytes(transcoder, chunks)
        actual = parse_sse_events(raw)
        expected = _expected_openai_tool_call_events()
        assert_event_sequence_equal(actual, expected, protocol="anthropic")

    def test_tool_use_input_parsed(self) -> None:
        """The tool_use content_block must carry parsed input, not raw JSON."""
        fixture = load_streaming_fixture("openai_tool_call_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        transcoder = OpenAIToAnthropicStreaming()
        raw = _run_transcoder_bytes(transcoder, chunks)
        actual = parse_sse_events(raw)
        # Find the content_block_start with tool_use
        for frame in actual:
            data = frame.get("data")
            if not isinstance(data, dict):
                continue
            if data.get("type") != "content_block_start":
                continue
            cb = data.get("content_block", {})
            if cb.get("type") == "tool_use":
                assert cb["name"] == "get_weather"
                assert cb["id"] == "toolu_weather001"
                assert cb["input"] == {"location": "NYC"}
                return
        pytest.fail("No tool_use content_block_start found in output")


class TestStreamingTranscoderSyncProtocol:
    """Verify feed/flush are synchronous (not coroutine)."""

    def test_feed_is_sync(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        # feed() must not return a coroutine
        import inspect

        assert not inspect.iscoroutinefunction(transcoder.feed)

    def test_flush_is_sync(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        import inspect

        assert not inspect.iscoroutinefunction(transcoder.flush)

    def test_openai_to_anthropic_feed_is_sync(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        import inspect

        assert not inspect.iscoroutinefunction(transcoder.feed)

    def test_openai_to_anthropic_flush_is_sync(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        import inspect

        assert not inspect.iscoroutinefunction(transcoder.flush)


class TestStreamingTranscoderUsageDefault:
    """The transcoder usage property returns a default result."""

    def test_anthropic_to_openai_usage(self) -> None:
        transcoder = AnthropicToOpenAIStreaming()
        usage = transcoder.usage
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.is_complete is False

    def test_openai_to_anthropic_usage(self) -> None:
        transcoder = OpenAIToAnthropicStreaming()
        usage = transcoder.usage
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.is_complete is False


class TestFixtureLoader:
    """Verify the fixture loader and SSE conversion helpers."""

    def test_load_all_fixtures(self) -> None:
        names = [
            "anthropic_text_streaming",
            "anthropic_thinking_streaming",
            "anthropic_tool_use_streaming",
            "anthropic_pause_turn",
            "openai_text_streaming",
            "openai_tool_call_streaming",
        ]
        for name in names:
            fixture = load_streaming_fixture(name)
            assert "events" in fixture
            assert len(fixture["events"]) > 0
            assert "upstream_protocol" in fixture

    def test_fixture_to_sse_bytes_roundtrip(self) -> None:
        fixture = load_streaming_fixture("anthropic_text_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        assert len(chunks) == len(fixture["events"])
        # Each chunk should be valid bytes
        for chunk in chunks:
            assert isinstance(chunk, bytes)
            assert len(chunk) > 0

    def test_parse_sse_events_roundtrip(self) -> None:
        fixture = load_streaming_fixture("openai_text_streaming")
        chunks = fixture_to_sse_bytes(
            fixture["events"],
            protocol=fixture["upstream_protocol"],
        )
        raw = b"".join(chunks)
        parsed = parse_sse_events(raw)
        # Should parse back to the same number of events
        assert len(parsed) == len(fixture["events"])
