"""Upstream HTTP client wrapper."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
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

    The OpenCode Go gateway accepts a single ``Authorization: Bearer``
    header for both OpenAI-compatible and Anthropic-compatible payloads.
    Returning exactly one header keeps the contract explicit and
    prevents accidental duplicate ``Authorization`` fields.
    """
    return {"Authorization": f"Bearer {upstream_api_key}"}


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
    filtered: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key in LOCAL_CREDENTIAL_HEADERS:
            continue
        if lower_key in HOP_BY_HOP_HEADERS:
            continue
        if lower_key in ("host", "content-length"):
            continue
        filtered[key] = value

    filtered.update(
        build_upstream_auth_headers(protocol="", upstream_api_key=upstream_api_key)
    )
    return filtered


def filter_response_headers(
    headers: httpx.Headers,
    streaming: bool = False,
) -> dict[str, str]:
    """Filter response headers for downstream.

    - Remove hop-by-hop headers
    - Remove content-length for streaming (chunked transfer)
    - Preserve useful headers
    """
    filtered: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS:
            continue
        if streaming and lower_key == "content-length":
            continue
        filtered[key] = value
    return filtered
