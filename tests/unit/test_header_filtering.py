"""Tests for proxy header filtering.

Verifies the credential-boundary contract: a local client credential
must never reach the upstream service in any form, and the upstream
request must carry exactly one ``Authorization`` header holding the
selected account's credential.
"""

from __future__ import annotations

from go_aggregator.proxy.client import (
    HOP_BY_HOP_HEADERS,
    LOCAL_CREDENTIAL_HEADERS,
    build_upstream_auth_headers,
    filter_request_headers,
    filter_response_headers,
)


def test_local_credential_headers_contain_dangerous_keys() -> None:
    """The credential set includes every local-auth-bearing header."""
    assert "authorization" in LOCAL_CREDENTIAL_HEADERS
    assert "x-api-key" in LOCAL_CREDENTIAL_HEADERS
    assert "proxy-authorization" in LOCAL_CREDENTIAL_HEADERS


def test_filter_request_headers_strips_authorization() -> None:
    headers = {
        "Authorization": "Bearer local-bearer-secret",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "upstream-key")
    assert "Authorization" in headers
    assert result["Authorization"] == "Bearer upstream-key"
    assert result["Content-Type"] == "application/json"
    assert "local-bearer-secret" not in result["Authorization"]


def test_filter_request_headers_strips_x_api_key() -> None:
    headers = {
        "X-Api-Key": "local-x-api-secret",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "upstream-key")
    assert "X-Api-Key" not in result
    assert "x-api-key" not in {k.lower() for k in result}
    assert result["Authorization"] == "Bearer upstream-key"


def test_filter_request_headers_strips_proxy_authorization() -> None:
    headers = {
        "Proxy-Authorization": "Basic local-proxy-secret",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "upstream-key")
    assert "Proxy-Authorization" not in result
    assert "proxy-authorization" not in {k.lower() for k in result}
    assert result["Authorization"] == "Bearer upstream-key"


def test_filter_request_headers_strips_lowercase_variants() -> None:
    headers = {
        "authorization": "Bearer local-secret",
        "X-API-KEY": "local-x-secret",
    }
    result = filter_request_headers(headers, "upstream-key")
    # The local client values must not survive; the upstream
    # Authorization is the only Authorization in the output.
    serialized = "\n".join(f"{k}: {v}" for k, v in result.items())
    assert "local-secret" not in serialized
    assert "local-x-secret" not in serialized
    assert result["Authorization"] == "Bearer upstream-key"


def test_filter_request_headers_injects_exactly_one_authorization() -> None:
    headers = {
        "Authorization": "Bearer local-bearer",
        "X-Api-Key": "local-x",
        "Proxy-Authorization": "Basic local-proxy",
    }
    result = filter_request_headers(headers, "upstream-key")
    auth_keys = [k for k in result if k.lower() == "authorization"]
    assert len(auth_keys) == 1
    assert result[auth_keys[0]] == "Bearer upstream-key"


def test_filter_request_headers_strips_hop_by_hop() -> None:
    headers = {
        "Connection": "keep-alive",
        "Transfer-Encoding": "chunked",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "key")
    assert "Connection" not in result
    assert "Transfer-Encoding" not in result
    assert result["Content-Type"] == "application/json"


def test_filter_request_headers_strips_host() -> None:
    headers = {
        "Host": "example.com",
        "Content-Type": "application/json",
    }
    result = filter_request_headers(headers, "key")
    assert "Host" not in result


def test_filter_request_headers_preserves_unrelated_headers() -> None:
    headers = {
        "X-Custom-Header": "value",
        "User-Agent": "test-agent",
    }
    result = filter_request_headers(headers, "key")
    assert result["X-Custom-Header"] == "value"
    assert result["User-Agent"] == "test-agent"
    assert result["Authorization"] == "Bearer key"


def test_filter_request_headers_no_local_secrets_in_output() -> None:
    """The filtered output must contain no local client secrets."""
    local_bearer = "LOCAL_BEARER_SECRET"
    local_x_api = "LOCAL_X_API_SECRET"
    local_proxy = "LOCAL_PROXY_SECRET"
    headers = {
        "Authorization": f"Bearer {local_bearer}",
        "X-Api-Key": local_x_api,
        "Proxy-Authorization": f"Basic {local_proxy}",
    }
    result = filter_request_headers(headers, "upstream-key")
    serialized = "\n".join(f"{k}: {v}" for k, v in result.items())
    for marker in (local_bearer, local_x_api, local_proxy):
        assert marker not in serialized, (
            f"Local secret marker {marker!r} survived filtering: {serialized}"
        )


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


def test_build_upstream_auth_headers_returns_bearer() -> None:
    headers = build_upstream_auth_headers(protocol="openai", upstream_api_key="x")
    assert headers == {"Authorization": "Bearer x"}


def test_build_upstream_auth_headers_anthropic_protocol() -> None:
    headers = build_upstream_auth_headers(
        protocol="anthropic", upstream_api_key="anthropic-key"
    )
    assert headers == {"Authorization": "Bearer anthropic-key"}
