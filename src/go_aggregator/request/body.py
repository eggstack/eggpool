"""Bounded request body reader."""

from __future__ import annotations

from typing import TYPE_CHECKING

from go_aggregator.errors import RequestTooLargeError

if TYPE_CHECKING:
    from starlette.requests import Request


async def read_body_limited(request: Request, max_bytes: int) -> bytes:
    """Read request body with bounded memory usage.

    Checks Content-Length upfront, then streams chunks up to the limit.
    Raises RequestTooLargeError if the body exceeds max_bytes.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise RequestTooLargeError(
                    f"Request body ({content_length} bytes) exceeds "
                    f"limit of {max_bytes} bytes"
                )
        except ValueError:
            pass  # Invalid content-length, fall through to streaming

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise RequestTooLargeError(
                f"Request body exceeds limit of {max_bytes} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)
