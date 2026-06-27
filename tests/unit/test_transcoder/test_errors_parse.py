"""Tests for upstream error envelope parsing."""

from __future__ import annotations

from eggpool.transcoder.errors import parse_upstream_error


def test_openai_error_with_object() -> None:
    body = {
        "error": {
            "type": "invalid_request_error",
            "message": "Invalid model",
        },
        "request_id": "req-123",
    }
    result = parse_upstream_error(400, body, protocol="openai")
    assert result.status_code == 400
    assert result.error_type == "invalid_request_error"
    assert result.message == "Invalid model"
    assert result.upstream_request_id == "req-123"


def test_openai_error_with_string() -> None:
    body = {"error": "Something went wrong"}
    result = parse_upstream_error(500, body, protocol="openai")
    assert result.status_code == 500
    assert result.error_type == "upstream_error"
    assert result.message == "Something went wrong"


def test_anthropic_error() -> None:
    body = {
        "type": "invalid_request_error",
        "error": {"message": "Bad request"},
        "request_id": "req-456",
    }
    result = parse_upstream_error(400, body, protocol="anthropic")
    assert result.status_code == 400
    assert result.error_type == "invalid_request_error"
    assert result.message == "Bad request"
    assert result.upstream_request_id == "req-456"


def test_anthropic_error_string_error() -> None:
    body = {"type": "api_error", "error": "overloaded"}
    result = parse_upstream_error(529, body, protocol="anthropic")
    assert result.status_code == 529
    assert result.error_type == "api_error"
    assert result.message == "overloaded"


def test_raw_preserved() -> None:
    body = {"error": {"type": "test", "message": "test"}}
    result = parse_upstream_error(400, body, protocol="openai")
    assert result.raw is body
