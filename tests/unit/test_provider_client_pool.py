"""Tests for ProviderClientPool."""

from __future__ import annotations

import httpx
import pytest

from eggpool.errors import UpstreamError
from eggpool.models.config import AppConfig, ProviderConfig
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.providers.pproxy_transport import AsyncPProxyTransport


class TestProviderClientPool:
    """Tests for ProviderClientPool."""

    def test_empty_pool(self) -> None:
        pool = ProviderClientPool()
        assert pool.providers == []

    def test_register_and_get_client(self) -> None:
        pool = ProviderClientPool()
        client = httpx.AsyncClient(base_url="https://example.com")
        pool.register("test", client)
        assert pool.get_client("test") is client

    def test_get_client_missing_raises(self) -> None:
        pool = ProviderClientPool()
        with pytest.raises(UpstreamError, match="No client for provider"):
            pool.get_client("nonexistent")

    def test_providers_property(self) -> None:
        pool = ProviderClientPool()
        pool.register("a", httpx.AsyncClient(base_url="https://a.example.com"))
        pool.register("b", httpx.AsyncClient(base_url="https://b.example.com"))
        assert sorted(pool.providers) == ["a", "b"]

    def test_register_overwrites(self) -> None:
        pool = ProviderClientPool()
        client1 = httpx.AsyncClient(base_url="https://a.example.com")
        client2 = httpx.AsyncClient(base_url="https://b.example.com")
        pool.register("x", client1)
        pool.register("x", client2)
        assert pool.get_client("x") is client2

    @pytest.mark.anyio
    async def test_close(self) -> None:
        pool = ProviderClientPool()
        pool.register("a", httpx.AsyncClient(base_url="https://a.example.com"))
        pool.register("b", httpx.AsyncClient(base_url="https://b.example.com"))
        await pool.close()

    @pytest.mark.anyio
    async def test_close_handles_exception(self) -> None:
        pool = ProviderClientPool()
        pool.register("a", httpx.AsyncClient(base_url="https://a.example.com"))
        # close should not raise even if aclose() fails
        await pool.close()

    def test_from_config(self) -> None:
        providers = {
            "alpha": ProviderConfig(
                id="alpha",
                base_url="https://alpha.example.com",
                connect_timeout_s=3,
                read_timeout_s=10,
                write_timeout_s=5,
                max_connections=50,
                max_keepalive=10,
                keepalive_timeout_s=15,
            ),
            "beta": ProviderConfig(
                id="beta",
                base_url="https://beta.example.com",
            ),
        }
        pool = ProviderClientPool.from_config(providers)
        assert sorted(pool.providers) == ["alpha", "beta"]

        alpha_client = pool.get_client("alpha")
        assert alpha_client is not None
        assert alpha_client.base_url == "https://alpha.example.com"

        beta_client = pool.get_client("beta")
        assert beta_client is not None
        assert beta_client.base_url == "https://beta.example.com"

    def test_from_config_empty(self) -> None:
        pool = ProviderClientPool.from_config({})
        assert pool.providers == []

    def test_from_app_config_uses_account_proxy_clients(self) -> None:
        config = AppConfig(
            proxies={"local": {"url": "http://127.0.0.1:8081"}},
            providers={
                "alpha": {
                    "id": "alpha",
                    "base_url": "https://alpha.example.com",
                    "accounts": [
                        {"name": "direct", "api_key_env": "DIRECT_KEY"},
                        {
                            "name": "proxied",
                            "api_key_env": "PROXIED_KEY",
                            "proxy": "local",
                        },
                    ],
                }
            },
        )

        pool = ProviderClientPool.from_app_config(config)
        provider_client = pool.get_client("alpha")
        proxied_client = pool.get_client("alpha", "proxied")

        assert pool.get_client("alpha", "direct") is provider_client
        assert proxied_client is not provider_client
        assert isinstance(proxied_client._transport, AsyncPProxyTransport)
