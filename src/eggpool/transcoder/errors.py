"""Upstream-error-envelope parser for cross-protocol error normalisation.

Both OpenAI and Anthropic return errors in different shapes.  This module
parses upstream error bodies into a common representation so the
coordinator can render the appropriate client-facing error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class UpstreamErrorEnvelope:
    """Normalised upstream error representation."""

    status_code: int
    error_type: str
    message: str
    upstream_request_id: str | None = None
    raw: dict[str, Any] | None = None


def parse_upstream_error(
    status_code: int,
    body: dict[str, Any],
    *,
    protocol: str,
) -> UpstreamErrorEnvelope:
    """Parse an upstream error body into a normalised envelope.

    Parameters
    ----------
    status_code:
        The HTTP status code from the upstream.
    body:
        The parsed JSON error body.
    protocol:
        ``"openai"`` or ``"anthropic"`` — selects the parsing path.
    """
    if protocol == "anthropic":
        return _parse_anthropic(status_code, body)
    return _parse_openai(status_code, body)


def _parse_openai(
    status_code: int,
    body: dict[str, Any],
) -> UpstreamErrorEnvelope:
    error = body.get("error", {})
    if isinstance(error, str):
        return UpstreamErrorEnvelope(
            status_code=status_code,
            error_type="upstream_error",
            message=error,
            raw=body,
        )
    return UpstreamErrorEnvelope(
        status_code=status_code,
        error_type=error.get("type", "upstream_error"),
        message=error.get("message", "Unknown upstream error"),
        upstream_request_id=body.get("request_id"),
        raw=body,
    )


def _parse_anthropic(
    status_code: int,
    body: dict[str, Any],
) -> UpstreamErrorEnvelope:
    return UpstreamErrorEnvelope(
        status_code=status_code,
        error_type=body.get("type", "upstream_error"),
        message=body.get("error", {}).get("message", "Unknown upstream error")
        if isinstance(body.get("error"), dict)
        else str(body.get("error", "Unknown upstream error")),
        upstream_request_id=body.get("request_id"),
        raw=body,
    )
