"""Anthropic-compatible /v1/messages endpoint."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

from go_aggregator.auth import require_auth
from go_aggregator.proxy.client import (
    filter_request_headers,
    filter_response_headers,
)
from go_aggregator.proxy.streaming import relay_streaming_response

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


async def handle_messages(
    request: Request,
) -> JSONResponse | StreamingResponse:
    """Handle POST /v1/messages."""
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
        "Proxying messages request: model=%s account=%s proxy_request_id=%s",
        model_id,
        selected.name,
        proxy_request_id,
    )

    # Forward to upstream
    request_headers = dict(request.headers)
    filtered_headers = filter_request_headers(request_headers, api_key)

    is_stream = payload.get("stream", False)

    try:
        # For streaming, we need to stream the response
        if is_stream:
            upstream_response = client.stream(
                "POST",
                "/messages",
                headers=filtered_headers,
                content=body,
                timeout=300.0,
            )
        else:
            upstream_response = await client.request(
                "POST",
                "/messages",
                headers=filtered_headers,
                content=body,
                timeout=300.0,
            )
    except httpx.ConnectError:
        logger.exception("Connection error to upstream")
        return JSONResponse(
            status_code=502,
            content={"error": "Upstream connection failed"},
        )
    except httpx.TimeoutException:
        logger.exception("Timeout connecting to upstream")
        return JSONResponse(
            status_code=504,
            content={"error": "Upstream timeout"},
        )

    # Handle streaming response
    if is_stream:

        async def stream_generator():
            """Generate streaming response with usage tracking."""
            async with upstream_response as response:
                # Filter response headers
                response_headers = filter_response_headers(response.headers)
                response_headers["x-proxy-request-id"] = proxy_request_id

                # Stream the response
                first_chunk = True
                final_metrics = None
                async for chunk, metrics in relay_streaming_response(
                    response, "anthropic", proxy_request_id
                ):
                    if first_chunk:
                        first_chunk = False
                    final_metrics = metrics
                    yield f"{chunk}\n"

                # Record request in database after streaming completes
                elapsed_ms = int((time.time() - start_time) * 1000)
                has_usage = (
                    final_metrics is not None and final_metrics.usage is not None
                )
                input_tokens = final_metrics.usage.input_tokens if has_usage else 0
                output_tokens = final_metrics.usage.output_tokens if has_usage else 0

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
                        'completed',
                        ?, ?, 0, ?
                    )
                    """,
                    (selected.name, model_id, input_tokens, output_tokens, elapsed_ms),
                )
                await db.connection.commit()

        return StreamingResponse(
            stream_generator(),
            status_code=200,
            headers={"x-proxy-request-id": proxy_request_id},
            media_type="text/event-stream",
        )

    # Handle non-streaming response
    response = upstream_response
    elapsed_ms = int((time.time() - start_time) * 1000)
    status = "completed" if response.status_code < 400 else "error"

    # Filter response headers
    response_headers = filter_response_headers(response.headers)
    response_headers["x-proxy-request-id"] = proxy_request_id

    # Extract usage from response
    input_tokens = 0
    output_tokens = 0
    try:
        resp_json = response.json()
        usage = resp_json.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
    except (json.JSONDecodeError, ValueError):
        pass

    # Record request in database
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
            ?, ?, 0, ?
        )
        """,
        (selected.name, model_id, status, input_tokens, output_tokens, elapsed_ms),
    )
    await db.connection.commit()

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
