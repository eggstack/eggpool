"""Tests for cross-protocol error translation."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from eggpool.transcoder.anthropic_to_openai import AnthropicToOpenAI
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.openai_to_anthropic import OpenAIToAnthropic

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _make_openai_context() -> TranscodeContext:
    return TranscodeContext(
        request_id="req-test",
        client_protocol="openai",
        upstream_protocol="anthropic",
    )


def _make_anthropic_context() -> TranscodeContext:
    return TranscodeContext(
        request_id="req-test",
        client_protocol="anthropic",
        upstream_protocol="openai",
    )


class TestAnthropicToOpenAIError:
    """AnthropicToOpenAI.reencode_error: re-encodes OpenAI errors into Anthropic format.

    Input is an OpenAI error (type inside error object).
    Output: {"type": mapped_type, "error": {"message": ...}}.
    """

    @pytest.fixture
    def transcoder(self) -> AnthropicToOpenAI:
        return AnthropicToOpenAI()

    def test_authentication_error_mapped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "invalid_api_key", "message": "Invalid API key"}}
        status, body, warnings = transcoder.reencode_error(
            401, payload, _make_anthropic_context()
        )

        assert status == 401
        assert body["type"] == "authentication_error"
        assert body["error"]["message"] == "Invalid API key"

    def test_invalid_request_error_mapped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "invalid_request_error", "message": "Bad request"}}
        status, body, _ = transcoder.reencode_error(
            400, payload, _make_anthropic_context()
        )

        assert status == 400
        assert body["type"] == "invalid_request_error"

    def test_rate_limit_error_mapped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "rate_limit_exceeded", "message": "Rate limited"}}
        status, body, _ = transcoder.reencode_error(
            429, payload, _make_anthropic_context()
        )

        assert status == 429
        assert body["type"] == "rate_limit_error"

    def test_api_error_mapped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "api_error", "message": "Server error"}}
        status, body, _ = transcoder.reencode_error(
            500, payload, _make_anthropic_context()
        )

        assert status == 500
        assert body["type"] == "api_error"

    def test_overloaded_error_mapped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "overloaded_error", "message": "Overloaded"}}
        status, body, _ = transcoder.reencode_error(
            529, payload, _make_anthropic_context()
        )

        assert status == 529
        assert body["type"] == "api_error"

    def test_permission_error_mapped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "permission_error", "message": "Forbidden"}}
        status, body, _ = transcoder.reencode_error(
            403, payload, _make_anthropic_context()
        )

        assert body["type"] == "api_error"

    def test_not_found_error_mapped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "not_found_error", "message": "Not found"}}
        status, body, _ = transcoder.reencode_error(
            404, payload, _make_anthropic_context()
        )

        assert body["type"] == "api_error"

    def test_billing_error_mapped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "billing_error", "message": "Billing issue"}}
        status, body, _ = transcoder.reencode_error(
            402, payload, _make_anthropic_context()
        )

        assert body["type"] == "api_error"

    def test_timeout_error_mapped(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "timeout_error", "message": "Timed out"}}
        status, body, _ = transcoder.reencode_error(
            504, payload, _make_anthropic_context()
        )

        assert body["type"] == "api_error"

    def test_unknown_error_type_mapped_to_api_error(
        self, transcoder: AnthropicToOpenAI
    ) -> None:
        payload = {"error": {"type": "some_new_error", "message": "Something new"}}
        status, body, _ = transcoder.reencode_error(
            500, payload, _make_anthropic_context()
        )

        assert body["type"] == "api_error"
        assert body["error"]["message"] == "Something new"

    def test_none_payload_returns_default(self, transcoder: AnthropicToOpenAI) -> None:
        status, body, warnings = transcoder.reencode_error(
            500, None, _make_anthropic_context()
        )

        assert status == 500
        assert body["type"] == "api_error"
        assert body["error"]["message"] == "Unknown error"

    def test_missing_error_field_handled(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"type": "api_error"}
        status, body, _ = transcoder.reencode_error(
            500, payload, _make_anthropic_context()
        )

        assert body["type"] == "api_error"
        assert body["error"]["message"] == "{}"

    def test_string_error_field_handled(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"type": "api_error", "error": "something broke"}
        status, body, _ = transcoder.reencode_error(
            500, payload, _make_anthropic_context()
        )

        assert body["type"] == "api_error"
        assert body["error"]["message"] == "something broke"

    def test_status_code_preserved(self, transcoder: AnthropicToOpenAI) -> None:
        payload = {"error": {"type": "api_error", "message": "err"}}
        for code in [400, 401, 403, 404, 429, 500, 502, 529]:
            status, _, _ = transcoder.reencode_error(
                code, payload, _make_anthropic_context()
            )
            assert status == code


class TestOpenAIToAnthropicError:
    """OpenAIToAnthropic.reencode_error: re-encodes Anthropic errors into OpenAI format.

    Input is an Anthropic error (type at top level and inside error object).
    Output: {"error": {"message": ..., "type": mapped, "code": orig, "param": None}}.
    """

    @pytest.fixture
    def transcoder(self) -> OpenAIToAnthropic:
        return OpenAIToAnthropic()

    def test_authentication_error_mapped(self, transcoder: OpenAIToAnthropic) -> None:
        payload = _load_fixture("anthropic_error_response.json")
        status, body, warnings = transcoder.reencode_error(
            401, payload, _make_openai_context()
        )

        assert status == 401
        assert body["error"]["message"] == "Invalid API key"
        assert body["error"]["type"] == "invalid_api_key"
        assert body["error"]["code"] == "authentication_error"

    def test_invalid_request_error_mapped(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {"type": "invalid_request_error", "error": {"message": "Bad model"}}
        status, body, _ = transcoder.reencode_error(
            400, payload, _make_openai_context()
        )

        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["code"] == "invalid_request_error"

    def test_rate_limit_error_mapped(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {"type": "rate_limit_error", "error": {"message": "Slow down"}}
        status, body, _ = transcoder.reencode_error(
            429, payload, _make_openai_context()
        )

        assert body["error"]["type"] == "rate_limit_exceeded"
        assert body["error"]["code"] == "rate_limit_error"

    def test_server_error_mapped(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {"type": "api_error", "error": {"message": "Internal"}}
        status, body, _ = transcoder.reencode_error(
            500, payload, _make_openai_context()
        )

        assert body["error"]["type"] == "api_error"
        assert body["error"]["code"] == "api_error"

    def test_unknown_error_type_mapped_to_invalid_request(
        self, transcoder: OpenAIToAnthropic
    ) -> None:
        payload = {"type": "brand_new_error", "error": {"message": "New"}}
        status, body, _ = transcoder.reencode_error(
            400, payload, _make_openai_context()
        )

        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["code"] == "brand_new_error"

    def test_none_payload_returns_default(self, transcoder: OpenAIToAnthropic) -> None:
        status, body, warnings = transcoder.reencode_error(
            500, None, _make_openai_context()
        )

        assert status == 500
        assert body["error"]["type"] == "api_error"
        assert body["error"]["message"] == "Unknown error"

    def test_string_error_field_handled(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {"error": "something broke"}
        status, body, _ = transcoder.reencode_error(
            500, payload, _make_openai_context()
        )

        assert body["error"]["message"] == "something broke"

    def test_status_code_preserved(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {"type": "test", "error": {"message": "test"}}
        for code in [400, 401, 403, 404, 429, 500, 502, 529]:
            status, _, _ = transcoder.reencode_error(
                code, payload, _make_openai_context()
            )
            assert status == code

    def test_empty_error_obj_uses_fallback(self, transcoder: OpenAIToAnthropic) -> None:
        payload = {"type": "api_error", "error": {}}
        status, body, _ = transcoder.reencode_error(
            400, payload, _make_openai_context()
        )

        assert body["error"]["type"] == "api_error"
        assert body["error"]["code"] == "api_error"
