"""Tests for proxy header sanitization, forwarding, and upstream injection.

Covers:

- Hop-by-hop header removal
- Credential header removal
- Connection-nominated header removal
- Host / content-length removal
- OpenAI / Anthropic specific headers preserved
- Auth header injection via ``build_upstream_headers``
- Case-insensitive matching
- Empty header dict
- ``extra_drop`` set for ``build_upstream_headers``
- Backward compatibility of ``filter_request_headers``
"""

from __future__ import annotations

from eggpool.proxy.client import (
    HOP_BY_HOP_HEADERS,
    _connection_header_tokens,
    build_upstream_headers,
    filter_request_headers,
    sanitize_request_headers,
)


class TestHopByHopRemoval:
    """All hop-by-hop headers are stripped from forwarded requests."""

    def test_connection_header_removed(self) -> None:
        result = sanitize_request_headers({"Connection": "keep-alive"})
        assert "connection" not in [k.lower() for k in result]

    def test_keep_alive_removed(self) -> None:
        result = sanitize_request_headers({"Keep-Alive": "timeout=5"})
        assert not any(k.lower() == "keep-alive" for k in result)

    def test_proxy_authenticate_removed(self) -> None:
        result = sanitize_request_headers({"Proxy-Authenticate": "Basic"})
        assert not any(k.lower() == "proxy-authenticate" for k in result)

    def test_proxy_authorization_removed(self) -> None:
        result = sanitize_request_headers({"Proxy-Authorization": "Basic abc"})
        assert not any(k.lower() == "proxy-authorization" for k in result)

    def test_te_removed(self) -> None:
        result = sanitize_request_headers({"TE": "trailers"})
        assert not any(k.lower() == "te" for k in result)

    def test_trailer_removed(self) -> None:
        result = sanitize_request_headers({"Trailer": "X-Foo"})
        assert not any(k.lower() == "trailer" for k in result)

    def test_transfer_encoding_removed(self) -> None:
        result = sanitize_request_headers({"Transfer-Encoding": "chunked"})
        assert not any(k.lower() == "transfer-encoding" for k in result)

    def test_upgrade_removed(self) -> None:
        result = sanitize_request_headers({"Upgrade": "websocket"})
        assert not any(k.lower() == "upgrade" for k in result)

    def test_proxy_connection_removed(self) -> None:
        result = sanitize_request_headers({"Proxy-Connection": "keep-alive"})
        assert not any(k.lower() == "proxy-connection" for k in result)

    def test_trailers_removed(self) -> None:
        result = sanitize_request_headers({"Trailers": "X-Checksum"})
        assert not any(k.lower() == "trailers" for k in result)

    def test_all_hop_by_hop_stripped_together(self) -> None:
        headers = {h: "val" for h in HOP_BY_HOP_HEADERS}
        headers["Content-Type"] = "application/json"
        result = sanitize_request_headers(headers)
        assert result == {"Content-Type": "application/json"}


class TestCredentialRemoval:
    """Local credential headers are stripped before forwarding."""

    def test_authorization_removed(self) -> None:
        result = sanitize_request_headers({"Authorization": "Bearer sk-xxx"})
        assert not any(k.lower() == "authorization" for k in result)

    def test_x_api_key_removed(self) -> None:
        result = sanitize_request_headers({"X-Api-Key": "key-123"})
        assert not any(k.lower() == "x-api-key" for k in result)

    def test_proxy_authorization_also_credential(self) -> None:
        result = sanitize_request_headers({"Proxy-Authorization": "Bearer local"})
        assert not any(k.lower() == "proxy-authorization" for k in result)


class TestConnectionNominatedRemoval:
    """Headers named in Connection: are removed."""

    def test_single_connection_token(self) -> None:
        result = sanitize_request_headers(
            {"Connection": "X-Forwarded-For", "X-Forwarded-For": "1.2.3.4"}
        )
        assert "X-Forwarded-For" not in result

    def test_multiple_connection_tokens(self) -> None:
        result = sanitize_request_headers(
            {
                "Connection": "X-Forwarded-For, X-Request-Id",
                "X-Forwarded-For": "1.2.3.4",
                "X-Request-Id": "abc",
            }
        )
        assert "X-Forwarded-For" not in result
        assert "X-Request-Id" not in result

    def test_connection_header_itself_always_dropped(self) -> None:
        result = sanitize_request_headers({"Connection": "X-Custom"})
        assert not any(k.lower() == "connection" for k in result)

    def test_non_nominated_header_preserved(self) -> None:
        result = sanitize_request_headers(
            {"Connection": "X-Forwarded-For", "Accept": "application/json"}
        )
        assert "Accept" in result


class TestHostAndContentLengthRemoval:
    """Host and Content-Length are always stripped."""

    def test_host_removed(self) -> None:
        result = sanitize_request_headers({"Host": "api.openai.com"})
        assert "Host" not in result

    def test_content_length_removed(self) -> None:
        result = sanitize_request_headers({"Content-Length": "1024"})
        assert "Content-Length" not in result

    def test_case_insensitive_removal(self) -> None:
        result = sanitize_request_headers(
            {"host": "example.com", "content-length": "0"}
        )
        assert "host" not in result
        assert "content-length" not in result


