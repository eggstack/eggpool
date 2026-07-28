"""ParsedUpstreamResponse lifecycle tests."""

from __future__ import annotations

import json

from eggpool.request.parsed_upstream_response import (
    ParsedUpstreamResponse,
    build_parsed_upstream_response,
)


class TestParsedUpstreamResponse:
    def test_parse_valid_dict(self) -> None:
        body = json.dumps({"usage": {"input_tokens": 10}}).encode()
        resp = build_parsed_upstream_response(
            status_code=200,
            headers=[("content-type", "application/json")],
            raw_body=body,
        )
        assert resp.status_code == 200
        assert resp.parse_status == "parsed"
        assert isinstance(resp.parsed_dict, dict)
        assert resp.parsed_dict is not None
        assert resp.parsed_dict["usage"]["input_tokens"] == 10

    def test_parse_valid_list(self) -> None:
        body = b'[{"id": 1}]'
        resp = build_parsed_upstream_response(
            status_code=200, headers=[], raw_body=body
        )
        assert resp.parse_status == "parsed"
        assert isinstance(resp.parsed_json, list)
        assert resp.parsed_dict is None  # not a dict

    def test_parse_invalid_json(self) -> None:
        resp = build_parsed_upstream_response(
            status_code=200, headers=[], raw_body=b"not json"
        )
        assert resp.parse_status == "invalid_json"
        assert resp.parsed_json is None
        assert resp.parsed_dict is None

    def test_parse_non_object_json(self) -> None:
        resp = build_parsed_upstream_response(
            status_code=200, headers=[], raw_body=b'"just a string"'
        )
        assert resp.parse_status == "non_object"
        assert resp.parsed_json is None

    def test_parse_laziness(self) -> None:
        body = b'{"key": "value"}'
        resp = build_parsed_upstream_response(
            status_code=200, headers=[], raw_body=body
        )
        # Before accessing parsed_json, status is not_attempted
        assert resp._parse_status == "not_attempted"
        # Access triggers parse
        _ = resp.parsed_json
        assert resp._parse_status == "parsed"
        # Second access reuses cached result
        assert resp.parsed_json is not None

    def test_is_success(self) -> None:
        resp = build_parsed_upstream_response(200, [], b"")
        assert resp.is_success is True
        resp_err = build_parsed_upstream_response(400, [], b"")
        assert resp_err.is_success is False

    def test_is_error(self) -> None:
        resp = build_parsed_upstream_response(500, [], b"")
        assert resp.is_error is True
        resp_ok = build_parsed_upstream_response(200, [], b"")
        assert resp_ok.is_error is False

    def test_header_value_case_insensitive(self) -> None:
        resp = build_parsed_upstream_response(
            200,
            [("X-Request-Id", "abc-123"), ("Content-Type", "application/json")],
            b"",
        )
        assert resp.header_value("x-request-id") == "abc-123"
        assert resp.header_value("X-Request-Id") == "abc-123"
        assert resp.header_value("content-type") == "application/json"
        assert resp.header_value("missing") is None

    def test_raw_body_preserved(self) -> None:
        body = b'{"raw": true}'
        resp = build_parsed_upstream_response(200, [], body)
        assert resp.raw_body == body

    def test_empty_body(self) -> None:
        resp = build_parsed_upstream_response(200, [], b"")
        assert resp.parse_status == "invalid_json"
        assert resp.parsed_json is None


class TestBuildParsedUpstreamResponse:
    def test_creates_instance(self) -> None:
        resp = build_parsed_upstream_response(
            status_code=201,
            headers=[("location", "/foo")],
            raw_body=b"created",
        )
        assert isinstance(resp, ParsedUpstreamResponse)
        assert resp.status_code == 201
