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
                "code": str(status_code),
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


def openai_capability_error_response(
    status_code: int,
    message: str,
    *,
    capability: str,
    requested_fields: list[str],
    model: str,
) -> JSONResponse:
    """Return an OpenAI-compatible error response with capability detail.

    Capability errors carry debugging context (the requested capability,
    the fields the client used to signal it, and the model id) that
    generic error responses do not.  This renderer emits the full
    schema specified by the Phase 6 plan.
    """
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": "capability_error",
                "code": str(status_code),
                "capability": capability,
                "requested_fields": list(requested_fields),
                "model": model,
            }
        },
    )


def anthropic_capability_error_response(
    status_code: int,
    message: str,
    *,
    capability: str,
    requested_fields: list[str],
    model: str,
) -> JSONResponse:
    """Return an Anthropic-compatible error response with capability detail."""
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": "capability_error",
                "message": message,
                "capability": capability,
                "requested_fields": list(requested_fields),
                "model": model,
            },
        },
    )


__all__ = [
    "anthropic_capability_error_response",
    "anthropic_error_response",
    "openai_capability_error_response",
    "openai_error_response",
]
