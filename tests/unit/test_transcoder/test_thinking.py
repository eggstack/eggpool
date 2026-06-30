"""Tests for Phase 6.3 — Extended thinking / reasoning transcoding."""

from __future__ import annotations

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic
from eggpool.transcoder.policy import TranscoderFeatures


def _make_context(
    client: str = "openai",
    upstream: str = "anthropic",
) -> TranscodeContext:
    return TranscodeContext(
        request_id="test-thinking",
        client_protocol=client,
        upstream_protocol=upstream,
    )


def _features(**kwargs: bool) -> TranscoderFeatures:
    defaults = {"thinking": True}
    defaults.update(kwargs)
    return TranscoderFeatures(**defaults)


# ---------------------------------------------------------------------------
# OpenAI → Anthropic thinking
# ---------------------------------------------------------------------------


class TestOpenAIToAnthropicThinking:
    def setup_method(self) -> None:
        self.transcoder = OpenAIToAnthropic()

    def test_reasoning_effort_low(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think hard"}],
            "reasoning_effort": "low",
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 1024}

    def test_reasoning_effort_medium(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think hard"}],
            "reasoning_effort": "medium",
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 4096}

    def test_reasoning_effort_high(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think hard"}],
            "reasoning_effort": "high",
        }
        result, _ = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assert result["thinking"] == {"type": "enabled", "budget_tokens": 16384}

    def test_reasoning_effort_disabled_dropped(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [{"role": "user", "content": "Think hard"}],
            "reasoning_effort": "high",
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=None
        )
        assert any(w.get("kind") == "dropped_field" for w in warnings)

    def test_reasoning_content_in_history(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {"role": "user", "content": "Think"},
                {
                    "role": "assistant",
                    "reasoning_content": "Let me think about this...",
                    "content": "The answer is 42.",
                },
            ],
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=_features()
        )
        assistant_msg = result["messages"][1]
        assert assistant_msg["content"][0] == {
            "type": "thinking",
            "thinking": "Let me think about this...",
        }
        assert assistant_msg["content"][1] == {
            "type": "text",
            "text": "The answer is 42.",
        }

    def test_reasoning_content_disabled_dropped(self) -> None:
        payload = {
            "model": "claude-3",
            "messages": [
                {"role": "user", "content": "Think"},
                {
                    "role": "assistant",
                    "reasoning_content": "Let me think...",
                    "content": "Answer",
                },
            ],
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context(), features=None
        )
        assert any(w.get("kind") == "reasoning_content_dropped" for w in warnings)

    def test_thinking_blocks_in_response(self) -> None:
        payload = {
            "id": "msg_123",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Reasoning here",
                    "signature": "sig_abc",
                },
                {"type": "text", "text": "The answer is 42."},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        result, warnings = self.transcoder.decode_response(
            payload, _make_context(), features=_features()
        )
        assert result["choices"][0]["message"]["reasoning_content"] == "Reasoning here"
        assert result["choices"][0]["message"]["content"] == "The answer is 42."
        sig_warnings = [
            w for w in warnings if w.get("kind") == "thinking_signature_dropped"
        ]
        assert len(sig_warnings) == 1

    def test_thinking_disabled_in_response(self) -> None:
        payload = {
            "id": "msg_123",
            "content": [
                {"type": "thinking", "thinking": "Reasoning here"},
                {"type": "text", "text": "Answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
        _, warnings = self.transcoder.decode_response(
            payload, _make_context(), features=None
        )
        assert any(w.get("kind") == "reasoning_content_dropped" for w in warnings)


# ---------------------------------------------------------------------------
# Anthropic → OpenAI thinking
# ---------------------------------------------------------------------------


class TestAnthropicToOpenAIThinking:
    def setup_method(self) -> None:
        self.transcoder = AnthropicToOpenAI()

    def test_thinking_in_history_translated(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "user", "content": "Think"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Let me think...",
                            "signature": "sig",
                        },
                        {"type": "text", "text": "The answer is 42."},
                    ],
                },
            ],
        }
        result, warnings = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=_features()
        )
        assistant_msg = result["messages"][1]
        assert assistant_msg["reasoning_content"] == "Let me think..."
        assert assistant_msg["content"] == "The answer is 42."
        sig_warnings = [
            w for w in warnings if w.get("kind") == "thinking_signature_dropped"
        ]
        assert len(sig_warnings) == 1

    def test_thinking_disabled_dropped(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "user", "content": "Think"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "Let me think..."},
                        {"type": "text", "text": "Answer"},
                    ],
                },
            ],
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=None
        )
        assert any(w.get("kind") == "reasoning_content_dropped" for w in warnings)

    def test_redacted_thinking_dropped(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [
                {"role": "user", "content": "Think"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "redacted_thinking", "data": "encrypted"},
                        {"type": "text", "text": "Answer"},
                    ],
                },
            ],
        }
        _, warnings = self.transcoder.encode_request(
            payload, _make_context("anthropic", "openai"), features=_features()
        )
        assert any(w.get("kind") == "dropped_field" for w in warnings)
