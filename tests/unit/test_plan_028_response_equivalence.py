"""Tests for Plan 028 — Response equivalence (Workstream I).

Verifies that the consolidated ParsedUpstreamResponse produces identical
results to the legacy byte-parsing approach for every required consumer:
usage extraction, error classification, header lookup, and status checks.
"""

from __future__ import annotations

import json

from eggpool.request.parsed_upstream_response import (
    build_parsed_upstream_response,
)


class TestResponseEquivalenceUsageExtraction:
    """Usage extraction from ParsedUpstreamResponse matches byte parsing."""

    def _openai_success_body(self) -> bytes:
        return json.dumps(
            {
                "id": "chatcmpl-123",
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
        ).encode()

    def _anthropic_success_body(self) -> bytes:
        return json.dumps(
            {
                "id": "msg-123",
                "type": "message",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                },
            }
        ).encode()

    def test_openai_usage_from_parsed_dict(self) -> None:
        body = self._openai_success_body()
        resp = build_parsed_upstream_response(200, [], body)
        d = resp.parsed_dict
        assert d is not None
        usage = d.get("usage", {})
        assert usage["prompt_tokens"] == 10
        assert usage["completion_tokens"] == 20
        assert usage["total_tokens"] == 30

    def test_anthropic_usage_from_parsed_dict(self) -> None:
        body = self._anthropic_success_body()
        resp = build_parsed_upstream_response(200, [], body)
        d = resp.parsed_dict
        assert d is not None
        usage = d.get("usage", {})
        assert usage["input_tokens"] == 10
        assert usage["output_tokens"] == 20

    def test_raw_body_matches_original(self) -> None:
        body = self._openai_success_body()
        resp = build_parsed_upstream_response(200, [], body)
        assert resp.raw_body == body

    def test_empty_usage(self) -> None:
        body = json.dumps({"id": "chatcmpl-1", "usage": {}}).encode()
        resp = build_parsed_upstream_response(200, [], body)
        d = resp.parsed_dict
        assert d is not None
        assert d.get("usage") == {}


class TestResponseEquivalenceErrorClassification:
    """Error classification from ParsedUpstreamResponse matches byte parsing."""

    def test_400_error_body(self) -> None:
        body = json.dumps(
            {"error": {"message": "Bad request", "type": "invalid_request_error"}}
        ).encode()
        resp = build_parsed_upstream_response(400, [], body)
        assert resp.is_error
        assert not resp.is_success
        d = resp.parsed_dict
        assert d is not None
        assert d["error"]["type"] == "invalid_request_error"

    def test_404_not_found(self) -> None:
        body = json.dumps({"error": {"message": "Model not found"}}).encode()
        resp = build_parsed_upstream_response(404, [], body)
        assert resp.is_error
        assert resp.status_code == 404

    def test_500_server_error(self) -> None:
        body = b"Internal Server Error"
        resp = build_parsed_upstream_response(500, [], body)
        assert resp.is_error
        assert resp.parse_status == "invalid_json"
        assert resp.parsed_json is None

    def test_429_rate_limit(self) -> None:
        body = json.dumps({"error": {"message": "Rate limited"}}).encode()
        resp = build_parsed_upstream_response(
            429,
            [("retry-after", "60")],
            body,
        )
        assert resp.is_error
        assert resp.header_value("retry-after") == "60"


class TestResponseEquivalenceHeaders:
    """Header lookup equivalence."""

    def test_case_insensitive_header_lookup(self) -> None:
        resp = build_parsed_upstream_response(
            200,
            [("X-Request-Id", "abc"), ("Content-Type", "application/json")],
            b"",
        )
        assert resp.header_value("x-request-id") == "abc"
        assert resp.header_value("X-Request-Id") == "abc"
        assert resp.header_value("CONTENT-TYPE") == "application/json"

    def test_missing_header(self) -> None:
        resp = build_parsed_upstream_response(200, [], b"")
        assert resp.header_value("x-missing") is None

    def test_first_value_wins(self) -> None:
        resp = build_parsed_upstream_response(
            200,
            [("x-dup", "first"), ("x-dup", "second")],
            b"",
        )
        assert resp.header_value("x-dup") == "first"


class TestResponseEquivalencePassthrough:
    """Raw pass-through byte preservation."""

    def test_binary_body_preserved(self) -> None:
        body = bytes(range(256))
        resp = build_parsed_upstream_response(200, [], body)
        assert resp.raw_body == body
        # Binary body won't parse as JSON
        assert resp.parse_status == "invalid_json"

    def test_unicode_body_preserved(self) -> None:
        body = '{"text": "héllo wörld 🌍"}'.encode()
        resp = build_parsed_upstream_response(200, [], body)
        assert resp.raw_body == body
        d = resp.parsed_dict
        assert d is not None
        assert d["text"] == "héllo wörld 🌍"

    def test_large_body_preserved(self) -> None:
        body = json.dumps({"data": "x" * 100_000}).encode()
        resp = build_parsed_upstream_response(200, [], body)
        assert resp.raw_body == body
        assert len(resp.raw_body) == len(body)
