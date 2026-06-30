"""Tests for Anthropic → OpenAI response translation."""

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


class TestBasicResponseTranslation:
    def test_id_and_model_preserved(self, transcoder: AnthropicToOpenAI) -> None:
        payload = _load_fixture("openai_text_response.json")
        result, warnings = transcoder.decode_response(payload, _make_context())

        assert result["id"] == "chatcmpl-abc123"
        assert result["model"] == "gpt-4"
        assert result["type"] == "message"
        assert result["role"] == "assistant"

    def test_content_single_text_block(self, transcoder: AnthropicToOpenAI) -> None:
        payload = _load_fixture("openai_text_response.json")
        result, _ = transcoder.decode_response(payload, _make_context())

        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "text"
        assert result["content"][0]["text"] == "I'm doing well, thanks!"


class TestFinishReasonMapping:
    @pytest.mark.parametrize(
        "openai_finish,expected_stop_reason",
        [
            ("stop", "end_turn"),
            ("length", "max_tokens"),
            ("tool_calls", "tool_use"),
            ("content_filter", "refusal"),
        ],
    )
    def test_finish_reason_mapped(
        self,
        transcoder: AnthropicToOpenAI,
        openai_finish: str,
        expected_stop_reason: str,
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": openai_finish,
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["stop_reason"] == expected_stop_reason

    def test_unknown_finish_reason_defaults_to_end_turn(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "unknown",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["stop_reason"] == "end_turn"


class TestUsageMapping:
    def test_usage_prompt_and_completion_tokens(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"]["input_tokens"] == 25
        assert result["usage"]["output_tokens"] == 10

    def test_usage_cache_fields_preserved(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 8,
                "total_tokens": 28,
                "prompt_tokens_details": {"cached_tokens": 5},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"]["input_tokens"] == 20
        assert result["usage"]["output_tokens"] == 8
        assert result["usage"]["cache_read_input_tokens"] == 5

    def test_usage_cache_creation_fields_preserved(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 8,
                "total_tokens": 28,
                "prompt_tokens_details": {
                    "cached_tokens": 5,
                    "cache_creation_tokens": 9,
                },
            },
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"]["cache_read_input_tokens"] == 5
        assert result["usage"]["cache_creation_input_tokens"] == 9

    def test_usage_no_cache_omits_anthropic_cache(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert "cache_read_input_tokens" not in result["usage"]
        assert "cache_creation_input_tokens" not in result["usage"]

    def test_usage_missing_defaults_to_zero(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"]["input_tokens"] == 0
        assert result["usage"]["output_tokens"] == 0

    def test_usage_invalid_values_zeroed(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": "not-a-number",
                "completion_tokens": -10,
                "prompt_tokens_details": {
                    "cached_tokens": float("inf"),
                    "cache_creation_tokens": None,
                },
            },
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["usage"] == {
            "input_tokens": 0,
            "output_tokens": 0,
        }


class TestResponseStructure:
    def test_empty_content_returns_empty_blocks(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["content"] == []

    def test_none_content_stringified(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "tc_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["content"] == [
            {"type": "text", "text": "None"},
            {
                "type": "tool_use",
                "id": result["content"][1]["id"],
                "name": "get_weather",
                "input": {},
            },
        ]

    def test_empty_choices_returns_empty_content(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["content"] == []
        assert result["stop_reason"] == "end_turn"

    def test_system_fingerprint_dropped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "system_fingerprint": "fp_abc123",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert "system_fingerprint" not in result


class TestToolResponseTranslation:
    def test_tool_calls_become_tool_use_blocks(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "SF"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result, warnings = transcoder.decode_response(payload, _make_context())

        assert result["content"] == [
            {"type": "text", "text": "None"},
            {
                "type": "tool_use",
                "id": result["content"][1]["id"],
                "name": "get_weather",
                "input": {"city": "SF"},
            },
        ]
        assert result["stop_reason"] == "tool_use"
        id_warnings = [
            w
            for w in warnings
            if w.get("kind") == "tool_call_id_translated"
            and w.get("from") == "call_abc"
        ]
        assert len(id_warnings) == 1
        assert result["content"][1]["id"].startswith("toolu_")

    def test_text_and_tool_calls_both_emitted(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Checking weather...",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["content"] == [
            {"type": "text", "text": "Checking weather..."},
            {
                "type": "tool_use",
                "id": result["content"][1]["id"],
                "name": "get_weather",
                "input": {},
            },
        ]

    def test_multiple_tool_calls_become_multiple_tool_use_blocks(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "SF"}',
                                },
                            },
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "get_time",
                                    "arguments": '{"timezone": "PST"}',
                                },
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert len(result["content"]) == 3
        assert result["content"][0]["type"] == "text"
        assert result["content"][1]["type"] == "tool_use"
        assert result["content"][1]["name"] == "get_weather"
        assert result["content"][2]["type"] == "tool_use"
        assert result["content"][2]["name"] == "get_time"

    def test_malformed_tool_arguments_passed_as_raw(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "not-valid-json{",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        result, warnings = transcoder.decode_response(payload, _make_context())

        tool_use_block = result["content"][1]
        assert tool_use_block["input"] == {"__raw_arguments__": "not-valid-json{"}
        malformed_warnings = [
            w for w in warnings if w.get("kind") == "malformed_tool_arguments"
        ]
        assert len(malformed_warnings) == 1

    def test_tool_call_with_known_id_uses_id_map(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        context = _make_context()
        context.id_map.register("call_known", "toolu_known")
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_known",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        result, _ = transcoder.decode_response(payload, context)

        assert result["content"][1]["type"] == "tool_use"
        assert result["content"][1]["id"] != "toolu_known"
        assert result["content"][1]["id"].startswith("toolu_")

    def test_finish_reason_tool_calls_maps_to_stop_reason_tool_use(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["stop_reason"] == "tool_use"

    def test_refusal_with_tool_calls_emits_refusal_block(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {
            "id": "cmpl-1",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "refusal": "I cannot help with that.",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["content"][0] == {
            "type": "text",
            "text": "I cannot help with that.",
        }
        assert result["content"][1]["type"] == "tool_use"
        assert result["stop_reason"] == "refusal"

    def test_tool_call_openai_response_fixture(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = _load_fixture("tool_call_openai_response.json")
        result, _ = transcoder.decode_response(payload, _make_context())

        assert result["content"] == [
            {"type": "text", "text": "None"},
            {
                "type": "tool_use",
                "id": result["content"][1]["id"],
                "name": "get_weather",
                "input": {"city": "San Francisco"},
            },
        ]
        assert result["stop_reason"] == "tool_use"
