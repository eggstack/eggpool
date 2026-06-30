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

    def test_tool_use_block_translates_to_tool_call(
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

        assert result["messages"][0]["role"] == "assistant"
        assert result["messages"][0]["content"] == "Text only"
        assert result["messages"][0]["tool_calls"] == [
            {
                "id": result["messages"][0]["tool_calls"][0]["id"],
                "type": "function",
                "function": {"name": "weather", "arguments": "{}"},
            }
        ]
        non_text_warnings = [w for w in warnings if "non-text" in w.get("field", "")]
        assert non_text_warnings == []
        id_warnings = [
            w
            for w in warnings
            if w.get("kind") == "tool_call_id_translated" and w.get("from") == "tu_1"
        ]
        assert len(id_warnings) == 1

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
        assert tk_warnings[0]["kind"] == "top_k_dropped"

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

    def test_tools_translated_to_function_shape(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [{"name": "weather", "input_schema": {}}],
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert "tools" in result
        assert result["tools"] == [
            {
                "type": "function",
                "function": {"name": "weather", "parameters": {}},
            }
        ]
        tool_warnings = [w for w in warnings if w.get("field") == "tools"]
        assert tool_warnings == []

    def test_tool_choice_translated_to_openai_shape(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "auto"},
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result.get("tool_choice") == "auto"
        tc_warnings = [w for w in warnings if w.get("field") == "tool_choice"]
        assert tc_warnings == []


class TestToolTranslation:
    def test_tools_array_translates_to_function_shape(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get the weather.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                }
            ],
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the weather.",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]
        tool_warnings = [w for w in warnings if w.get("field") == "tools"]
        assert tool_warnings == []

    def test_tools_cache_control_dropped_with_warning(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "input_schema": {},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        _, warnings = transcoder.encode_request(payload, _make_context())

        cc_warnings = [w for w in warnings if w.get("kind") == "cache_control_dropped"]
        assert len(cc_warnings) == 1

    def test_tool_choice_auto_translates(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "auto"},
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["tool_choice"] == "auto"

    def test_tool_choice_none_translates(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "none"},
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["tool_choice"] == "none"

    def test_tool_choice_any_translates_to_required(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "any"},
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["tool_choice"] == "required"

    def test_tool_choice_tool_translates_to_function(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "tool", "name": "foo"},
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["tool_choice"] == {
            "type": "function",
            "function": {"name": "foo"},
        }

    def test_tool_choice_tool_with_empty_name_dropped(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "tool", "name": ""},
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert "tool_choice" not in result
        invalid_warnings = [
            w for w in warnings if w.get("kind") == "invalid_tool_choice"
        ]
        assert len(invalid_warnings) == 1

    def test_assistant_message_with_tool_use_blocks(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Checking weather..."},
                        {
                            "type": "tool_use",
                            "id": "toolu_input_1",
                            "name": "get_weather",
                            "input": {"city": "SF"},
                        },
                    ],
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"] == [
            {
                "role": "assistant",
                "content": "Checking weather...",
                "tool_calls": [
                    {
                        "id": result["messages"][0]["tool_calls"][0]["id"],
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "SF"}',
                        },
                    }
                ],
            }
        ]

    def test_assistant_message_with_tool_use_only(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_input_2",
                            "name": "get_weather",
                            "input": {"city": "NYC"},
                        }
                    ],
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0] == {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": result["messages"][0]["tool_calls"][0]["id"],
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "NYC"}',
                    },
                }
            ],
        }

    def test_user_message_with_tool_result_blocks(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        context = _make_context()
        context.id_map.register("call_in", "toolu_input_1")
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_input_1",
                            "content": "Sunny, 68F",
                        }
                    ],
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, context)

        assert result["messages"] == [
            {
                "role": "tool",
                "tool_call_id": "call_in",
                "content": "Sunny, 68F",
            }
        ]

    def test_multiple_tool_result_blocks_emit_separate_messages(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        context = _make_context()
        context.id_map.register("call_a", "toolu_a")
        context.id_map.register("call_b", "toolu_b")
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_a",
                            "content": "first",
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_b",
                            "content": "second",
                        },
                    ],
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, context)

        assert result["messages"] == [
            {"role": "tool", "tool_call_id": "call_a", "content": "first"},
            {"role": "tool", "tool_call_id": "call_b", "content": "second"},
        ]

    def test_tool_result_with_list_content_joined(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        context = _make_context()
        context.id_map.register("call_x", "toolu_x")
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_x",
                            "content": [
                                {"type": "text", "text": "line one"},
                                {"type": "text", "text": "line two"},
                            ],
                        }
                    ],
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, context)

        assert result["messages"][0] == {
            "role": "tool",
            "tool_call_id": "call_x",
            "content": "line one\nline two",
        }

    def test_tool_result_with_is_error_emits_warning(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        context = _make_context()
        context.id_map.register("call_x", "toolu_x")
        payload = {
            "model": "claude-3",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_x",
                            "content": "Tool failed",
                            "is_error": True,
                        }
                    ],
                }
            ],
        }
        _, warnings = transcoder.encode_request(payload, context)

        passthrough_warnings = [
            w for w in warnings if w.get("kind") == "tool_result_error_passthrough"
        ]
        assert len(passthrough_warnings) == 1

    def test_full_tool_request_fixture_translates(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = _load_fixture("tool_call_anthropic_request.json")
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"] == [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What's the weather in San Francisco?"},
        ]
        assert result["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]
        assert result["tool_choice"] == {
            "type": "function",
            "function": {"name": "get_weather"},
        }
