"""Centralized outbound HTTP client management.

The :class:`OutboundClientManager` owns a long-lived async HTTP client
shared by all non-provider network paths: update checks, external
catalog fetches, and future background/CLI network operations.

Provider-specific clients (LLM forwarding, catalog model-list fetches)
remain in :class:`ProviderClientPool` because they carry per-provider
base URLs, timeouts, and connection-pool limits.

Hot-path provider requests must **never** construct fresh HTTP clients.
The manager is the single escape hatch for background and CLI network
paths that need a plain shared client without provider-specific
transport policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import httpx

logger = logging.getLogger(__name__)

# Defaults chosen for background / CLI network paths.  These are
# intentionally conservative: update checks and catalog fetches are
# infrequent and non-latency-critical.
_DEFAULT_CONNECT_TIMEOUT_S = 10.0
_DEFAULT_READ_TIMEOUT_S = 30.0
_DEFAULT_MAX_CONNECTIONS = 10
_DEFAULT_MAX_KEEPALIVE = 4
_DEFAULT_KEEPALIVE_EXPIRY_S = 90.0


class OutboundClientManager:
    """Manages a shared async HTTP client for non-provider network paths.

    The manager is owned by application state and initialized once at
    startup.  Background tasks (update checker, catalog resolvers) and
    CLI diagnostic commands should use :meth:`get_client` rather than
    constructing fresh ``httpx.AsyncClient`` instances.

    The manager tracks construction counts so operators can verify that
    client builds do not grow with request volume.
    """

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._build_count: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

    def _build_client(self) -> httpx.AsyncClient:
        """Build the shared outbound HTTP client.

        Called at most once per manager lifetime (lazy init on first
        :meth:`get_client` call).
        """
        self._build_count += 1
        limits = httpx.Limits(
            max_connections=_DEFAULT_MAX_CONNECTIONS,
            max_keepalive_connections=_DEFAULT_MAX_KEEPALIVE,
            keepalive_expiry=_DEFAULT_KEEPALIVE_EXPIRY_S,
        )
        timeout = httpx.Timeout(
            connect=_DEFAULT_CONNECT_TIMEOUT_S,
            read=_DEFAULT_READ_TIMEOUT_S,
            write=_DEFAULT_READ_TIMEOUT_S,
            pool=_DEFAULT_CONNECT_TIMEOUT_S,
        )
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
        )
        logger.info(
            "Outbound client manager: built shared HTTP client "
            "(build #%d, max_connections=%d)",
            self._build_count,
            _DEFAULT_MAX_CONNECTIONS,
        )
        return client

    async def get_client(self) -> httpx.AsyncClient:
        """Return the shared outbound HTTP client, building it on first call.

        Thread-safe: concurrent callers will wait for the first build
        rather than creating duplicate clients.
        """
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            self._client = self._build_client()
            return self._client

    @property
    def build_count(self) -> int:
        """Return the number of clients built by this manager.

        Should stabilize at 1 after startup.  If this counter grows
        with request volume, a code path is constructing clients on
        the hot path and must be fixed.
        """
        return self._build_count

    async def aclose(self) -> None:
        """Close the shared client if one was built."""
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None
