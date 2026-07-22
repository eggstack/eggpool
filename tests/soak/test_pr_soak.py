"""Short PR soak test (Workstream D).

A bounded deterministic soak test suitable for normal CI (target: < 3
minutes).  Exercises the core reload/lifecycle/streaming/dispatch paths
in a single test to catch regressions that unit tests miss.
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from eggpool.db.consistency_audit import ConsistencyAuditor
from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator

if TYPE_CHECKING:
    import random

    from eggpool.db.connection import Database

pytestmark = [pytest.mark.soak]

UPSTREAM_BASE = "https://soak-test-upstream.example.com"


# ---------------------------------------------------------------------------
# Mock upstream handlers
# ---------------------------------------------------------------------------


async def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "cmpl-pr",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        },
    )


async def _stream_handler(request: httpx.Request) -> httpx.Response:
    async def _aiter_bytes():  # type: ignore[no-untyped-def]
        yield b"data: "
        yield json.dumps(
            {
                "id": "cmpl-pr",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hi"},
                        "finish_reason": None,
                    }
                ],
            }
        ).encode()
        yield b"\n\n"
        yield b"data: [DONE]\n\n"

    return httpx.Response(
        200,
        stream=_aiter_bytes(),
        headers={"content-type": "text/event-stream"},
    )


async def _rate_limit_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, json={"error": {"message": "rate limited"}})


async def _slow_stream_handler(request: httpx.Request) -> httpx.Response:
    async def _aiter_bytes():  # type: ignore[no-untyped-def]
        for i in range(8):
            yield b"data: "
            yield json.dumps(
                {
                    "id": "cmpl-pr",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"tok-{i}"},
                            "finish_reason": None,
                        }
                    ],
                }
            ).encode()
            yield b"\n\n"
            await asyncio.sleep(0.01)
        yield b"data: [DONE]\n\n"

    return httpx.Response(
        200,
        stream=_aiter_bytes(),
        headers={"content-type": "text/event-stream"},
    )


async def _consume_stream(stream_iter: object) -> None:
    async for _chunk in stream_iter:  # type: ignore[misc]
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_rss_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if os.uname().sysname == "Darwin":
        return usage.ru_maxrss * 1024
    return usage.ru_maxrss


def _get_thread_count() -> int:
    return threading.active_count()


def _make_body(model: str, idx: int, stream: bool = False) -> bytes:
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": f"Msg {idx}"}],
    }
    if stream:
        payload["stream"] = True
    return json.dumps(payload).encode()


async def _count_requests(db: Database) -> dict[str, int]:
    """Count requests by status."""
    rows = await db.fetch_all("SELECT status FROM requests")
    counts: dict[str, int] = {}
    for row in rows:
        s = row["status"]
        counts[s] = counts.get(s, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestPRSoak:
    """Bounded deterministic soak test for CI."""

    @pytest.mark.asyncio
    async def test_pr_soak_deterministic(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
        soak_rng: random.Random,
    ) -> None:
        rng = soak_rng  # seeded at 42

        initial_threads = _get_thread_count()
        rss_start = _get_rss_bytes()
        models = ["gpt-4", "claude-3-sonnet-20240229"]

        # -- Phase 1: Serial requests (20 non-streaming, mixed models) --------
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_ok_handler,
            )
            respx.post(f"{UPSTREAM_BASE}/messages").mock(
                side_effect=_ok_handler,
            )
            for i in range(20):
                model = models[i % 2]
                protocol = "openai" if model == "gpt-4" else "anthropic"
                context = ProxyRequestContext(
                    request_id=f"pr-soak-serial-{i}",
                    protocol=protocol,
                    model_id=model,
                    streaming=False,
                    original_body=_make_body(model, i),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                assert response.status_code == 200

        counts = await _count_requests(soak_db)
        total = sum(counts.values())
        assert total == 20, f"Phase 1: expected 20, got {total} ({counts})"

        # -- Phase 2: Concurrent streaming burst (10 requests) ----------------
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler,
            )
            respx.post(f"{UPSTREAM_BASE}/messages").mock(
                side_effect=_stream_handler,
            )
            tasks = []
            for i in range(10):
                model = models[i % 2]
                protocol = "openai" if model == "gpt-4" else "anthropic"
                context = ProxyRequestContext(
                    request_id=f"pr-soak-burst-{i}",
                    protocol=protocol,
                    model_id=model,
                    streaming=True,
                    original_body=_make_body(model, i, stream=True),
                    incoming_headers={"content-type": "application/json"},
                )
                tasks.append(soak_coordinator.execute(context))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            assert len(errors) == 0, f"Phase 2 errors: {errors}"

            for r in results:
                if not isinstance(r, Exception) and r.stream_iterator is not None:
                    await _consume_stream(r.stream_iterator)

        counts = await _count_requests(soak_db)
        total = sum(counts.values())
        assert total == 30, f"Phase 2: expected 30, got {total} ({counts})"

        # -- Phase 3: Cancellation (5 streaming, fully consumed) --------------
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_slow_stream_handler,
            )
            respx.post(f"{UPSTREAM_BASE}/messages").mock(
                side_effect=_slow_stream_handler,
            )
            for i in range(5):
                context = ProxyRequestContext(
                    request_id=f"pr-soak-cancel-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=True,
                    original_body=_make_body("gpt-4", i, stream=True),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                if response.stream_iterator is not None:
                    await _consume_stream(response.stream_iterator)

        counts = await _count_requests(soak_db)
        total = sum(counts.values())
        assert total == 35, f"Phase 3: expected 35, got {total} ({counts})"

        # Brief settle
        await asyncio.sleep(0.1)

        # -- Phase 4: Mixed load (15 concurrent, streaming + non-streaming) ---
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler,
            )
            respx.post(f"{UPSTREAM_BASE}/messages").mock(
                side_effect=_stream_handler,
            )
            tasks = []
            for i in range(15):
                streaming = bool(rng.random() < 0.5)
                model = rng.choice(models)
                protocol = "openai" if model == "gpt-4" else "anthropic"
                context = ProxyRequestContext(
                    request_id=f"pr-soak-mixed-{i}",
                    protocol=protocol,
                    model_id=model,
                    streaming=streaming,
                    original_body=_make_body(model, i, stream=streaming),
                    incoming_headers={"content-type": "application/json"},
                )
                tasks.append(soak_coordinator.execute(context))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, Exception)]
            assert len(errors) == 0, f"Phase 4 errors: {errors}"

            for r in results:
                if not isinstance(r, Exception) and r.stream_iterator is not None:
                    await _consume_stream(r.stream_iterator)

        # -- Phase 5: Drain ---------------------------------------------------
        await asyncio.sleep(0.2)

        rss_end = _get_rss_bytes()

        counts = await _count_requests(soak_db)
        total = sum(counts.values())
        assert total == 50, f"Phase 5: expected 50, got {total} ({counts})"

        # -- Phase 6: Error handling (5 rate-limited requests) ----------------
        # This phase is last because 429 responses degrade account health,
        # which would prevent subsequent phases from finding eligible accounts.
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_rate_limit_handler,
            )
            respx.post(f"{UPSTREAM_BASE}/messages").mock(
                side_effect=_rate_limit_handler,
            )
            for i in range(5):
                context = ProxyRequestContext(
                    request_id=f"pr-soak-err-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=_make_body("gpt-4", i),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                assert response.status_code in (200, 429, 503)

        # -- Resource plateau checks ------------------------------------------
        active_resv = await soak_db.fetch_all(
            "SELECT * FROM reservations WHERE status = 'active'"
        )
        assert len(active_resv) == 0, (
            f"Found {len(active_resv)} active reservations after drain"
        )

        pending = await soak_db.fetch_all(
            "SELECT * FROM requests WHERE status = 'pending'"
        )
        assert len(pending) == 0, f"Found {len(pending)} pending requests after drain"

        final_threads = _get_thread_count()
        max_allowed_threads = initial_threads + 20
        assert final_threads <= max_allowed_threads, (
            f"Thread count {final_threads} exceeds initial ({initial_threads}) + 20"
        )

        # RSS should not grow unboundedly; allow 50% growth from start
        if rss_start > 0:
            assert rss_end < rss_start * 1.5, (
                f"RSS grew from {rss_start} to {rss_end} ({rss_end / rss_start:.2f}x)"
            )

        # -- Consistency audit ------------------------------------------------
        auditor = ConsistencyAuditor(soak_db)
        result = await auditor.run_full_audit()
        assert result.passed, (
            f"Consistency audit failed with {result.failed_count} violations: "
            + "; ".join(v.description for v in result.violations)
        )
        assert result.checks_run > 0
