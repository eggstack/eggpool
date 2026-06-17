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
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer"):
        # Strip the scheme token (handles "Bearer", "bearer", "Bearer foo",
        # "bearer\tfoo", etc.) without relying on str.removeprefix
        # semantics that would treat "Bearerfoo" as a valid scheme.
        provided = authorization[len("bearer") :].lstrip()
    else:
        provided = ""
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
            f"Set the environment variable or disable authentication by removing "
            f"api_key_env from configuration."
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
