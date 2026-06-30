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

    def test_tool_messages_translate_to_user_tool_result(
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

        assert len(result["messages"]) == 2
        assert result["messages"][0] == {"role": "user", "content": "Hello"}
        assert result["messages"][1]["role"] == "user"
        assert result["messages"][1]["content"] == [
            {
                "type": "tool_result",
                "tool_use_id": result["messages"][1]["content"][0]["tool_use_id"],
                "content": "result",
            }
        ]
        tool_dropped_warnings = [
            w
            for w in warnings
            if w.get("kind") == "dropped_field" and w.get("field") == "messages[tool]"
        ]
        assert tool_dropped_warnings == []
        id_warnings = [
            w
            for w in warnings
            if w.get("kind") == "tool_call_id_translated" and w.get("from") == "tc_123"
        ]
        assert len(id_warnings) == 1

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
            "seed",
            "user",
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

    def test_stream_true_is_preserved(self, transcoder: OpenAIToAnthropic) -> None:
        payload = _load_fixture("openai_text_request.json")
        payload["stream"] = True
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["stream"] is True


class TestMalformedUsage:
    def test_decode_response_zeroes_invalid_usage(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "id": "msg_1",
            "model": "claude-3",
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": "bad",
                "output_tokens": -2,
                "cache_read_input_tokens": float("inf"),
                "cache_creation_input_tokens": None,
            },
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"] == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }


class TestToolTranslation:
    def test_tools_array_translates_to_anthropic_shape(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
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
            ],
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["tools"] == [
            {
                "name": "get_weather",
                "description": "Get the weather.",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ]
        tool_warnings = [w for w in warnings if w.get("field") == "tools"]
        assert tool_warnings == []

    def test_tool_strict_field_dropped_with_warning(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object"},
                        "strict": True,
                    },
                }
            ],
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert "strict" not in result["tools"][0]
        strict_warnings = [
            w for w in warnings if w.get("field") == "tools[].function.strict"
        ]
        assert len(strict_warnings) == 1

    def test_unsupported_tool_type_dropped_with_warning(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [{"type": "code_interpreter"}],
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert "tools" not in result
        unsupported_warnings = [
            w for w in warnings if w.get("kind") == "unsupported_tool_type"
        ]
        assert len(unsupported_warnings) == 1

    def test_tool_choice_auto_omitted(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": "auto",
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert "tool_choice" not in result

    def test_tool_choice_none_translates(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": "none",
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["tool_choice"] == {"type": "none"}

    def test_tool_choice_required_translates(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": "required",
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["tool_choice"] == {"type": "any"}

    def test_tool_choice_function_translates_to_tool(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "function", "function": {"name": "foo"}},
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["tool_choice"] == {"type": "tool", "name": "foo"}

    def test_tool_choice_invalid_name_dropped(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "tool_choice": {"type": "function", "function": {"name": ""}},
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert "tool_choice" not in result
        invalid_warnings = [
            w for w in warnings if w.get("kind") == "invalid_tool_choice"
        ]
        assert len(invalid_warnings) == 1

    def test_parallel_tool_calls_true_omitted(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "parallel_tool_calls": True,
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert "parallel_tool_calls" not in result
        collapse_warnings = [
            w for w in warnings if w.get("kind") == "parallel_tool_calls_collapsed"
        ]
        assert collapse_warnings == []

    def test_parallel_tool_calls_false_emits_warning(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "parallel_tool_calls": False,
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert "parallel_tool_calls" not in result
        collapse_warnings = [
            w for w in warnings if w.get("kind") == "parallel_tool_calls_collapsed"
        ]
        assert len(collapse_warnings) == 1
        assert collapse_warnings[0]["field"] == "parallel_tool_calls"

    def test_assistant_tool_calls_history_translates(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "assistant",
                    "content": "I'll check the weather.",
                    "tool_calls": [
                        {
                            "id": "call_history_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "SF"}',
                            },
                        }
                    ],
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0]["role"] == "assistant"
        tool_use_block = result["messages"][0]["content"][1]
        assert tool_use_block == {
            "type": "tool_use",
            "id": tool_use_block["id"],
            "name": "get_weather",
            "input": {"city": "SF"},
        }
        assert tool_use_block["id"].startswith("toolu_")

    def test_assistant_tool_calls_only_history_translates(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_xx",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, _make_context())

        assert result["messages"][0]["role"] == "assistant"
        assert result["messages"][0]["content"] == [
            {
                "type": "tool_use",
                "id": result["messages"][0]["content"][0]["id"],
                "name": "get_weather",
                "input": {},
            }
        ]

    def test_tool_result_message_with_string_content(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        context = _make_context()
        context.id_map.register("call_known", "toolu_known")
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_known",
                    "content": "Sunny, 68F",
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, context)

        assert result["messages"] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_known",
                        "content": "Sunny, 68F",
                    }
                ],
            }
        ]

    def test_tool_result_message_with_list_content(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        context = _make_context()
        context.id_map.register("call_x", "toolu_x")
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_x",
                    "content": [
                        {"type": "text", "text": "line one"},
                        {"type": "text", "text": "line two"},
                    ],
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, context)

        assert result["messages"][0]["content"] == [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_x",
                "content": "line one\nline two",
            }
        ]

    def test_tool_result_message_with_image_content_emits_warning(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        context = _make_context()
        context.id_map.register("call_x", "toolu_x")
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_x",
                    "content": [
                        {"type": "text", "text": "see image"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "http://example.com/img.png"},
                        },
                    ],
                }
            ],
        }
        _, warnings = transcoder.encode_request(payload, context)

        image_warnings = [
            w for w in warnings if w.get("kind") == "tool_result_image_dropped"
        ]
        assert len(image_warnings) == 1

    def test_tool_result_message_with_is_error_passes_through(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        context = _make_context()
        context.id_map.register("call_x", "toolu_x")
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_x",
                    "content": "Tool failed",
                    "is_error": True,
                }
            ],
        }
        result, _ = transcoder.encode_request(payload, context)

        assert result["messages"][0]["content"][0]["is_error"] is True

    def test_malformed_tool_arguments_passes_as_raw(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": "not-valid-json{",
                            },
                        }
                    ],
                }
            ],
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        tool_use_block = result["messages"][0]["content"][0]
        assert tool_use_block["type"] == "tool_use"
        assert tool_use_block["input"] == {"__raw_arguments__": "not-valid-json{"}
        malformed_warnings = [
            w for w in warnings if w.get("kind") == "malformed_tool_arguments"
        ]
        assert len(malformed_warnings) == 1

    def test_stream_options_include_usage_lifted_to_context(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        context = _make_context()
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream_options": {"include_usage": True},
        }
        result, _ = transcoder.encode_request(payload, context)

        assert "stream_options" not in result
        assert context.request_include_usage is True

    def test_stream_options_include_usage_false_lifted_to_context(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        context = _make_context()
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream_options": {"include_usage": False},
        }
        _, _ = transcoder.encode_request(payload, context)

        assert context.request_include_usage is False

    def test_full_tool_request_fixture_translates(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = _load_fixture("tool_call_openai_request.json")
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert result["messages"] == [
            {"role": "user", "content": "What's the weather in San Francisco?"}
        ]
        assert len(result["tools"]) == 2
        assert result["tools"][0]["name"] == "get_weather"
        assert result["tools"][0]["input_schema"] == {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        }
        assert result["tool_choice"] == {"type": "tool", "name": "get_weather"}
        assert "parallel_tool_calls" not in result
        strict_warnings = [
            w for w in warnings if w.get("field") == "tools[].function.strict"
        ]
        assert len(strict_warnings) == 1

    def test_history_messages_fixture_translates(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {
            "model": "gpt-4",
            "messages": _load_fixture("tool_call_history_messages.json"),
        }
        result, warnings = transcoder.encode_request(payload, _make_context())

        assert len(result["messages"]) == 4
        assert result["messages"][0] == {
            "role": "user",
            "content": "What's the weather in San Francisco?",
        }
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][1]["content"] == [
            {
                "type": "tool_use",
                "id": result["messages"][1]["content"][0]["id"],
                "name": "get_weather",
                "input": {"city": "San Francisco"},
            }
        ]
        assert result["messages"][2]["role"] == "user"
        assert result["messages"][2]["content"][0]["type"] == "tool_result"
        assert (
            result["messages"][2]["content"][0]["tool_use_id"]
            == (result["messages"][1]["content"][0]["id"])
        )
        assert (
            result["messages"][2]["content"][0]["content"]
            == '{"temperature": 68, "unit": "F"}'
        )
        assert result["messages"][3] == {
            "role": "assistant",
            "content": "It is 68 degrees Fahrenheit in San Francisco.",
        }
        id_warnings = [
            w for w in warnings if w.get("kind") == "tool_call_id_translated"
        ]
        assert len(id_warnings) == 1
