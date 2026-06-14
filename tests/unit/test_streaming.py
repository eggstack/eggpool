"""Tests for streaming proxy and usage extraction."""

from __future__ import annotations

from go_aggregator.proxy.usage import (
    AnthropicStreamUsageExtractor,
    OpenAIStreamUsageExtractor,
    StreamUsageResult,
)


class TestOpenAIStreamUsageExtractor:
    """Tests for OpenAI stream usage extraction."""

    def test_extract_usage_from_final_chunk(self) -> None:
        extractor = OpenAIStreamUsageExtractor()
        data = {
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 10},
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }
        result = extractor.extract(data)

        assert result is not None
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.cache_read_tokens == 10
        assert result.reasoning_tokens == 5
        assert result.is_complete is True

    def test_extract_no_usage(self) -> None:
        extractor = OpenAIStreamUsageExtractor()
        data = {"choices": [{"delta": {"content": "Hello"}}]}
        result = extractor.extract(data)
        assert result is None

    def test_extract_partial_usage(self) -> None:
        extractor = OpenAIStreamUsageExtractor()
        data = {
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 0,
            },
        }
        result = extractor.extract(data)

        assert result is not None
        assert result.input_tokens == 100
        assert result.output_tokens == 0
        assert result.is_complete is True


class TestAnthropicStreamUsageExtractor:
    """Tests for Anthropic stream usage extraction."""

    def test_extract_message_start(self) -> None:
        extractor = AnthropicStreamUsageExtractor()
        data = {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": 200,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                }
            },
        }
        result = extractor.extract(data)

        assert result is not None
        assert result.input_tokens == 200
        assert result.cache_read_tokens == 50
        assert result.cache_creation_tokens == 10
        assert result.is_complete is False

    def test_extract_message_delta(self) -> None:
        extractor = AnthropicStreamUsageExtractor()
        data = {
            "type": "message_delta",
            "usage": {"output_tokens": 75},
        }
        result = extractor.extract(data)

        assert result is not None
        assert result.output_tokens == 75
        assert result.is_complete is True

    def test_extract_content_block_delta_thinking(self) -> None:
        extractor = AnthropicStreamUsageExtractor()
        thinking_text = "Let me think about this..."
        data = {
            "type": "content_block_delta",
            "delta": {"type": "thinking", "thinking": thinking_text},
        }
        result = extractor.extract(data)

        assert result is not None
        assert result.reasoning_tokens == len(thinking_text)

    def test_extract_unknown_event(self) -> None:
        extractor = AnthropicStreamUsageExtractor()
        data = {"type": "content_block_delta", "delta": {"type": "text", "text": "Hi"}}
        result = extractor.extract(data)
        assert result is None


class TestStreamUsageResult:
    """Tests for StreamUsageResult merging."""

    def test_merge_usage_results(self) -> None:
        result1 = StreamUsageResult(input_tokens=100, output_tokens=0)
        result2 = StreamUsageResult(input_tokens=0, output_tokens=50, is_complete=True)

        # Merge
        result1.input_tokens += result2.input_tokens
        result1.output_tokens += result2.output_tokens
        if result2.is_complete:
            result1.is_complete = True

        assert result1.input_tokens == 100
        assert result1.output_tokens == 50
        assert result1.is_complete is True
