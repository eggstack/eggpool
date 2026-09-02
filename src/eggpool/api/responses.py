"""Stateless OpenAI Responses-compatible ``/v1/responses`` endpoint.

The handler preserves the Responses client grammar while the coordinator may
adapt through the canonical wire boundary to a selected provider surface.
Stateful fields (``previous_response_id``, ``conversation``, ``store = true``,
and ``background = true``) are rejected locally in
:mod:`eggpool.api.proxy_request`; no response identity or conversation state
is persisted across account or wire-surface changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TCH002 — FastAPI needs runtime access

from eggpool.api.errors import openai_error_response
from eggpool.api.proxy_request import (
    ProxyEndpointConfig,
    handle_proxy_request,
)

if TYPE_CHECKING:
    from fastapi.responses import Response

_ENDPOINT = ProxyEndpointConfig(
    protocol="openai",
    request_surface="responses",
    request_label="responses request",
    error_response=openai_error_response,
    not_found_error_type="invalid_request_error",
    service_error_type="server_error",
)


async def handle_responses(request: Request) -> Response:
    """Handle POST /v1/responses."""
    return await handle_proxy_request(request, _ENDPOINT)
