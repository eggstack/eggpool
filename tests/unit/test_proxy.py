"""Tests for proxy header filtering."""

from __future__ import annotations

from go_aggregator.proxy.client import (
    HOP_BY_HOP_HEADERS,
    filter_request_headers,
    filter_response_headers,
)


def test_filter_request_headers_removes_auth() -> None:
    headers = {
        "Authorization": "Bearer local-key",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "upstream-key")
    # Original dict is not modified
    assert "Authorization" in headers
    # Result has the upstream key
    assert result["Authorization"] == "Bearer upstream-key"
    assert result["Content-Type"] == "application/json"


def test_filter_request_headers_removes_hop_by_hop() -> None:
    headers = {
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "key")
    assert "Connection" not in result
    assert "Transfer-Encoding" not in result
    assert "Content-Type" in result


def test_filter_request_headers_removes_host() -> None:
    headers = {
        "Host": "example.com",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "key")
    assert "Host" not in result


def test_filter_response_headers_removes_hop_by_hop() -> None:
    class MockHeaders:
        def __init__(self, h: dict[str, str]) -> None:
            self._h = h

        def items(self) -> list[tuple[str, str]]:
            return list(self._h.items())

    headers = MockHeaders(
        {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "X-Custom": "value",
        }
    )
    result = filter_response_headers(headers)  # type: ignore[arg-type]
    assert "Content-Type" in result
    assert "Connection" not in result
    assert "X-Custom" in result


def test_hop_by_hop_headers_complete() -> None:
    expected = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
    assert expected == HOP_BY_HOP_HEADERS
