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


_EXTRA_DROP_HEADERS = frozenset({"host", "content-length"})


def sanitize_request_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip local credentials, hop-by-hop, and framing headers.

    This does NOT inject upstream auth. Use
    :func:`build_upstream_headers` to compose the final upstream header
    set after sanitization.
    """
    connection_headers = _connection_header_tokens(
        value for key, value in headers.items() if key.casefold() == "connection"
    )
    drop = (
        HOP_BY_HOP_HEADERS
        | LOCAL_CREDENTIAL_HEADERS
        | connection_headers
        | _EXTRA_DROP_HEADERS
    )
    return {key: value for key, value in headers.items() if key.casefold() not in drop}


def build_upstream_headers(
    headers: dict[str, str],
    upstream_api_key: str,
    *,
    extra_drop: frozenset[str] | None = None,
) -> dict[str, str]:
    """Build the final upstream header set in a single pass.

    Combines sanitization (strip credentials, hop-by-hop,
    connection-nominated) with auth header injection.  More efficient
    than calling :func:`sanitize_request_headers` followed by
    :meth:`dict.update`.
    """
    connection_headers = _connection_header_tokens(
        value for key, value in headers.items() if key.casefold() == "connection"
    )
    drop = (
        HOP_BY_HOP_HEADERS
        | LOCAL_CREDENTIAL_HEADERS
        | connection_headers
        | _EXTRA_DROP_HEADERS
    )
    if extra_drop:
        drop = drop | extra_drop
    filtered: dict[str, str] = {
        key: value for key, value in headers.items() if key.casefold() not in drop
    }
    filtered["Authorization"] = f"Bearer {upstream_api_key}"
    return filtered


def filter_request_headers(
    headers: dict[str, str],
    upstream_api_key: str,
) -> dict[str, str]:
    """Filter and transform request headers for upstream.

    - Strip every local credential-bearing header
      (``Authorization``, ``X-Api-Key``, ``Proxy-Authorization``)
      before forwarding. The selected account's credential is then
      injected via :func:`build_upstream_headers`.
    - Remove hop-by-hop headers
    - Remove host and content-length (recalculated by httpx)
    """
    return build_upstream_headers(headers, upstream_api_key)


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
    raw_headers = [
        (raw_name.decode("latin-1"), raw_value.decode("latin-1"))
        for raw_name, raw_value in headers.raw
    ]
    connection_headers = _connection_header_tokens(
        value for name, value in raw_headers if name.casefold() == "connection"
    )
    filtered: list[tuple[str, str]] = []
    for name, value in raw_headers:
        lower_name = name.lower()
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
        filtered.append((name, value))
    return filtered


def _connection_header_tokens(values: Iterable[str]) -> set[str]:
    """Return lower-cased header names nominated by Connection fields."""
    return {
        token.strip().casefold()
        for value in values
        for token in value.split(",")
        if token.strip()
    }
