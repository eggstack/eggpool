"""Tests for Phase 6.5 — Anthropic primitives transcoding."""

from __future__ import annotations

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext


def _make_context() -> TranscodeContext:
    return TranscodeContext(
        request_id="test-primitives",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )


class TestAnthropicPrimitives:
    def setup_method(self) -> None:
        self.transcoder = AnthropicToOpenAI()

    def test_top_k_dropped_with_specific_warning(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "top_k": 40,
        }
        _, warnings = self.transcoder.encode_request(payload, _make_context())
        tk = [w for w in warnings if w.get("kind") == "top_k_dropped"]
        assert len(tk) == 1
        assert tk[0]["field"] == "top_k"

    def test_cache_control_dropped(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "cache_control": {"type": "ephemeral"},
        }
        _, warnings = self.transcoder.encode_request(payload, _make_context())
        cc = [w for w in warnings if w.get("kind") == "cache_control_dropped"]
        assert len(cc) == 1

    def test_context_management_dropped(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "context_management": {"edits": []},
        }
        _, warnings = self.transcoder.encode_request(payload, _make_context())
        cm = [w for w in warnings if w.get("field") == "context_management"]
        assert len(cm) == 1
        assert cm[0]["reason"] == "experimental"

    def test_container_dropped(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "container": {"type": "sandbox"},
        }
        _, warnings = self.transcoder.encode_request(payload, _make_context())
        ct = [w for w in warnings if w.get("field") == "container"]
        assert len(ct) == 1
        assert ct[0]["reason"] == "experimental"

    def test_mcp_servers_dropped(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "mcp_servers": [{"name": "test"}],
        }
        _, warnings = self.transcoder.encode_request(payload, _make_context())
        mc = [w for w in warnings if w.get("field") == "mcp_servers"]
        assert len(mc) == 1
        assert mc[0]["reason"] == "experimental"

    def test_metadata_user_id_translated(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "metadata": {"user_id": "user-123"},
        }
        result, _ = self.transcoder.encode_request(payload, _make_context())
        assert result["user"] == "user-123"

    def test_stop_sequences_translated(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stop_sequences": ["END", "STOP"],
        }
        result, _ = self.transcoder.encode_request(payload, _make_context())
        assert result["stop"] == ["END", "STOP"]

    def test_thinking_still_dropped(self) -> None:
        payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        }
        _, warnings = self.transcoder.encode_request(payload, _make_context())
        th = [w for w in warnings if w.get("field") == "thinking"]
        assert len(th) == 1
