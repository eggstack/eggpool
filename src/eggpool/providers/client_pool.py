"""Per-provider HTTP client management."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import httpcore
import httpx

from eggpool.constants import DEFAULT_PROVIDER_ID
from eggpool.errors import UpstreamError
from eggpool.providers.outbound import HttpcoreTransport
from eggpool.providers.pproxy_transport import AsyncPProxyTransport

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig, ProviderConfig


class ProviderClientPool:
    """Manages per-provider HTTPX clients."""

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._account_clients: dict[tuple[str, str], httpx.AsyncClient] = {}

    def register(self, provider_id: str, client: httpx.AsyncClient) -> None:
        """Register a client for a provider."""
        self._clients[provider_id] = client

    def register_account(
        self,
        provider_id: str,
        account_name: str,
        client: httpx.AsyncClient,
    ) -> None:
        """Register a client for a specific provider account."""
        self._account_clients[(provider_id, account_name)] = client

    def get_client(
        self,
        provider_id: str,
        account_name: str | None = None,
    ) -> httpx.AsyncClient:
        """Get the HTTP client for a provider or a specific provider account."""
        if account_name is not None:
            account_client = self._account_clients.get((provider_id, account_name))
            if account_client is not None:
                return account_client

        client = self._clients.get(provider_id)
        if client is None:
            raise UpstreamError(f"No client for provider {provider_id!r}")
        return client

    def get_default_client(self) -> httpx.AsyncClient | None:
        """Return the legacy default provider client, if registered."""
        try:
            return self.get_client(DEFAULT_PROVIDER_ID)
        except UpstreamError:
            return None

    @property
    def providers(self) -> list[str]:
        """List registered provider IDs."""
        return list(self._clients.keys())

    def snapshot(self) -> dict[str, Any]:
        """Return a metrics snapshot for runtime diagnostics.

        Each provider gets exactly one client at startup, so per-provider
        build counts are always 1.  This exposes the total count and a
        per-provider breakdown for the diagnostics endpoint.
        """
        return {
            "build_count": len(self._clients),
            "providers": {pid: 1 for pid in self._clients},
        }

    async def close(self) -> None:
        """Close all clients."""
        closed: set[int] = set()
        for client in self._clients.values():
            if id(client) not in closed:
                closed.add(id(client))
                with contextlib.suppress(Exception):
                    await client.aclose()
        for client in self._account_clients.values():
            if id(client) not in closed:
                closed.add(id(client))
                with contextlib.suppress(Exception):
                    await client.aclose()

    @classmethod
    def from_config(
        cls,
        providers: dict[str, ProviderConfig],
        *,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> ProviderClientPool:
        """Create a client pool from provider configurations."""
        pool = cls()
        for provider_id, cfg in providers.items():
            client = _build_client(cfg, network_backend=network_backend)
            pool.register(provider_id, client)
        return pool

    @classmethod
    def from_app_config(
        cls,
        config: AppConfig,
        *,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> ProviderClientPool:
        """Create a client pool from full app config, including account proxies."""
        pool = cls.from_config(config.providers, network_backend=network_backend)
        for provider_id, cfg in config.providers.items():
            for account in cfg.accounts:
                proxy_url = config.resolve_account_proxy_url(account)
                if proxy_url is None:
                    continue
                pool.register_account(
                    provider_id,
                    account.name,
                    _build_client(cfg, proxy_url=proxy_url),
                )
        return pool


def _build_client(
    cfg: ProviderConfig,
    proxy_url: str | None = None,
    *,
    network_backend: httpcore.AsyncNetworkBackend | None = None,
) -> httpx.AsyncClient:
    """Build an HTTPX client with provider timeouts and optional proxy."""
    limits = httpx.Limits(
        max_connections=cfg.max_connections,
        max_keepalive_connections=cfg.max_keepalive,
        keepalive_expiry=cfg.keepalive_timeout_s,
    )
    transport = (
        AsyncPProxyTransport(proxy_url, limits=limits)
        if proxy_url is not None
        else None
    )
    if transport is None and network_backend is not None:
        pool = httpcore.AsyncConnectionPool(
            max_connections=cfg.max_connections,
            max_keepalive_connections=cfg.max_keepalive,
            keepalive_expiry=cfg.keepalive_timeout_s,
            network_backend=network_backend,
        )
        transport = HttpcoreTransport(pool)
    return httpx.AsyncClient(
        base_url=cfg.base_url,
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout_s,
            read=cfg.read_timeout_s,
            write=cfg.write_timeout_s,
            pool=cfg.pool_timeout_s,
        ),
        limits=limits,
        transport=transport,
    )
