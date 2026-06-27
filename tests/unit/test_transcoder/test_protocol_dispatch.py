"""Tests for the select_transcoder factory and BodyTranscoder Protocol."""

from __future__ import annotations

import pytest

from eggpool.errors import ConfigError
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.protocol import select_transcoder


def _make_context(client: str, upstream: str) -> TranscodeContext:
    return TranscodeContext(
        request_id="req-test",
        client_protocol=client,
        upstream_protocol=upstream,
    )


class TestSelectTranscoder:
    def test_same_protocol_returns_none(self) -> None:
        result = select_transcoder(client_protocol="openai", upstream_protocol="openai")
        assert result is None

    def test_same_anthropic_returns_none(self) -> None:
        result = select_transcoder(
            client_protocol="anthropic", upstream_protocol="anthropic"
        )
        assert result is None

    def test_openai_to_anthropic_returns_transcoder(self) -> None:
        result = select_transcoder(
            client_protocol="openai", upstream_protocol="anthropic"
        )
        assert result is not None
        assert result.client_protocol == "openai"
        assert result.upstream_protocol == "anthropic"

    def test_anthropic_to_openai_returns_transcoder(self) -> None:
        result = select_transcoder(
            client_protocol="anthropic", upstream_protocol="openai"
        )
        assert result is not None
        assert result.client_protocol == "anthropic"
        assert result.upstream_protocol == "openai"

    def test_unknown_pair_raises_config_error(self) -> None:
        with pytest.raises(ConfigError, match="Unknown protocol pair"):
            select_transcoder(client_protocol="openai", upstream_protocol="grpc")

    def test_another_unknown_pair_raises(self) -> None:
        with pytest.raises(ConfigError):
            select_transcoder(client_protocol="custom", upstream_protocol="openai")


class TestTranscoderProtocolCompliance:
    def test_openai_to_anthropic_has_required_methods(self) -> None:
        from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic

        t = OpenAIToAnthropic()
        ctx = _make_context("openai", "anthropic")

        assert hasattr(t, "encode_request")
        assert hasattr(t, "decode_response")
        assert hasattr(t, "reencode_error")
        assert t.client_protocol == "openai"
        assert t.upstream_protocol == "anthropic"

        result, warnings = t.encode_request(
            {"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            ctx,
        )
        assert isinstance(result, dict)
        assert isinstance(warnings, list)

    def test_anthropic_to_openai_has_required_methods(self) -> None:
        from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI

        t = AnthropicToOpenAI()
        ctx = _make_context("anthropic", "openai")

        assert hasattr(t, "encode_request")
        assert hasattr(t, "decode_response")
        assert hasattr(t, "reencode_error")
        assert t.client_protocol == "anthropic"
        assert t.upstream_protocol == "openai"

        result, warnings = t.encode_request(
            {"model": "claude-3", "messages": [{"role": "user", "content": "Hi"}]},
            ctx,
        )
        assert isinstance(result, dict)
        assert isinstance(warnings, list)

    def test_encode_request_returns_tuple(self) -> None:
        from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic

        t = OpenAIToAnthropic()
        ctx = _make_context("openai", "anthropic")
        result = t.encode_request(
            {"model": "gpt-4", "messages": []},
            ctx,
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], dict)
        assert isinstance(result[1], list)

    def test_decode_response_returns_tuple(self) -> None:
        from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI

        t = AnthropicToOpenAI()
        ctx = _make_context("anthropic", "openai")
        result = t.decode_response(
            {"id": "cmpl-1", "model": "gpt-4", "choices": [], "usage": {}},
            ctx,
        )
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_reencode_error_returns_tuple(self) -> None:
        from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic

        t = OpenAIToAnthropic()
        ctx = _make_context("openai", "anthropic")
        result = t.reencode_error(
            500,
            {"error": {"type": "api_error", "message": "err"}},
            ctx,
        )
        assert isinstance(result, tuple)
        assert len(result) == 3
        assert isinstance(result[0], int)
        assert isinstance(result[1], dict)
        assert isinstance(result[2], list)
