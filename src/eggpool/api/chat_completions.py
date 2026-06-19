"""OpenAI-compatible ``/v1/chat/completions`` endpoint."""

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
    request_label="chat completion",
    error_response=openai_error_response,
    not_found_error_type="invalid_request_error",
    service_error_type="server_error",
)


async def handle_chat_completions(
    request: Request,
) -> Response:
    """Handle POST /v1/chat/completions."""
    return await handle_proxy_request(request, _ENDPOINT)
