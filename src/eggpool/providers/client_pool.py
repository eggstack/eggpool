"""Per-provider HTTP client management."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import httpx

from eggpool.constants import DEFAULT_PROVIDER_ID
from eggpool.errors import UpstreamError

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig, ProviderConfig


class ProviderClientPool:
    """Manages per-provider HTTPX clients."""

    def __init__(self) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._account_clients: dict[tuple[str, str], httpx.AsyncClient] = {}
        # Clients displaced by a later registration of the same key.
        # Deferred to close() so re-registration cannot leak the
        # previous connection pool.
        self._displaced: list[httpx.AsyncClient] = []

    def register(self, provider_id: str, client: httpx.AsyncClient) -> None:
        """Register a client for a provider."""
        previous = self._clients.get(provider_id)
        if previous is not None and previous is not client:
            self._displaced.append(previous)
        self._clients[provider_id] = client

    def register_account(
        self,
        provider_id: str,
        account_name: str,
        client: httpx.AsyncClient,
    ) -> None:
        """Register a client for a specific provider account."""
        key = (provider_id, account_name)
        previous = self._account_clients.get(key)
        if previous is not None and previous is not client:
            self._displaced.append(previous)
        self._account_clients[key] = client

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
        build counts are always 1.  Account-specific clients registered
        via :meth:`register_account` (e.g. when an account has a
        configured proxy) are counted separately so runtime and dashboard
        network diagnostics do not underreport total client construction.
        """
        per_provider_accounts: dict[str, int] = {}
        for provider_id, _account_name in self._account_clients:
            per_provider_accounts[provider_id] = (
                per_provider_accounts.get(provider_id, 0) + 1
            )
        provider_ids = set(self._clients) | set(per_provider_accounts)
        providers: dict[str, int] = {
            pid: (1 if pid in self._clients else 0) + per_provider_accounts.get(pid, 0)
            for pid in sorted(provider_ids)
        }
        return {
            "build_count": len(self._clients) + len(self._account_clients),
            "providers": providers,
            "account_client_count": len(self._account_clients),
            "account_clients": [
                {"provider_id": pid, "account_name": acct}
                for pid, acct in sorted(self._account_clients)
            ],
        }

    async def close(self) -> None:
        """Close all clients."""
        closed: set[int] = set()

        async def _aclose(client: httpx.AsyncClient) -> None:
            if id(client) in closed:
                return
            closed.add(id(client))
            with contextlib.suppress(Exception):
                await client.aclose()

        for client in self._displaced:
            await _aclose(client)
        self._displaced.clear()
        for client in self._clients.values():
            await _aclose(client)
        for client in self._account_clients.values():
            await _aclose(client)

    @classmethod
    def from_config(
        cls,
        providers: dict[str, ProviderConfig],
    ) -> ProviderClientPool:
        """Create a client pool from provider configurations."""
        pool = cls()
        for provider_id, cfg in providers.items():
            client = _build_client(cfg)
            pool.register(provider_id, client)
        return pool

    @classmethod
    def from_app_config(
        cls,
        config: AppConfig,
    ) -> ProviderClientPool:
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
        _build_proxy_transport(proxy_url, limits) if proxy_url is not None else None
    )
    return httpx.AsyncClient(
        base_url=cfg.base_url,
        timeout=httpx.Timeout(
            connect=cfg.connect_timeout_s,
            read=cfg.stream_timeouts.transport_read_timeout(cfg.read_timeout_s),
            write=cfg.write_timeout_s,
            pool=cfg.pool_timeout_s,
        ),
        limits=limits,
        transport=transport,
    )


def _build_proxy_transport(
    proxy_url: str,
    limits: httpx.Limits,
) -> httpx.AsyncBaseTransport:
    """Load the optional pproxy transport only for proxied accounts."""
    from eggpool.providers.pproxy_transport import AsyncPProxyTransport

    return AsyncPProxyTransport(proxy_url, limits=limits)
