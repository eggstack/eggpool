"""Tests for proxy header filtering."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

from eggpool.api.proxy_request import (
    ProxyEndpointConfig,
    get_client_ip,
    handle_proxy_request,
)
from eggpool.proxy.client import (
    HOP_BY_HOP_HEADERS,
    filter_request_headers,
    filter_response_headers,
)


def _request_with_peer(peer: str, headers: list[tuple[bytes, bytes]]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 1234),
        "server": ("127.0.0.1", 11300),
        "scheme": "http",
    }
    return Request(scope, lambda: None)  # type: ignore[arg-type]


def test_forwarded_client_ip_ignored_from_untrusted_peer() -> None:
    request = _request_with_peer(
        "10.0.0.8",
        [(b"x-forwarded-for", b"198.51.100.4"), (b"x-real-ip", b"203.0.113.5")],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "10.0.0.8"


def test_forwarded_client_ip_honored_from_trusted_peer() -> None:
    request = _request_with_peer(
        "127.0.0.1",
        [(b"x-forwarded-for", b"198.51.100.4, 127.0.0.1")],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "198.51.100.4"


def test_forwarded_client_ip_uses_nearest_untrusted_hop() -> None:
    request = _request_with_peer(
        "127.0.0.1",
        [(b"x-forwarded-for", b"198.51.100.4, 10.0.0.8")],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "10.0.0.8"


def test_malformed_forwarded_client_ip_falls_back_to_peer() -> None:
    request = _request_with_peer(
        "127.0.0.1",
        [(b"x-forwarded-for", b"\x00" + b"x" * 100)],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "127.0.0.1"


def test_forwarded_client_ip_skips_empty_entries() -> None:
    request = _request_with_peer(
        "127.0.0.1",
        [(b"x-forwarded-for", b", 198.51.100.4")],
    )
    assert get_client_ip(request, trusted_proxies=("127.0.0.1",)) == "198.51.100.4"


def test_forwarded_ipv6_with_zone_id_passes_through() -> None:
    """IPv6 zone identifiers (RFC 6874) appear in ``%`` form in raw headers."""
    request = _request_with_peer(
        "::1",
        [(b"x-forwarded-for", b"fe80::1%eth0")],
    )
    assert get_client_ip(request, trusted_proxies=("::1",)) == "fe80::1%eth0"


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


@pytest.mark.asyncio
async def test_lease_failure_log_uses_proxy_request_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime_manager = SimpleNamespace(
        acquire=AsyncMock(side_effect=RuntimeError("unavailable"))
    )
    app = SimpleNamespace(state=SimpleNamespace(runtime_manager=runtime_manager))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "app": app,
    }
    request = Request(scope, lambda: None)  # type: ignore[arg-type]
    endpoint = ProxyEndpointConfig(
        protocol="openai",
        request_label="test",
        error_response=lambda status_code, message, error_type="server_error": (
            JSONResponse(
                {"message": message, "type": error_type}, status_code=status_code
            )
        ),
        not_found_error_type="not_found",
        service_error_type="server_error",
    )

    with caplog.at_level("WARNING", logger="eggpool.api.proxy_request"):
        response = await handle_proxy_request(request, endpoint)

    assert response.status_code == 503
    record = next(
        record
        for record in caplog.records
        if record.message == "Runtime lease acquisition failed; returning 503"
    )
    assert record.proxy_request_id == request.state.proxy_request_id
