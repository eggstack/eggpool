"""OpenAI-compatible /v1/chat/completions endpoint."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse, StreamingResponse

from go_aggregator.auth import require_auth
from go_aggregator.proxy.client import (
    filter_response_headers,
    forward_to_upstream,
)

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


async def handle_chat_completions(
    request: Request,
) -> JSONResponse | StreamingResponse:
    """Handle POST /v1/chat/completions."""
    await require_auth(request)

    registry = request.app.state.registry
    catalog = request.app.state.catalog
    router = request.app.state.router
    db = request.app.state.db
    client: Any = request.app.state.httpx_client

    # Parse request body
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON"},
        )

    model_id = payload.get("model")
    if not model_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Missing model field"},
        )

    # Check model availability
    if not catalog.is_model_available(model_id):
        return JSONResponse(
            status_code=404,
            content={"error": f"Model {model_id!r} not available"},
        )

    # Select account
    selected = router.select_account(model_id)
    if selected is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No accounts available for this model"},
        )

    api_key = registry.get_api_key(selected.name)
    if not api_key:
        return JSONResponse(
            status_code=503,
            content={"error": "Account API key not available"},
        )

    # Generate proxy request ID
    proxy_request_id = str(uuid.uuid4())
    start_time = time.time()

    # Log request
    logger.info(
        "Proxying chat completion: model=%s account=%s proxy_request_id=%s",
        model_id,
        selected.name,
        proxy_request_id,
    )

    # Forward to upstream
    request_headers = dict(request.headers)
    response = await forward_to_upstream(
        client,
        "POST",
        "/chat/completions",
        api_key,
        request_headers,
        body,
    )

    # Record request in database
    elapsed_ms = int((time.time() - start_time) * 1000)
    status = "completed" if response.status_code < 400 else "error"
    await db.execute(
        """
        INSERT INTO requests (
            account_id, model_id, started_at, completed_at,
            status, input_tokens, output_tokens,
            cost_microdollars, upstream_latency_ms
        ) VALUES (
            (SELECT id FROM accounts WHERE name = ?),
            ?,
            datetime('now'),
            datetime('now'),
            ?,
            0, 0, 0, ?
        )
        """,
        (selected.name, model_id, status, elapsed_ms),
    )
    await db.connection.commit()

    # Filter response headers
    response_headers = filter_response_headers(response.headers)
    response_headers["x-proxy-request-id"] = proxy_request_id

    # Return response
    if payload.get("stream", False):
        # Streaming response - for Phase 4
        return StreamingResponse(
            iter([response.content]),
            status_code=response.status_code,
            headers=response_headers,
            media_type=response.headers.get("content-type", "application/json"),
        )

    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        resp_content = response.json()
    else:
        resp_content = {"raw": response.text}

    return JSONResponse(
        status_code=response.status_code,
        content=resp_content,
        headers=response_headers,
    )
