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


def filter_request_headers(
    headers: dict[str, str],
    upstream_api_key: str,
) -> dict[str, str]:
    """Filter and transform request headers for upstream.

    - Remove local Authorization header
    - Insert upstream API key
    - Remove hop-by-hop headers
    """
    filtered: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key == "authorization":
            continue  # Will be replaced
        if lower_key in HOP_BY_HOP_HEADERS:
            continue
        if lower_key in ("host", "content-length"):
            continue  # Recalculate as needed
        filtered[key] = value

    filtered["Authorization"] = f"Bearer {upstream_api_key}"
    return filtered


def filter_response_headers(
    headers: httpx.Headers,
) -> dict[str, str]:
    """Filter response headers for downstream.

    - Remove hop-by-hop headers
    - Preserve useful headers
    """
    filtered: dict[str, str] = {}
    for key, value in headers.items():
        lower_key = key.lower()
        if lower_key in HOP_BY_HOP_HEADERS:
            continue
        filtered[key] = value
    return filtered


async def forward_to_upstream(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    upstream_api_key: str,
    request_headers: dict[str, str],
    body: bytes | None = None,
) -> httpx.Response:
    """Forward a request to the upstream API."""
    headers = filter_request_headers(request_headers, upstream_api_key)

    response = await client.request(
        method=method,
        url=path,
        headers=headers,
        content=body,
        timeout=300.0,
    )
    return response
