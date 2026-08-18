"""OpenAI Responses-compatible ``/v1/responses`` endpoint.

Stateless same-protocol passthrough introduced by Plan 143. EggPool does
not implement Responses ↔ Anthropic translation, conversation
persistence, response retrieval, cancellation, background jobs, or
WebSocket transport. Stateful Responses fields
(``previous_response_id``, ``conversation``, ``store = true``,
``background = true``) are rejected locally in
:mod:`eggpool.api.proxy_request` so the client never believes provider
state is being preserved across account failover.

The endpoint shares the OpenAI protocol family with Chat Completions
because the upstream wire format is byte-for-byte passthrough. Only
the ``request_surface`` field distinguishes the dispatch path: the
provider-bound URL resolver picks ``responses_path`` instead of
``openai_path``, the Chat Completions ``stream_options`` transform is
skipped, and the streaming observer recognises Responses terminal
events.
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
