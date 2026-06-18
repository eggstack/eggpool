"""Authentication middleware for the aggregator."""

from __future__ import annotations

import hmac
import logging
import os
import re

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_BEARER_RE = re.compile(r"^bearer[ \t]+(.+)$", re.IGNORECASE)


def verify_api_key(request: Request, api_key: str) -> bool:
    """Verify the API key using constant-time comparison.

    Args:
        request: The incoming FastAPI request.
        api_key: The expected API key value.

    Returns:
        True if the keys match, False otherwise.
    """
    authorization = request.headers.get("authorization", "").strip()
    match = _BEARER_RE.match(authorization)
    provided = match.group(1).strip() if match is not None else ""
    if not provided:
        provided = request.headers.get("x-api-key", "")
    return hmac.compare_digest(provided, api_key)


def require_auth_at_startup(api_key_env: str | None) -> str | None:
    """Validate that the configured API key environment variable is set.

    Returns the API key value if set, None if auth is disabled (no env var name).
    Raises RuntimeError if auth is enabled but the env var is not set.
    """
    if not api_key_env:
        return None
    expected = os.environ.get(api_key_env)
    if not expected:
        raise RuntimeError(
            f"Authentication enabled but API key env var {api_key_env!r} is not set. "
            f"Set the environment variable or disable authentication by setting "
            f'api_key_env = "" in the [server] config section.'
        )
    return expected


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
        # Startup check should have caught this, but if env var
        # disappears at runtime, fail closed
        raise HTTPException(
            status_code=401,
            detail="Authentication unavailable: API key not configured",
        )

    if not verify_api_key(request, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
