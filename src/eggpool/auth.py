"""Authentication middleware for the aggregator."""

from __future__ import annotations

import hmac
import logging
import re
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig

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
        provided = request.headers.get("x-api-key", "").strip()
    if not api_key:
        return False
    return hmac.compare_digest(provided, api_key)


def require_auth_at_startup(api_key: str | None) -> str | None:
    """Validate that the configured API key is set.

    Returns the API key value if set, None if auth is disabled (no key).
    Raises RuntimeError if auth is enabled but the key is not set or is a
    placeholder value.
    """
    if not api_key:
        return None
    expected = api_key.strip()
    if not expected:
        raise RuntimeError(
            "Authentication enabled but API key is not set. "
            "Set api_key in the [server] config section or disable "
            "authentication by removing it."
        )
    _placeholder_keys = frozenset(
        {
            "your-proxy-api-key",
            "your-opencode-go-key-1",
            "your-opencode-go-key-2",
            "your-api-key-here",
            "your-local-api-key-here",
        }
    )
    if expected.lower() in _placeholder_keys:
        raise RuntimeError(
            "API key contains a placeholder value. "
            "Set a real key before starting the service."
        )
    return expected


async def require_auth(request: Request) -> None:
    """FastAPI dependency that enforces API key authentication.

    Raises:
        HTTPException: If the API key is missing or invalid.
    """
    config: AppConfig = request.app.state.config
    expected = config.server.resolved_api_key
    if not expected:
        return

    stripped = expected.strip()
    if not stripped:
        raise HTTPException(
            status_code=401,
            detail="Authentication unavailable: API key not configured",
        )
    if not verify_api_key(request, stripped):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