class TestPreservedHeaders:
    """Legitimate headers survive sanitization."""

    def test_content_type_preserved(self) -> None:
        result = sanitize_request_headers(
            {"Content-Type": "application/json", "Authorization": "Bearer x"}
        )
        assert "Content-Type" in result
        assert result["Content-Type"] == "application/json"

    def test_accept_preserved(self) -> None:
        result = sanitize_request_headers({"Accept": "text/event-stream"})
        assert result["Accept"] == "text/event-stream"

    def test_openai_org_preserved(self) -> None:
        result = sanitize_request_headers({"OpenAI-Organization": "org-xxx"})
        assert result["OpenAI-Organization"] == "org-xxx"

    def test_anthropic_version_preserved(self) -> None:
        result = sanitize_request_headers({"anthropic-version": "2023-06-01"})
        assert result["anthropic-version"] == "2023-06-01"

    def test_custom_app_headers_preserved(self) -> None:
        headers = {"X-Custom-Trace": "abc", "X-Request-Source": "proxy"}
        result = sanitize_request_headers(headers)
        assert result == headers


class TestBuildUpstreamHeaders:
    """``build_upstream_headers`` combines sanitization + auth injection."""

    def test_auth_injected(self) -> None:
        result = build_upstream_headers({}, "sk-test")
        assert result["Authorization"] == "Bearer sk-test"

    def test_strips_old_authorization_before_injecting(self) -> None:
        result = build_upstream_headers({"Authorization": "Bearer old-key"}, "sk-new")
        assert result["Authorization"] == "Bearer sk-new"

    def test_single_pass_strips_and_injects(self) -> None:
        result = build_upstream_headers(
            {
                "Authorization": "Bearer old",
                "Host": "api.openai.com",
                "Content-Length": "42",
                "Content-Type": "application/json",
                "Connection": "close",
            },
            "sk-key",
        )
        assert result["Authorization"] == "Bearer sk-key"
        assert "Host" not in result
        assert "Content-Length" not in result
        assert "Connection" not in result
        assert result["Content-Type"] == "application/json"

    def test_extra_drop_set(self) -> None:
        result = build_upstream_headers(
            {"X-Trace": "abc", "X-Request-Id": "xyz"},
            "sk-key",
            extra_drop=frozenset({"x-trace"}),
        )
        assert "X-Trace" not in result
        assert result["X-Request-Id"] == "xyz"

    def test_extra_drop_set_empty(self) -> None:
        result = build_upstream_headers(
            {"X-Custom": "val"}, "sk-key", extra_drop=frozenset()
        )
        assert result["X-Custom"] == "val"

    def test_extra_drop_none(self) -> None:
        result = build_upstream_headers({"X-Custom": "val"}, "sk-key", extra_drop=None)
        assert result["X-Custom"] == "val"

    def test_empty_headers(self) -> None:
        result = build_upstream_headers({}, "sk-key")
        assert result == {"Authorization": "Bearer sk-key"}


class TestFilterRequestHeaders:
    """Backward-compatible ``filter_request_headers`` delegates correctly."""

    def test_delegates_to_build_upstream_headers(self) -> None:
        headers = {
            "Authorization": "Bearer old",
            "Content-Type": "application/json",
        }
        result = filter_request_headers(headers, "sk-upstream")
        assert result["Authorization"] == "Bearer sk-upstream"
        assert result["Content-Type"] == "application/json"

    def test_strips_hop_by_hop(self) -> None:
        result = filter_request_headers({"Keep-Alive": "timeout=5"}, "sk-k")
        assert not any(k.lower() == "keep-alive" for k in result)


class TestCaseInsensitivity:
    """Case-insensitive matching is correct across all drop rules."""

    def test_mixed_case_hop_by_hop(self) -> None:
        result = sanitize_request_headers(
            {"TRANSFER-ENCODING": "chunked", "Transfer-Encoding": "gzip"}
        )
        assert not any(k.lower() == "transfer-encoding" for k in result)

    def test_mixed_case_credentials(self) -> None:
        result = sanitize_request_headers(
            {"AUTHORIZATION": "Bearer a", "X-API-KEY": "k"}
        )
        assert not any(k.lower() in ("authorization", "x-api-key") for k in result)

    def test_mixed_case_host(self) -> None:
        result = sanitize_request_headers({"HOST": "x.com"})
        assert not any(k.lower() == "host" for k in result)


class TestEmptyHeaders:
    """Empty header dicts return correctly shaped results."""

    def test_sanitize_empty(self) -> None:
        assert sanitize_request_headers({}) == {}

    def test_build_upstream_empty(self) -> None:
        result = build_upstream_headers({}, "sk-key")
        assert result == {"Authorization": "Bearer sk-key"}


class TestConnectionHeaderTokens:
    """The ``_connection_header_tokens`` helper parses correctly."""

    def test_single_token(self) -> None:
        tokens = _connection_header_tokens(["X-Custom"])
        assert tokens == {"x-custom"}

    def test_multiple_tokens_comma_separated(self) -> None:
        tokens = _connection_header_tokens(["X-A, X-B, X-C"])
        assert tokens == {"x-a", "x-b", "x-c"}

    def test_whitespace_around_tokens(self) -> None:
        tokens = _connection_header_tokens(["  X-A ,  X-B  "])
        assert tokens == {"x-a", "x-b"}

    def test_empty_tokens_ignored(self) -> None:
        tokens = _connection_header_tokens(["X-A,,X-B,"])
        assert tokens == {"x-a", "x-b"}

    def test_multiple_values(self) -> None:
        tokens = _connection_header_tokens(["X-A", "X-B"])
        assert tokens == {"x-a", "x-b"}

    def test_casefold_normalization(self) -> None:
        tokens = _connection_header_tokens(["X-CUSTOM"])
        assert tokens == {"x-custom"}

    def test_empty_input(self) -> None:
        tokens = _connection_header_tokens([])
        assert tokens == set()
