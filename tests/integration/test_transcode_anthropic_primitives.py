"""Integration tests for Anthropic primitives transcoding.

Tests end-to-end encode→decode pipelines for Anthropic-only fields
(top_k, cache_control, context_management, container, mcp_servers,
metadata.user_id, stop_sequences, thinking).
"""

from __future__ import annotations

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext


class TestAnthropicPrimitivesRoundTrip:
    def setup_method(self) -> None:
        self.ctx = TranscodeContext(
            request_id="integ-primitives-1",
            client_protocol="anthropic",
            upstream_protocol="openai",
        )
        self.transcoder = AnthropicToOpenAI()

    def test_top_k_dropped(self) -> None:
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "top_k": 40,
        }
        _, warnings = self.transcoder.encode_request(request, self.ctx)
        assert any(w.get("kind") == "top_k_dropped" for w in warnings)

    def test_cache_control_dropped(self) -> None:
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "cache_control": {"type": "ephemeral"},
        }
        _, warnings = self.transcoder.encode_request(request, self.ctx)
        assert any(w.get("kind") == "cache_control_feature_disabled" for w in warnings)

    def test_context_management_dropped(self) -> None:
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "context_management": {"edits": []},
        }
        _, warnings = self.transcoder.encode_request(request, self.ctx)
        assert any(
            w.get("field") == "context_management" and w.get("reason") == "experimental"
            for w in warnings
        )

    def test_container_dropped(self) -> None:
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "container": {"type": "sandbox"},
        }
        _, warnings = self.transcoder.encode_request(request, self.ctx)
        assert any(
            w.get("field") == "container" and w.get("reason") == "experimental"
            for w in warnings
        )

    def test_mcp_servers_dropped(self) -> None:
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "mcp_servers": [{"name": "test"}],
        }
        _, warnings = self.transcoder.encode_request(request, self.ctx)
        assert any(
            w.get("field") == "mcp_servers" and w.get("reason") == "experimental"
            for w in warnings
        )

    def test_metadata_user_id_translated(self) -> None:
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "metadata": {"user_id": "user-123"},
        }
        result, _ = self.transcoder.encode_request(request, self.ctx)
        assert result["user"] == "user-123"

    def test_stop_sequences_translated(self) -> None:
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stop_sequences": ["END", "STOP"],
        }
        result, _ = self.transcoder.encode_request(request, self.ctx)
        assert result["stop"] == ["END", "STOP"]

    def test_thinking_dropped(self) -> None:
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        }
        _, warnings = self.transcoder.encode_request(request, self.ctx)
        assert any(w.get("field") == "thinking" for w in warnings)

    def test_multiple_primitives_combined(self) -> None:
        """All primitives in one request produce independent warnings."""
        request = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "top_k": 10,
            "cache_control": {"type": "ephemeral"},
            "context_management": {"edits": []},
            "container": {"type": "sandbox"},
            "mcp_servers": [{"name": "mcp"}],
        }
        _, warnings = self.transcoder.encode_request(request, self.ctx)
        warning_fields = {(w.get("kind"), w.get("field")) for w in warnings}
        assert ("top_k_dropped", "top_k") in warning_fields
        assert ("cache_control_feature_disabled", "cache_control") in warning_fields
        assert ("dropped_field", "context_management") in warning_fields
        assert ("dropped_field", "container") in warning_fields
        assert ("dropped_field", "mcp_servers") in warning_fields

    def test_response_decode_preserves_usage(self) -> None:
        """Usage fields decode correctly even when primitives were dropped."""
        response = {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "model": "gpt-4",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hello!"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        client_response, _ = self.transcoder.decode_response(response, self.ctx)
        assert client_response["usage"]["input_tokens"] == 10
        assert client_response["usage"]["output_tokens"] == 5
