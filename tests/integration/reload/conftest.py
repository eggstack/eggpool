"""Shared fixtures for reload correctness baseline tests (Phase 1)."""

from __future__ import annotations

import pytest_asyncio

from tests.support.reload_harness import ReloadHarness


@pytest_asyncio.fixture()
async def reload_harness():
    """Provide an initialized ReloadHarness for a single test.

    The harness creates a temporary in-memory database with migrations,
    a real RuntimeManager, ReloadManager, and ProcessRuntime. All services
    in the initial generation are MagicMock() instances.
    """
    async with ReloadHarness() as harness:
        yield harness
