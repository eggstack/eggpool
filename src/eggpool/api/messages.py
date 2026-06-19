"""Anthropic-compatible ``/v1/messages`` endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access

from eggpool.api.errors import anthropic_error_response
from eggpool.api.proxy_request import (
    ProxyEndpointConfig,
    handle_proxy_request,
)

if TYPE_CHECKING:
    from fastapi.responses import Response

_ENDPOINT = ProxyEndpointConfig(
    protocol="anthropic",
    request_label="messages request",
    error_response=anthropic_error_response,
    not_found_error_type="not_found_error",
    service_error_type="api_error",
)


async def handle_messages(
    request: Request,
) -> Response:
    """Handle POST /v1/messages."""
    return await handle_proxy_request(request, _ENDPOINT)
