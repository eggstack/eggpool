"""Shared primitives for the high-concurrency stream stability harness.

Canonical scenario names, cancellation offsets, SSE helpers, and the
scenario→respx-response builder used by both the integration test and
the CLI reproducer script.  This module must NOT depend on pytest or
any test fixtures.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

UPSTREAM_BASE = "https://test-upstream.example.com"

# -- scenario names -------------------------------------------------------

SCENARIO_HAPPY_PATH = "happy-path"
SCENARIO_NO_USAGE = "no-usage"
SCENARIO_SLOW_FIRST_BYTE = "slow-first-byte"
SCENARIO_SLOW_STREAM = "slow-stream"
SCENARIO_ABRUPT_CLOSE = "abrupt-upstream-close"
SCENARIO_SERVER_STALL = "read-timeout"
SCENARIO_MALFORMED_FRAME = "malformed-frame"
SCENARIO_CONNECTION_RESET = "connection-reset"

SCENARIO_ALIASES: dict[str, str] = {
    "slow-token-cadence": "slow-stream",
}

ALL_SCENARIOS: tuple[str, ...] = (
    SCENARIO_HAPPY_PATH,
    SCENARIO_NO_USAGE,
    SCENARIO_SLOW_FIRST_BYTE,
    SCENARIO_SLOW_STREAM,
    SCENARIO_ABRUPT_CLOSE,
    SCENARIO_SERVER_STALL,
    SCENARIO_MALFORMED_FRAME,
    SCENARIO_CONNECTION_RESET,
)


def normalize_scenario(name: str) -> str:
    """Map *name* to its canonical form, resolving any known alias."""
    return SCENARIO_ALIASES.get(name, name)


# -- cancellation offsets -------------------------------------------------

CANCEL_BEFORE_FIRST_BYTE = "before-first-byte"
CANCEL_AFTER_FIRST_TOKEN = "after-first-token"
CANCEL_MIDSTREAM = "midstream"
CANCEL_AFTER_FINAL_BEFORE_USAGE = "after-final-before-usage"

ALL_CANCEL_OFFSETS: tuple[str, ...] = (
    CANCEL_BEFORE_FIRST_BYTE,
    CANCEL_AFTER_FIRST_TOKEN,
    CANCEL_MIDSTREAM,
    CANCEL_AFTER_FINAL_BEFORE_USAGE,
)

# -- SSE helpers ----------------------------------------------------------


def sse_chunk(delta: str, *, finish: bool = False) -> bytes:
    payload = {
        "id": f"chunk-{delta}",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "delta": {"content": delta},
                "index": 0,
                "finish_reason": "stop" if finish else None,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def sse_usage_chunk(
    *,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> bytes:
    payload = {
        "id": "usage",
        "object": "chat.completion.chunk",
        "choices": [],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


# -- cancellation logic ---------------------------------------------------


def should_cancel(offset: str, chunks_seen: int, started: bool) -> bool:
    """Pick the cancellation condition for *offset*."""
    if not started:
        return False
    if offset == CANCEL_BEFORE_FIRST_BYTE:
        return chunks_seen == 0
    if offset == CANCEL_AFTER_FIRST_TOKEN:
        return chunks_seen >= 1
    if offset == CANCEL_MIDSTREAM:
        return chunks_seen >= 2
    if offset == CANCEL_AFTER_FINAL_BEFORE_USAGE:
        return chunks_seen >= 4
    return chunks_seen >= 2


# -- arithmetic helper ----------------------------------------------------


def positive_delta(baseline: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    keys = set(baseline) | set(current)
    return {
        key: current.get(key, 0) - baseline.get(key, 0)
        for key in keys
        if current.get(key, 0) - baseline.get(key, 0) > 0
    }


# -- scenario → respx response builder -----------------------------------


def scenario_respx_response(
    scenario: str,
    *,
    chunks_per_stream: int,
    chunk_delay_s: float,
) -> httpx.Response:
    """Build a ``respx``-compatible ``httpx.Response`` for *scenario*.

    Each scenario models a distinct upstream failure mode:

    - happy-path: standard SSE with final usage frame and [DONE]
    - no-usage: standard SSE but no final usage frame
    - slow-first-byte: long delay before the first chunk
    - slow-stream: long delay between chunks
    - abrupt-upstream-close: closes after N chunks without usage
    - read-timeout: hangs past the read timeout
    - malformed-frame: emits a garbage SSE frame partway through
    - connection-reset: closes the stream with a transport error
    """
    canonical = normalize_scenario(scenario)

    if canonical == SCENARIO_HAPPY_PATH:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)
            yield sse_usage_chunk()
            yield sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if canonical == SCENARIO_NO_USAGE:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)
            yield sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if canonical == SCENARIO_SLOW_FIRST_BYTE:

        async def _gen() -> Any:
            await asyncio.sleep(max(1.0, chunk_delay_s * 100))
            yield sse_chunk("first")
            for i in range(1, chunks_per_stream):
                yield sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)
            yield sse_usage_chunk()
            yield sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if canonical == SCENARIO_SLOW_STREAM:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield sse_chunk(f"tok{i}")
                await asyncio.sleep(max(0.5, chunk_delay_s * 50))
            yield sse_usage_chunk()
            yield sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if canonical == SCENARIO_ABRUPT_CLOSE:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if canonical == SCENARIO_SERVER_STALL:

        async def _gen() -> Any:
            await asyncio.sleep(60.0)
            yield b""  # pragma: no cover - never reached within test window

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if canonical == SCENARIO_MALFORMED_FRAME:

        async def _gen() -> Any:
            for i in range(chunks_per_stream):
                yield sse_chunk(f"tok{i}")
                await asyncio.sleep(chunk_delay_s)
            yield b"this is not a valid SSE frame at all\n\n"
            await asyncio.sleep(chunk_delay_s)
            yield sse_done()

        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_gen(),
        )

    if canonical == SCENARIO_CONNECTION_RESET:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
        )

    msg = f"Unknown scenario: {scenario}"
    raise ValueError(msg)
