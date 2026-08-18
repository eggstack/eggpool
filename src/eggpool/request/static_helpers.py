"""Pure static and timing helpers extracted from RequestCoordinator."""

from __future__ import annotations

import logging
import time
import typing

from eggpool.errors import (
    AuthenticationError,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitError,
    RequestTooLargeError,
    UpstreamError,
)

if typing.TYPE_CHECKING:
    import httpx

    from eggpool.request.coordinator import (
        PreparedProxyResponse,
        ProxyRequestContext,
    )

logger = logging.getLogger(__name__)


def get_header_value(
    headers: list[tuple[str, str]],
    name: str | list[str],
) -> str | None:
    """Return the value for a header, or None.

    Accepts a single name or a list of names tried in order
    (case-insensitive).
    """
    names = [name] if isinstance(name, str) else name
    lower_names = [n.lower() for n in names]
    for key, value in headers:
        if key.lower() in lower_names:
            return value
    return None


def elapsed_ms(context: ProxyRequestContext) -> int:
    """Return request latency from a clock unaffected by wall-clock jumps."""
    return max(0, int((time.monotonic() - context.started_monotonic) * 1000))


def upstream_read_ms(
    context: ProxyRequestContext,
    observed_elapsed_ms: int,
) -> int | None:
    """Return elapsed upstream body/stream read time after response headers."""
    if context.upstream_headers_ms is None:
        return None
    return max(0, observed_elapsed_ms - context.upstream_headers_ms)


def upstream_header_ms(context: ProxyRequestContext) -> int | None:
    """Return elapsed time to receive upstream response headers.

    Returns ``None`` when the upstream response had not finished
    receiving headers when the context was last updated; the
    coordinator uses this only for diagnostic instrumentation.
    """
    headers_ms = getattr(context, "upstream_headers_ms", None)
    if headers_ms is None:
        return None
    return max(0, int(headers_ms))


def coordinator_overhead_ms(
    *,
    total_ms: int,
    connect_ms: int | None,
    read_ms: int | None,
) -> int | None:
    """Return elapsed time not attributed to upstream connect or read phases."""
    if connect_ms is None or read_ms is None:
        return None
    return max(0, total_ms - connect_ms - read_ms)


def error_status_code(err: Exception | None) -> int:
    """Map an exception to an HTTP status code."""
    if err is None:
        return 500
    if isinstance(err, AuthenticationError):
        return 502
    if isinstance(err, RateLimitError):
        return 429
    if isinstance(err, QuotaExhaustedError):
        return 503
    if isinstance(err, ModelUnavailableError):
        return 503
    if isinstance(err, RequestTooLargeError):
        return 413
    if isinstance(err, UpstreamError) and err.status_code is not None:
        return err.status_code
    # Check for coordinator-internal error types by class name to avoid
    # importing private types across module boundaries.
    err_class_name = type(err).__name__
    if err_class_name == "_RetryableUpstreamError":
        status_code = getattr(err, "status_code", None)
        if status_code is not None:
            return status_code
        return 502
    if err_class_name == "_NonRetryableUpstreamError":
        status_code = getattr(err, "status_code", None)
        if status_code is not None:
            return status_code
        return 502
    if err_class_name == "_LocalDispatchError":
        return 500
    return 502


def build_local_error_response(
    context: ProxyRequestContext,
    *,
    status_code: int,
) -> PreparedProxyResponse:
    """Build a bounded protocol-shaped local error without exception text."""
    from eggpool.request.body import encode_json_body
    from eggpool.request.coordinator import PreparedProxyResponse

    if context.protocol == "anthropic":
        body = encode_json_body(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": "Internal proxy error",
                },
            }
        )
    else:
        body = encode_json_body(
            {
                "error": {
                    "message": "Internal proxy error",
                    "type": "server_error",
                    "code": status_code,
                }
            }
        )
    return PreparedProxyResponse(
        status_code=status_code,
        headers=[
            ("content-type", "application/json"),
            ("x-proxy-request-id", context.request_id),
        ],
        body=body,
        request_id=context.request_id,
        account_name=str(context.client_metadata.get("account_name", "")),
        latency_ms=0,
        attempt_count=0,
    )


async def close_response(response: httpx.Response | None) -> None:
    """Close an upstream response without masking the original failure."""
    if response is None:
        return
    try:
        await response.aclose()
    except Exception:
        logger.debug("Error closing upstream response", exc_info=True)
