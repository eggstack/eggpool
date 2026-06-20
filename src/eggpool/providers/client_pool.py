"""Per-provider HTTP client management."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import httpx

from eggpool.errors import UpstreamError
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

    @property
    def providers(self) -> list[str]:
        """List registered provider IDs."""
        return list(self._clients.keys())

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
    def from_config(cls, providers: dict[str, ProviderConfig]) -> ProviderClientPool:
        """Create a client pool from provider configurations."""
        pool = cls()
        for provider_id, cfg in providers.items():
            client = _build_client(cfg)
            pool.register(provider_id, client)
        return pool

    @classmethod
    def from_app_config(cls, config: AppConfig) -> ProviderClientPool:
        """Create a client pool from full app config, including account proxies."""
        pool = cls.from_config(config.providers)
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
    return httpx.AsyncClient(
        base_url=cfg.base_url,
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout_s,
            read=cfg.read_timeout_s,
            write=cfg.write_timeout_s,
            pool=cfg.connect_timeout_s,
        ),
        limits=limits,
        transport=transport,
    )
