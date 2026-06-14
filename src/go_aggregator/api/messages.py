"""Anthropic-compatible /v1/messages endpoint."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse, StreamingResponse

from go_aggregator.api.errors import anthropic_error_response
from go_aggregator.auth import require_auth
from go_aggregator.errors import (
    CatalogUnavailableError,
    ModelNotFoundError,
    NoEligibleAccountError,
    UpstreamExhaustedError,
)
from go_aggregator.request.coordinator import (
    PreparedProxyResponse,
    ProxyRequestContext,
    RequestCoordinator,
)

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


async def handle_messages(
    request: Request,
) -> JSONResponse | StreamingResponse:
    """Handle POST /v1/messages."""
    await require_auth(request)

    coordinator: RequestCoordinator = request.app.state.coordinator

    # Enforce body size limit
    body = await request.body()
    from go_aggregator.constants import MAX_REQUEST_BODY_BYTES

    if len(body) > MAX_REQUEST_BODY_BYTES:
        return anthropic_error_response(
            status_code=413,
            message="Request body too large",
            error_type="invalid_request_error",
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return anthropic_error_response(
            status_code=400,
            message="Invalid JSON",
            error_type="invalid_request_error",
        )

    model_id = payload.get("model")
    if not model_id:
        return anthropic_error_response(
            status_code=400,
            message="Missing model field",
            error_type="invalid_request_error",
        )

    is_stream = payload.get("stream", False)

    context = ProxyRequestContext(
        request_id=str(uuid.uuid4()),
        protocol="anthropic",
        model_id=model_id,
        streaming=is_stream,
        original_body=body,
        incoming_headers=dict(request.headers),
        started_at=time.time(),
    )

    logger.info(
        "Proxying messages request: model=%s proxy_request_id=%s streaming=%s",
        model_id,
        context.request_id,
        is_stream,
    )

    try:
        result = await coordinator.execute(context)
    except ModelNotFoundError as exc:
        return anthropic_error_response(
            status_code=404,
            message=str(exc),
            error_type="not_found_error",
        )
    except NoEligibleAccountError as exc:
        return anthropic_error_response(
            status_code=503,
            message=str(exc),
            error_type="api_error",
        )
    except CatalogUnavailableError as exc:
        return anthropic_error_response(
            status_code=503,
            message=str(exc),
            error_type="api_error",
        )
    except UpstreamExhaustedError as exc:
        return anthropic_error_response(
            status_code=502,
            message=str(exc),
            error_type="api_error",
        )

    return _render_response(result)


def _render_response(
    result: PreparedProxyResponse,
) -> JSONResponse | StreamingResponse:
    """Render a PreparedProxyResponse as a FastAPI response."""
    if result.stream_iterator is not None:

        async def _stream_gen():  # type: ignore[no-untyped-def]
            async for chunk in result.stream_iterator:
                yield chunk

        return StreamingResponse(
            _stream_gen(),
            status_code=result.status_code,
            headers=result.headers,
            media_type="text/event-stream",
        )

    return JSONResponse(
        status_code=result.status_code,
        content=json.loads(result.body) if result.body else {},
        headers=result.headers,
    )
