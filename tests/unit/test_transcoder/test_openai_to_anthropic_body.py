"""Tests for OpenAI → Anthropic body translation."""

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


class TestBasicRequestTranslation:
    def test_system_message_extracted(self, transcoder: OpenAIToAnthropic) -> None:
        payload = _load_fixture("openai_text_request.json")
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["system"] == "You are a helpful assistant."
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "Hello!"
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][1]["content"] == "Hi there!"
        assert result["messages"][2]["role"] == "user"
        assert result["messages"][2]["content"] == "How are you?"

    def test_multiple_system_messages_joined(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "system", "content": "First system message"},
                {"role": "system", "content": "Second system message"},
                {"role": "user", "content": "Hello"},
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["system"] == "First system message\n\nSecond system message"

    def test_developer_message_extracted_as_system(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "developer", "content": "Follow these rules."},
                {"role": "user", "content": "Hello"},
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["system"] == "Follow these rules."
        assert result["messages"] == [{"role": "user", "content": "Hello"}]

    def test_string_content_messages_wrapped(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "user", "content": "Hello!"},
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"] == [{"role": "user", "content": "Hello!"}]

    def test_array_content_text_parts_concatenated(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Part 1"},
                        {"type": "text", "text": "Part 2"},
                    ],
                },
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0]["content"] == "Part 1\nPart 2"

    def test_non_text_content_parts_dropped_with_warning(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Look at this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "http://example.com/img.png"},
                        },
                    ],
                },
            ],
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0]["content"] == "Look at this"
        non_text_warnings = [
            w for w in warnings if w.get("field") == "messages[user].content[non-text]"
        ]
        assert len(non_text_warnings) == 1

    def test_tool_messages_dropped_with_warning(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "tool", "content": "result", "tool_call_id": "tc_123"},
            ],
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert len(result["messages"]) == 1
        assert result["messages"][0]["role"] == "user"
        tool_warnings = [w for w in warnings if w.get("field") == "messages[tool]"]
        assert len(tool_warnings) == 1

    def test_empty_messages_list(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {"model": "gpt-4", "messages": []}
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"] == [{"role": "user", "content": "(empty)"}]

    def test_input_dict_not_mutated(self, transcoder: OpenAIToAnthropic) -> None:
        payload = _load_fixture("openai_text_request.json")
        original = json.loads(json.dumps(payload))
        transcoder.encode_request(payload, _make_context())

        assert payload == original


class TestTemperatureClamping:
    def test_temperature_passthrough_when_le_1(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.7,
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["temperature"] == 0.7
        clamp_warnings = [w for w in warnings if w.get("field") == "temperature"]
        assert len(clamp_warnings) == 0

    def test_temperature_clamped_when_above_1(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 1.5,
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["temperature"] == 1.0
        clamp_warnings = [w for w in warnings if w.get("field") == "temperature"]
        assert len(clamp_warnings) == 1
        assert clamp_warnings[0]["from"] == 1.5
        assert clamp_warnings[0]["to"] == 1.0

    def test_temperature_zero(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0,
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["temperature"] == 0


class TestMaxTokens:
    def test_max_tokens_passthrough(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 2048,
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["max_tokens"] == 2048
        missing_warnings = [w for w in warnings if w.get("field") == "max_tokens"]
        assert len(missing_warnings) == 0

    def test_max_completion_tokens_maps_to_max_tokens(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_completion_tokens": 512,
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["max_tokens"] == 512
        missing_warnings = [w for w in warnings if w.get("field") == "max_tokens"]
        assert len(missing_warnings) == 0

    def test_max_tokens_takes_precedence_over_max_completion_tokens(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 2048,
            "max_completion_tokens": 512,
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["max_tokens"] == 2048

    def test_max_tokens_default_4096_when_absent(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]}
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["max_tokens"] == 4096
        missing_warnings = [w for w in warnings if w.get("field") == "max_tokens"]
        assert len(missing_warnings) == 1
        assert missing_warnings[0]["default"] == 4096


class TestStopSequences:
    def test_stop_string_to_stop_sequences_list(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stop": "END",
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["stop_sequences"] == ["END"]

    def test_stop_list_to_stop_sequences_list(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stop": ["END", "STOP"],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["stop_sequences"] == ["END", "STOP"]


class TestDroppedFields:
    def test_n_dropped_with_warning(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "n": 3,
        }
        _, warnings = transcoder.encode_request(payload, _make_context())

        n_warnings = [w for w in warnings if w.get("field") == "n"]
        assert len(n_warnings) == 1
        assert n_warnings[0]["reason"] == "anthropic_unsupported"

    def test_dropped_fields_from_payload(self, transcoder: OpenAIToAnthropic) -> None:
        dropped_fields = [
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "logprobs",
            "top_logprobs",
            "response_format",
            "seed",
            "user",
            "tools",
            "tool_choice",
            "functions",
            "function_call",
            "parallel_tool_calls",
            "stream_options",
            "logit_bias",
        ]
        payload: dict[str, Any] = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        for field in dropped_fields:
            payload[field] = "value"  # type: ignore[assignment]

        result, warnings = transcoder.encode_request(payload, _make_context())

        warned_fields = {
            w.get("field") for w in warnings if w.get("kind") == "dropped_field"
        }
        for field in dropped_fields:
            assert field in warned_fields, f"Expected warning for field '{field}'"

        for field in dropped_fields:
            assert field not in result, f"Expected field '{field}' to be dropped"

    def test_stream_not_in_output(self, transcoder: OpenAIToAnthropic) -> None:
        payload = _load_fixture("openai_text_request.json")
        result, _ = transcoder.encode_request(payload, _make_context())

        assert "stream" not in result
