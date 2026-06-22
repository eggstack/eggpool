"""Tests for proxy header filtering."""

from __future__ import annotations

from eggpool.proxy.client import (
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
        def __init__(self, h: list[tuple[bytes, bytes]]) -> None:
            self._raw = h

        @property
        def raw(self) -> list[tuple[bytes, bytes]]:
            return self._raw

    headers = MockHeaders(
        [
            (b"Content-Type", b"application/json"),
            (b"Connection", b"keep-alive"),
            (b"X-Custom", b"value"),
        ]
    )
    result = filter_response_headers(headers)  # type: ignore[arg-type]
    result_dict = {k.lower(): v for k, v in result}
    assert "content-type" in result_dict
    assert "connection" not in result_dict
    assert "x-custom" in result_dict


def test_hop_by_hop_headers_complete() -> None:
    expected = {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
    assert expected == HOP_BY_HOP_HEADERS
