"""Bounded request body reader."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from eggpool.errors import RequestTooLargeError

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
    too_large = False
    stream = request.stream()
    try:
        async for chunk in stream:
            total += len(chunk)
            if total > max_bytes:
                too_large = True
                break
            chunks.append(chunk)
    finally:
        if too_large:
            # Drain the remaining stream so the upstream connection
            # is properly released; otherwise HTTP/1.1 keep-alive
            # connections may stall waiting for the body to finish.
            # Consume from the existing generator — request.stream()
            # is a one-shot async generator that cannot be called
            # again after partial consumption.
            try:
                async with asyncio.timeout(5.0):
                    async for _chunk in stream:
                        pass
            except Exception:
                pass
    if too_large:
        raise RequestTooLargeError(f"Request body exceeds limit of {max_bytes} bytes")
    return b"".join(chunks)
