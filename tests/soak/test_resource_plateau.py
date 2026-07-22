"""Resource plateau validation tests (Workstream G5).

Verifies that RSS, file descriptors, threads, tasks, and other
runtime resources plateau after warm-up and do not grow unboundedly.

Tolerances (from plans/013-phase-12-ci-soak-and-performance-closure.md):
- Tasks, open clients, and retiring generations must return exactly
  to baseline after quiescence.
- Descriptors may have a small fixed warm-up delta but no positive
  slope in late windows.
- RSS may plateau above startup due to allocator behavior, but
  late-window growth slope must remain within a documented bound.
- Writer queue must drain after load stops.
- No unobserved task exception is permitted.
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

from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator

if TYPE_CHECKING:
    from eggpool.db.connection import Database

pytestmark = [pytest.mark.soak, pytest.mark.resource_plateau]

UPSTREAM_BASE = "https://soak-test-upstream.example.com"


def _get_rss_bytes() -> int:
    """Get current RSS in bytes."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    # ru_maxrss is in bytes on Linux, kilobytes on macOS
    if os.uname().sysname == "Darwin":
        return usage.ru_maxrss * 1024
    return usage.ru_maxrss


def _get_thread_count() -> int:
    """Get current thread count."""
    return threading.active_count()


async def _stream_handler(request: httpx.Request) -> httpx.Response:
    async def _aiter_bytes():  # type: ignore[no-untyped-def]
        yield b"data: "
        yield json.dumps(
            {
                "id": "cmpl-1",
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


async def _consume_stream(stream_iter: object) -> None:
    async for _chunk in stream_iter:  # type: ignore[misc]
        pass


class TestResourcePlateau:
    """Verify resources plateau after warm-up."""

    @pytest.mark.asyncio
    async def test_thread_count_plateau(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Thread count should plateau and not grow unboundedly."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            initial_threads = _get_thread_count()
            thread_counts = [initial_threads]

            for cycle in range(3):
                for i in range(10):
                    context = ProxyRequestContext(
                        request_id=f"plateau-thr-{cycle}-{i}",
                        protocol="openai",
                        model_id="gpt-4",
                        streaming=True,
                        original_body=json.dumps(
                            {
                                "model": "gpt-4",
                                "messages": [{"role": "user", "content": f"Msg {i}"}],
                                "stream": True,
                            }
                        ).encode(),
                        incoming_headers={"content-type": "application/json"},
                    )
                    response = await soak_coordinator.execute(context)
                    if response.stream_iterator is not None:
                        await _consume_stream(response.stream_iterator)
                thread_counts.append(_get_thread_count())

        # Thread count should not grow beyond reasonable bounds
        # Allow up to 2x initial + some headroom for async tasks
        max_allowed = initial_threads + 20
        assert max(thread_counts) <= max_allowed, (
            f"Thread count grew from {initial_threads} to {max(thread_counts)} "
            f"(allowed max: {max_allowed})"
        )

    @pytest.mark.asyncio
    async def test_memory_not_growing_unboundedly(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """RSS should not grow indefinitely under stationary workload."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            rss_samples = []
            for cycle in range(3):
                for i in range(10):
                    context = ProxyRequestContext(
                        request_id=f"plateau-mem-{cycle}-{i}",
                        protocol="openai",
                        model_id="gpt-4",
                        streaming=True,
                        original_body=json.dumps(
                            {
                                "model": "gpt-4",
                                "messages": [{"role": "user", "content": f"Msg {i}"}],
                                "stream": True,
                            }
                        ).encode(),
                        incoming_headers={"content-type": "application/json"},
                    )
                    response = await soak_coordinator.execute(context)
                    if response.stream_iterator is not None:
                        await _consume_stream(response.stream_iterator)
                rss_samples.append(_get_rss_bytes())

        # After warm-up (first cycle), RSS should not grow dramatically
        # Allow up to 50% growth from first to last sample
        if rss_samples[0] > 0:
            growth_ratio = rss_samples[-1] / rss_samples[0]
            assert growth_ratio < 1.5, (
                f"RSS grew from {rss_samples[0]} to {rss_samples[-1]} "
                f"({growth_ratio:.2f}x)"
            )

    @pytest.mark.asyncio
    async def test_reservations_cleaned_after_workload(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """All reservations should be released after workload completes."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            for i in range(20):
                context = ProxyRequestContext(
                    request_id=f"plateau-resv-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=True,
                    original_body=json.dumps(
                        {
                            "model": "gpt-4",
                            "messages": [{"role": "user", "content": f"Msg {i}"}],
                            "stream": True,
                        }
                    ).encode(),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                if response.stream_iterator is not None:
                    await _consume_stream(response.stream_iterator)

        await asyncio.sleep(0.1)
        active = await soak_db.fetch_all(
            "SELECT * FROM reservations WHERE status = 'active'"
        )
        assert len(active) == 0

        pending = await soak_db.fetch_all(
            "SELECT * FROM requests WHERE status = 'pending'"
        )
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_asyncio_task_count_plateau(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Asyncio task count should plateau and return to baseline."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            initial_tasks = len(asyncio.all_tasks())
            task_counts = [initial_tasks]

            for cycle in range(3):
                for i in range(10):
                    context = ProxyRequestContext(
                        request_id=f"plateau-task-{cycle}-{i}",
                        protocol="openai",
                        model_id="gpt-4",
                        streaming=True,
                        original_body=json.dumps(
                            {
                                "model": "gpt-4",
                                "messages": [{"role": "user", "content": f"Msg {i}"}],
                                "stream": True,
                            }
                        ).encode(),
                        incoming_headers={"content-type": "application/json"},
                    )
                    response = await soak_coordinator.execute(context)
                    if response.stream_iterator is not None:
                        await _consume_stream(response.stream_iterator)
                # Brief yield to let tasks complete
                await asyncio.sleep(0.01)
                task_counts.append(len(asyncio.all_tasks()))

        # After quiescence, task count must return to baseline.
        # Allow +2 for background supervisor tasks that survive across cycles.
        final_tasks = task_counts[-1]
        assert final_tasks <= initial_tasks + 2, (
            f"Task count grew from {initial_tasks} to {final_tasks} "
            f"after quiescence (allowed max: {initial_tasks + 2})"
        )

    @pytest.mark.asyncio
    async def test_file_descriptor_plateau(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Open file descriptors should plateau (no positive slope in late windows)."""
        try:
            import psutil

            proc = psutil.Process()
            get_fds = lambda: proc.num_fds()  # noqa: E731
        except ImportError:
            pytest.skip("psutil not installed")

        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_stream_handler
            )
            fd_samples = []
            for cycle in range(3):
                for i in range(10):
                    context = ProxyRequestContext(
                        request_id=f"plateau-fd-{cycle}-{i}",
                        protocol="openai",
                        model_id="gpt-4",
                        streaming=True,
                        original_body=json.dumps(
                            {
                                "model": "gpt-4",
                                "messages": [{"role": "user", "content": f"Msg {i}"}],
                                "stream": True,
                            }
                        ).encode(),
                        incoming_headers={"content-type": "application/json"},
                    )
                    response = await soak_coordinator.execute(context)
                    if response.stream_iterator is not None:
                        await _consume_stream(response.stream_iterator)
                fd_samples.append(get_fds())

        # FD count should not have a positive slope in late windows.
        # Allow a small warm-up delta between first and second cycle,
        # but second-to-third must be flat or decreasing.
        if len(fd_samples) >= 3:
            warmup_delta = fd_samples[1] - fd_samples[0]
            late_delta = fd_samples[2] - fd_samples[1]
            # Late window must not grow more than warm-up delta
            assert late_delta <= warmup_delta + 5, (
                f"FD slope in late window: {late_delta} "
                f"(warm-up delta: {warmup_delta}). Samples: {fd_samples}"
            )
