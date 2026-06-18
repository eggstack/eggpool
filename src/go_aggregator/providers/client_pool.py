"""Per-provider HTTP client management."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import httpx

from go_aggregator.errors import UpstreamError

if TYPE_CHECKING:
    from go_aggregator.models.config import ProviderConfig


class ProviderClientPool:
    """Manages per-provider HTTPX clients."""

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}

    def register(self, provider_id: str, client: httpx.AsyncClient) -> None:
        """Register a client for a provider."""
        self._clients[provider_id] = client

    def get_client(self, provider_id: str) -> httpx.AsyncClient:
        """Get the HTTP client for a provider."""
        client = self._clients.get(provider_id)
        if client is None:
            raise UpstreamError(f"No client for provider {provider_id!r}")
        return client

    @property
    def providers(self) -> list[str]:
        """List registered provider IDs."""
        return list(self._clients.keys())

    async def close(self) -> None:
        """Close all clients."""
        for client in self._clients.values():
            with contextlib.suppress(Exception):
                await client.aclose()

    @classmethod
    def from_config(cls, providers: dict[str, ProviderConfig]) -> ProviderClientPool:
        """Create a client pool from provider configurations."""
        pool = cls()
        for provider_id, cfg in providers.items():
            client = httpx.AsyncClient(
                base_url=cfg.base_url,
                timeout=httpx.Timeout(
                    connect=cfg.connect_timeout_s,
                    read=cfg.read_timeout_s,
                    write=cfg.write_timeout_s,
                    pool=cfg.connect_timeout_s,
                ),
                limits=httpx.Limits(
                    max_connections=cfg.max_connections,
                    max_keepalive_connections=cfg.max_keepalive,
                    keepalive_expiry=cfg.keepalive_timeout_s,
                ),
            )
            pool.register(provider_id, client)
        return pool
