"""Authentication middleware for the aggregator."""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


def verify_api_key(request: Request, api_key: str) -> bool:
    """Verify the API key using constant-time comparison.

    Args:
        request: The incoming FastAPI request.
        api_key: The expected API key value.

    Returns:
        True if the keys match, False otherwise.
    """
    provided = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not provided:
        provided = request.headers.get("x-api-key", "")
    return hmac.compare_digest(provided, api_key)


async def require_auth(request: Request) -> None:
    """FastAPI dependency that enforces API key authentication.

    Raises:
        HTTPException: If the API key is missing or invalid.
    """
    api_key_env: str | None = request.app.state.config.server.api_key_env
    if not api_key_env:
        return

    expected = os.environ.get(api_key_env)
    if not expected:
        logger.warning("API key env var %s not set; auth disabled", api_key_env)
        return

    if not verify_api_key(request, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
