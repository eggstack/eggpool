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

        assert result["usage"]["prompt_tokens"] == 20
        assert result["usage"]["completion_tokens"] == 8
        assert result["usage"]["total_tokens"] == 28

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
