"""Tests for OpenAI → Anthropic response translation."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _make_context() -> TranscodeContext:
    return TranscodeContext(
        request_id="req-test",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )


@pytest.fixture
def transcoder() -> OpenAIToAnthropic:
    return OpenAIToAnthropic()


class TestBasicResponseTranslation:
    def test_id_and_model_preserved(self, transcoder: OpenAIToAnthropic) -> None:
        payload = _load_fixture("anthropic_text_response.json")
        result, warnings = transcoder.decode_response(payload, _make_context())

        assert result["id"] == "msg-xyz789"
        assert result["model"] == "claude-3-opus"
        assert result["object"] == "chat.completion"

    def test_content_text_blocks_concatenated(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["choices"][0]["message"]["content"] == "Hello world"


class TestStopReasonMapping:
    @pytest.mark.parametrize(
        "anthropic_reason,expected_finish",
        [
            ("end_turn", "stop"),
            ("max_tokens", "length"),
            ("stop_sequence", "stop"),
            ("tool_use", "tool_calls"),
            ("refusal", "content_filter"),
            ("pause_turn", "tool_calls"),
            ("model_context_window_exceeded", "length"),
        ],
    )
    def test_stop_reason_mapped(
        self,
        transcoder: OpenAIToAnthropic,
        anthropic_reason: str,
        expected_finish: str,
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": anthropic_reason,
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["choices"][0]["finish_reason"] == expected_finish

    def test_unknown_stop_reason_defaults_to_stop(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "unknown_reason",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["choices"][0]["finish_reason"] == "stop"


class TestUsageMapping:
    def test_usage_input_output_tokens(self, transcoder: OpenAIToAnthropic) -> None:
        payload = _load_fixture("anthropic_text_response.json")
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"]["prompt_tokens"] == 25
        assert result["usage"]["completion_tokens"] == 10
        assert result["usage"]["total_tokens"] == 35

    def test_usage_cache_fields(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 8,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 3,
            },
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"]["prompt_tokens"] == 28
        assert result["usage"]["completion_tokens"] == 8
        assert result["usage"]["total_tokens"] == 36
        assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 3
        assert result["usage"]["prompt_tokens_details"]["cache_creation_tokens"] == 5

    def test_usage_cache_read_only(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 8,
                "cache_read_input_tokens": 7,
            },
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"]["prompt_tokens"] == 27
        assert result["usage"]["total_tokens"] == 35
        assert result["usage"]["prompt_tokens_details"]["cached_tokens"] == 7
        assert "cache_creation_tokens" not in result["usage"]["prompt_tokens_details"]

    def test_usage_no_cache_omits_details(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 8},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert "prompt_tokens_details" not in result["usage"]

    def test_usage_missing_defaults_to_zero(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"]["prompt_tokens"] == 0
        assert result["usage"]["completion_tokens"] == 0
        assert result["usage"]["total_tokens"] == 0


class TestResponseStructure:
    def test_response_envelope_structure(self, transcoder: OpenAIToAnthropic) -> None:
        payload = _load_fixture("anthropic_text_response.json")
        result, _ = transcoder.decode_response(payload, _make_context())

        assert "id" in result
        assert result["object"] == "chat.completion"
        assert "created" in result
        assert "model" in result
        assert "choices" in result
        assert "usage" in result
        assert len(result["choices"]) == 1
        assert result["choices"][0]["index"] == 0
        assert result["choices"][0]["message"]["role"] == "assistant"

    def test_tool_use_content_dropped(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "tu_1", "name": "weather", "input": {}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, warnings = transcoder.decode_response(payload, _make_context())

        assert result["choices"][0]["message"]["content"] == "Let me check."
        assert result["choices"][0]["finish_reason"] == "tool_calls"

    def test_thinking_content_dropped(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [
                {"type": "thinking", "thinking": "Let me think..."},
                {"type": "text", "text": "The answer is 42."},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 8},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["choices"][0]["message"]["content"] == "The answer is 42."

    def test_redacted_thinking_content_dropped(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [
                {"type": "redacted_thinking", "data": "encrypted"},
                {"type": "text", "text": "Response."},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["choices"][0]["message"]["content"] == "Response."


class TestLossWarnings:
    def test_stop_sequence_generates_loss_warning(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "stop_sequence",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        _, warnings = transcoder.decode_response(payload, _make_context())

        loss_warnings = [w for w in warnings if w.get("kind") == "lossy_mapping"]
        assert len(loss_warnings) == 1
        assert loss_warnings[0]["field"] == "stop_reason"
        assert loss_warnings[0]["from"] == "stop_sequence"

    def test_pause_turn_generates_loss_warning(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "pause_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        _, warnings = transcoder.decode_response(payload, _make_context())

        loss_warnings = [w for w in warnings if w.get("kind") == "lossy_mapping"]
        assert len(loss_warnings) == 1
        assert loss_warnings[0]["from"] == "pause_turn"

    def test_end_turn_no_loss_warning(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        _, warnings = transcoder.decode_response(payload, _make_context())

        loss_warnings = [w for w in warnings if w.get("kind") == "lossy_mapping"]
        assert len(loss_warnings) == 0


class TestToolResponseTranslation:
    def test_tool_use_block_becomes_tool_call(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "toolu_input_1",
                    "name": "weather",
                    "input": {"city": "SF"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, warnings = transcoder.decode_response(payload, _make_context())

        message = result["choices"][0]["message"]
        assert message["content"] == "Let me check."
        assert message["tool_calls"] == [
            {
                "id": message["tool_calls"][0]["id"],
                "type": "function",
                "function": {
                    "name": "weather",
                    "arguments": '{"city": "SF"}',
                },
            }
        ]
        assert result["choices"][0]["finish_reason"] == "tool_calls"
        id_warnings = [
            w
            for w in warnings
            if w.get("kind") == "tool_call_id_translated"
            and w.get("from") == "toolu_input_1"
        ]
        assert len(id_warnings) == 1
        assert message["tool_calls"][0]["id"].startswith("call_")

    def test_tool_use_block_with_known_id_uses_id_map(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        context = _make_context()
        context.id_map.register("call_known", "toolu_input_1")
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_input_1",
                    "name": "weather",
                    "input": {},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, _ = transcoder.decode_response(payload, context)

        tool_call = result["choices"][0]["message"]["tool_calls"][0]
        assert tool_call["id"].startswith("call_")
        assert tool_call["id"] != "call_known"
        assert tool_call["function"]["name"] == "weather"
        assert tool_call["function"]["arguments"] == "{}"

    def test_multiple_tool_use_blocks(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_a",
                    "name": "get_weather",
                    "input": {"city": "SF"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_b",
                    "name": "get_time",
                    "input": {"timezone": "PST"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        message = result["choices"][0]["message"]
        assert len(message["tool_calls"]) == 2
        assert message["tool_calls"][0]["function"]["name"] == "get_weather"
        assert message["tool_calls"][1]["function"]["name"] == "get_time"

    def test_pause_turn_emits_synthetic_tool_call(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Pausing."}],
            "stop_reason": "pause_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["choices"][0]["finish_reason"] == "tool_calls"
        tool_calls = result["choices"][0]["message"]["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["function"]["name"] == "__eggpool_pause_turn__"
        assert tool_calls[0]["function"]["arguments"] == "{}"
        assert tool_calls[0]["id"] == "call_pause_turn_req-test"

    def test_text_and_tool_use_both_emitted(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [
                {"type": "text", "text": "Let me check."},
                {
                    "type": "tool_use",
                    "id": "toolu_input_1",
                    "name": "weather",
                    "input": {"city": "SF"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        message = result["choices"][0]["message"]
        assert message["content"] == "Let me check."
        assert len(message["tool_calls"]) == 1
        assert message["tool_calls"][0]["function"]["name"] == "weather"

    def test_tool_use_block_with_empty_input(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_input_1",
                    "name": "weather",
                    "input": {},
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        tool_call = result["choices"][0]["message"]["tool_calls"][0]
        assert tool_call["function"]["arguments"] == "{}"

    def test_tool_use_with_finish_reason_tool_use_empty_blocks_warning(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg-1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Just text."}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        _, warnings = transcoder.decode_response(payload, _make_context())

        empty_warnings = [
            w for w in warnings if w.get("kind") == "empty_tool_use_block"
        ]
        assert len(empty_warnings) == 1

    def test_tool_call_anthropic_response_fixture(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = _load_fixture("tool_call_anthropic_response.json")
        result, _ = transcoder.decode_response(payload, _make_context())

        message = result["choices"][0]["message"]
        assert message["content"] == "Let me check."
        assert message["tool_calls"] == [
            {
                "id": message["tool_calls"][0]["id"],
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "San Francisco"}',
                },
            }
        ]
        assert result["choices"][0]["finish_reason"] == "tool_calls"
