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
import warnings
from collections.abc import AsyncIterable, AsyncIterator
from typing import TYPE_CHECKING, Any

import httpcore
import httpx

if TYPE_CHECKING:
    from eggpool.models.config import NetworkConfig

logger = logging.getLogger(__name__)

# Module-level flag: set to True after the first OutboundClientManager
# is created, so we can warn about ad-hoc constructions after startup.
_manager_created = False


def warn_adhoc_client_construction(location: str) -> None:
    """Warn when a fresh httpx client is constructed outside managed paths.

    Call this from any module that builds an ``httpx.AsyncClient`` or
    ``httpx.Client`` directly.  The warning is suppressed until the
    first :class:`OutboundClientManager` is created (during startup)
    so bootstrap code does not trigger false positives.
    """
    if _manager_created:
        warnings.warn(
            f"Fresh HTTP client constructed at {location}; this defeats "
            "connection reuse. Use OutboundClientManager.get_client() or "
            "ProviderClientPool instead.",
            stacklevel=2,
        )


def default_network_backend() -> httpcore.AsyncNetworkBackend:
    """Return the default httpcore async network backend."""
    try:
        from httpcore._backends.anyio import AnyIOBackend

        return AnyIOBackend()
    except ImportError:
        pass
    try:
        from httpcore._backends.trio import TrioBackend

        return TrioBackend()
    except ImportError:
        pass
    raise RuntimeError("No supported async network backend found")


class OutboundClientManager:
    """Manages a shared async HTTP client for non-provider network paths.

    The manager is owned by application state and initialized once at
    startup.  Background tasks (update checker, catalog resolvers) and
    CLI diagnostic commands should use :meth:`get_client` rather than
    constructing fresh ``httpx.AsyncClient`` instances.

    The manager tracks construction counts so operators can verify that
    client builds do not grow with request volume.
    """

    def __init__(
        self,
        config: NetworkConfig | None = None,
        *,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        global _manager_created
        _manager_created = True

        self._config = config
        self._network_backend = network_backend
        self._client: httpx.AsyncClient | None = None
        self._build_count: int = 0
        self._request_count: int = 0
        self._error_count: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()
        self._per_host_requests: dict[str, int] = {}
        self._per_host_errors: dict[str, int] = {}

    def _build_client(self) -> httpx.AsyncClient:
        """Build the shared outbound HTTP client.

        Called at most once per manager lifetime (lazy init on first
        :meth:`get_client` call).
        """
        self._build_count += 1
        if self._config is not None:
            cfg = self._config
            max_connections = cfg.max_connections
            max_keepalive = cfg.max_keepalive
            keepalive_expiry = cfg.keepalive_expiry_s
            connect_timeout = cfg.connect_timeout_s
            read_timeout = cfg.read_timeout_s
        else:
            max_connections = 10
            max_keepalive = 4
            keepalive_expiry = 90.0
            connect_timeout = 10.0
            read_timeout = 30.0

        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive,
            keepalive_expiry=keepalive_expiry,
        )
        timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )
        if self._network_backend is not None:
            pool = httpcore.AsyncConnectionPool(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive,
                keepalive_expiry=keepalive_expiry,
                network_backend=self._network_backend,
            )
            base_transport: httpx.AsyncBaseTransport = HttpcoreTransport(pool)
        else:
            base_transport = httpx.AsyncHTTPTransport(limits=limits)
        transport = MeteredAsyncTransport(base_transport, self)
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=True,
            transport=transport,
        )
        logger.debug(
            "Outbound client manager: built shared HTTP client "
            "(build #%d, max_connections=%d)",
            self._build_count,
            max_connections,
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

    def inject_client(self, client: httpx.AsyncClient) -> None:
        """Replace the internal client with a pre-built instance.

        Intended for tests that need to inject a mock transport or
        verify that code paths use the shared client.  Not for
        production use.
        """
        self._client = client

    def record_request(self, *, success: bool = True, host: str | None = None) -> None:
        """Record a completed outbound request for metrics.

        Call this after a request made through the shared client
        completes.  ``success`` should be False for connection errors,
        timeouts, and unexpected status classes.  ``host`` optionally
        identifies the target host for per-host diagnostics.
        """
        self._request_count += 1
        if host is not None:
            self._per_host_requests[host] = self._per_host_requests.get(host, 0) + 1
        if not success:
            self._error_count += 1
            if host is not None:
                self._per_host_errors[host] = self._per_host_errors.get(host, 0) + 1

    @property
    def build_count(self) -> int:
        """Return the number of clients built by this manager.

        Should stabilize at 1 after startup.  If this counter grows
        with request volume, a code path is constructing clients on
        the hot path and must be fixed.
        """
        return self._build_count

    @property
    def request_count(self) -> int:
        """Return the total number of requests made through the shared client."""
        return self._request_count

    @property
    def error_count(self) -> int:
        """Return the total number of failed requests through the shared client."""
        return self._error_count

    def snapshot(self) -> dict[str, Any]:
        """Return a metrics snapshot for runtime diagnostics."""
        return {
            "build_count": self._build_count,
            "request_count": self._request_count,
            "error_count": self._error_count,
            "has_client": self._client is not None,
            "scopes": {"global": self._build_count},
            "per_host_requests": dict(self._per_host_requests),
            "per_host_errors": dict(self._per_host_errors),
        }

    async def aclose(self) -> None:
        """Close the shared client if one was built."""
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None


class HttpcoreTransport(httpx.AsyncBaseTransport):
    """Thin transport adapter wrapping an httpcore connection pool."""

    def __init__(self, pool: httpcore.AsyncConnectionPool) -> None:
        self._pool = pool

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert isinstance(request.stream, httpx.AsyncByteStream)
        req = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        resp = await self._pool.handle_async_request(req)
        assert isinstance(resp.stream, AsyncIterable)
        extensions: dict[str, Any] = dict(resp.extensions)  # type: ignore[arg-type]
        return httpx.Response(
            status_code=resp.status,
            headers=resp.headers,
            stream=HttpcoreResponseStream(resp.stream),
            extensions=extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


class MeteredAsyncTransport(httpx.AsyncBaseTransport):
    """Transport wrapper that records shared outbound request diagnostics."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        manager: OutboundClientManager,
    ) -> None:
        self._inner = inner
        self._manager = manager

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        try:
            response = await self._inner.handle_async_request(request)
        except Exception:
            self._manager.record_request(success=False, host=host)
            raise
        self._manager.record_request(success=response.status_code < 400, host=host)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class HttpcoreResponseStream(httpx.AsyncByteStream):
    """Map httpcore response streams onto HTTPX response streams."""

    def __init__(self, stream: AsyncIterable[bytes]) -> None:
        self._stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for part in self._stream:
            yield part

    async def aclose(self) -> None:
        aclose = getattr(self._stream, "aclose", None)
        if aclose is not None:
            await aclose()
