"""Tests for public status mapping of uncaught aggregator errors."""

from __future__ import annotations

import pytest
from fastapi import Request

from eggpool.app import create_app
from eggpool.errors import (
    AggregatorError,
    AuthenticationError,
    QuotaExhaustedError,
    RateLimitError,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (AuthenticationError("bad credentials"), 503),
        (QuotaExhaustedError("quota exhausted"), 503),
        (RateLimitError("slow down", retry_after=5.0), 429),
    ],
)
async def test_upstream_error_status_mapping(
    error: AggregatorError,
    status_code: int,
) -> None:
    app = create_app()
    handler = app.exception_handlers[AggregatorError]
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "http_version": "1.1",
        }
    )

    response = await handler(request, error)  # type: ignore[operator]

    assert response.status_code == status_code
