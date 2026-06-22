"""Upstream HTTP client wrapper."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    import httpx

logger = logging.getLogger(__name__)

HOP_BY_HOP_HEADERS = frozenset(
    {
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
)

LOCAL_CREDENTIAL_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "proxy-authorization",
    }
)


def build_upstream_auth_headers(
    protocol: str,
    upstream_api_key: str,
) -> dict[str, str]:
    """Build the upstream authentication header set.

    .. deprecated::
        Use :func:`eggpool.providers.contract.build_auth_headers` instead.
        This wrapper exists for backwards compatibility only.
    """
    return {"Authorization": f"Bearer {upstream_api_key}"}


def sanitize_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip local credentials, hop-by-hop, and framing headers.

    This does NOT inject upstream auth. Use
    :func:`eggpool.providers.contract.build_upstream_headers` to compose
    the final upstream header set after sanitization.
    """
    connection_headers = _connection_header_tokens(
        value for key, value in headers.items() if key.casefold() == "connection"
    )
    filtered: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key in LOCAL_CREDENTIAL_HEADERS:
            continue
        if lower_key in HOP_BY_HOP_HEADERS:
            continue
        if lower_key in connection_headers:
            continue
        if lower_key in ("host", "content-length"):
            continue
        filtered[key] = value
    return filtered


def filter_request_headers(
    headers: dict[str, str],
    upstream_api_key: str,
) -> dict[str, str]:
    """Filter and transform request headers for upstream.

    - Strip every local credential-bearing header
      (``Authorization``, ``X-Api-Key``, ``Proxy-Authorization``)
      before forwarding. The selected account's credential is then
      injected via :func:`build_upstream_auth_headers`.
    - Remove hop-by-hop headers
    - Remove host and content-length (recalculated by httpx)
    """
    filtered = sanitize_request_headers(headers)
    filtered.update(
        build_upstream_auth_headers(protocol="", upstream_api_key=upstream_api_key)
    )
    return filtered


def filter_response_headers(
    headers: httpx.Headers,
    streaming: bool = False,
) -> list[tuple[str, str]]:
    """Filter response headers for downstream.

    - Remove hop-by-hop headers
    - Always remove content-encoding (HTTPX decodes the body)
    - Always remove content-length (Starlette recomputes for non-streaming;
      chunked transfer for streaming)
    - Preserve useful headers
    - Preserve duplicate headers (e.g. multiple Set-Cookie) as separate entries
    """
    connection_headers = _connection_header_tokens(
        raw_value.decode("latin-1")
        for raw_name, raw_value in headers.raw
        if raw_name.decode("latin-1").casefold() == "connection"
    )
    filtered: list[tuple[str, str]] = []
    for raw_name, raw_value in headers.raw:
        lower_name = raw_name.decode("latin-1").lower()
        if lower_name in HOP_BY_HOP_HEADERS:
            continue
        if lower_name in connection_headers:
            continue
        if lower_name in ("content-encoding", "content-length"):
            # HTTPX decodes compressed bodies for .content and
            # .aiter_bytes(); forwarding the original encoding header
            # would mislabel the decoded bytes for downstream clients.
            # Starlette computes Content-Length for non-streaming
            # responses; streaming uses chunked transfer.
            continue
        filtered.append((raw_name.decode("latin-1"), raw_value.decode("latin-1")))
    return filtered


def _connection_header_tokens(values: Iterable[str]) -> set[str]:
    """Return lower-cased header names nominated by Connection fields."""
    return {
        token.strip().casefold()
        for value in values
        for token in value.split(",")
        if token.strip()
    }
