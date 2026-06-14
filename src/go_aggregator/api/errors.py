"""Protocol-specific error response renderers.

Provides OpenAI-style and Anthropic-style error response formats
so that upstream clients receive protocol-compatible error payloads.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse


def openai_error_response(
    status_code: int,
    message: str,
    error_type: str = "invalid_request_error",
) -> JSONResponse:
    """Return an OpenAI-compatible error response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": status_code,
            }
        },
    )


def anthropic_error_response(
    status_code: int,
    message: str,
    error_type: str = "invalid_request_error",
) -> JSONResponse:
    """Return an Anthropic-compatible error response."""
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            },
        },
    )


__all__ = [
    "anthropic_error_response",
    "openai_error_response",
]
