"""Tests for Anthropic → OpenAI body translation."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _make_context() -> TranscodeContext:
    return TranscodeContext(
        request_id="req-test",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )


@pytest.fixture
def transcoder() -> AnthropicToOpenAI:
    return AnthropicToOpenAI()


class TestBasicRequestTranslation:
    def test_system_string_prepended_as_system_message(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = _load_fixture("anthropic_text_request.json")
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "You are a helpful assistant."
        assert result["messages"][1]["role"] == "user"
        assert result["messages"][1]["content"] == "Hello!"
        assert result["messages"][2]["role"] == "assistant"
        assert result["messages"][2]["content"] == "Hi there!"
        assert result["messages"][3]["role"] == "user"
        assert result["messages"][3]["content"] == "How are you?"

    def test_system_as_list_of_text_blocks_joined(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3-opus",
            "system": [
                {"type": "text", "text": "Part one"},
                {"type": "text", "text": "Part two"},
            ],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "Part one\n\nPart two"

    def test_user_assistant_messages_string_content(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there"},
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0] == {"role": "user", "content": "Hello"}
        assert result["messages"][1] == {"role": "assistant", "content": "Hi there"}

    def test_content_blocks_text_concatenated(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Part A"},
                        {"type": "text", "text": "Part B"},
                    ],
                },
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0]["content"] == "Part A\nPart B"

    def test_non_text_content_blocks_dropped_with_warning(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Text only"},
                        {"type": "tool_use", "id": "tu_1", "name": "weather"},
                    ],
                },
            ],
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0]["content"] == "Text only"
        non_text_warnings = [w for w in warnings if "non-text" in w.get("field", "")]
        assert len(non_text_warnings) == 1

    def test_empty_messages_prepends_empty_user(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {"model": "claude-3", "messages": []}
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["messages"] == [{"role": "user", "content": ""}]
        inserted_warnings = [w for w in warnings if w.get("kind") == "inserted_field"]
        assert len(inserted_warnings) == 1

    def test_input_dict_not_mutated(self, transcoder: AnthropicToOpenAI) -> None:
        payload = _load_fixture("anthropic_text_request.json")
        original = json.loads(json.dumps(payload))
        transcoder.encode_request(payload, _make_context())

        assert payload == original


class TestParameterTranslation:
    def test_max_tokens_passthrough(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 2048,
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["max_tokens"] == 2048

    def test_temperature_passthrough(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.5,
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["temperature"] == 0.5

    def test_top_k_dropped_with_warning(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "top_k": 10,
        }
        _, warnings = transcoder.encode_request(payload, _make_context())

        tk_warnings = [w for w in warnings if w.get("field") == "top_k"]
        assert len(tk_warnings) == 1
        assert tk_warnings[0]["reason"] == "openai_unsupported"

    def test_stop_sequences_single_to_stop_string(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "stop_sequences": ["END"],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["stop"] == "END"

    def test_stop_sequences_list_to_stop_list(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "stop_sequences": ["END", "STOP"],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["stop"] == ["END", "STOP"]

    def test_metadata_user_id_to_user(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "metadata": {"user_id": "user-123"},
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["user"] == "user-123"


class TestDroppedFields:
    def test_thinking_dropped_with_warning(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        }
        _, warnings = transcoder.encode_request(payload, _make_context())

        thinking_warnings = [w for w in warnings if w.get("field") == "thinking"]
        assert len(thinking_warnings) == 1
        assert thinking_warnings[0]["reason"] == "openai_unsupported"

    def test_tools_dropped_with_warning(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [{"name": "weather", "input_schema": {}}],
        }
        _, warnings = transcoder.encode_request(payload, _make_context())

        tool_warnings = [w for w in warnings if w.get("field") == "tools"]
        assert len(tool_warnings) == 1

    def test_tool_choice_dropped_with_warning(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "auto"},
        }
        _, warnings = transcoder.encode_request(payload, _make_context())

        tc_warnings = [w for w in warnings if w.get("field") == "tool_choice"]
        assert len(tc_warnings) == 1
