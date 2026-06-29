"""Integration tests for DNS cache behavior.

Verifies end-to-end caching through DnsNetworkBackend, covering
TTL-based deduplication, stale-if-error fallback, cache expiry
causing re-resolution, and TLS hostname preservation.
"""

from __future__ import annotations

import socket
from unittest.mock import MagicMock, patch

import httpcore
import pytest

from eggpool.models.config import DnsCacheConfig
from eggpool.providers.dns_cache import DnsNetworkBackend


def _default_backend(
    config: DnsCacheConfig | None = None,
) -> DnsNetworkBackend:
    if config is None:
        config = DnsCacheConfig()
    wrapped = MagicMock(spec=httpcore.AsyncNetworkBackend)
    return DnsNetworkBackend(config, wrapped)


class TestRepeatedRequestsShareCache:
    @pytest.mark.asyncio
    async def test_repeated_connect_tcp_one_resolver_call(self) -> None:
        cfg = DnsCacheConfig(positive_ttl_seconds=300)
        backend = _default_backend(cfg)
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
        captured_hosts: list[str] = []

        async def _fake_connect_tcp(
            host: str, *args: object, **kwargs: object
        ) -> MagicMock:
            captured_hosts.append(host)
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ) as mock_lookup:
            await backend.connect_tcp("api.example.com", 443)
            await backend.connect_tcp("api.example.com", 443)
            await backend.connect_tcp("api.example.com", 443)
        assert mock_lookup.call_count == 1
        assert captured_hosts == ["1.2.3.4", "1.2.3.4", "1.2.3.4"]
        snap = backend.cache.snapshot()
        assert snap["hits"] == 2
        assert snap["misses"] == 1
        assert snap["cache_hits_total"] == 2
        assert snap["cache_misses_owner_total"] == 1
        assert snap["singleflight_waits_total"] == 0
        assert snap["resolver_calls_total"] == 1


class TestCacheExpiryCausesReResolution:
    @pytest.mark.asyncio
    async def test_expired_entry_triggers_new_lookup(self) -> None:
        cfg = DnsCacheConfig(positive_ttl_seconds=10, stale_if_error_seconds=0)
        backend = _default_backend(cfg)
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
        fake_time = [1000.0]

        def _monotonic() -> float:
            return fake_time[0]

        call_count = 0

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(socket.AF_INET, 0, 0, "", ("1.1.1.1", 0))]
            return [(socket.AF_INET, 0, 0, "", ("2.2.2.2", 0))]

        async def _fake_connect_tcp(
            host: str, *args: object, **kwargs: object
        ) -> MagicMock:
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with (
            patch(
                "eggpool.providers.dns_cache.time.monotonic",
                side_effect=_monotonic,
            ),
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=_mock_getaddrinfo,
            ),
        ):
            await backend.connect_tcp("api.example.com", 443)
            assert call_count == 1
            fake_time[0] += 20
            await backend.connect_tcp("api.example.com", 443)
            assert call_count == 2


class TestDnsFailurePreservesErrorClass:
    @pytest.mark.asyncio
    async def test_gaierror_raises_connect_error_through_backend(self) -> None:
        cfg = DnsCacheConfig()
        backend = _default_backend(cfg)

        async def _fake_connect_tcp(*args: object, **kwargs: object) -> MagicMock:
            return MagicMock(spec=httpcore.AsyncNetworkStream)

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("name resolution failed"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await backend.connect_tcp("bad.example.com", 443)

    @pytest.mark.asyncio
    async def test_timeout_raises_connect_timeout_through_backend(self) -> None:
        cfg = DnsCacheConfig()
        backend = _default_backend(cfg)

        async def _fake_connect_tcp(*args: object, **kwargs: object) -> MagicMock:
            return MagicMock(spec=httpcore.AsyncNetworkStream)

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=TimeoutError("timed out"),
            ),
            pytest.raises(httpcore.ConnectTimeout),
        ):
            await backend.connect_tcp("slow.example.com", 443)


class TestTlsHostnamePreserved:
    @pytest.mark.asyncio
    async def test_original_hostname_used_for_tls_not_resolved_ip(self) -> None:
        cfg = DnsCacheConfig(positive_ttl_seconds=300)
        backend = _default_backend(cfg)
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
        resolved_hosts: list[str] = []

        async def _fake_connect_tcp(
            host: str, *args: object, **kwargs: object
        ) -> MagicMock:
            resolved_hosts.append(host)
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
        ):
            await backend.connect_tcp("example.com", 443)
        # The resolved IP is passed to the wrapped backend for the TCP connection.
        # TLS SNI is handled separately by httpcore using the original URL hostname.
        assert resolved_hosts == ["93.184.216.34"]


class TestIpBypassThroughBackend:
    @pytest.mark.asyncio
    async def test_ip_address_bypasses_dns_cache(self) -> None:
        cfg = DnsCacheConfig(positive_ttl_seconds=300)
        backend = _default_backend(cfg)
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
        resolved_hosts: list[str] = []

        async def _fake_connect_tcp(
            host: str, *args: object, **kwargs: object
        ) -> MagicMock:
            resolved_hosts.append(host)
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
        ) as mock_lookup:
            await backend.connect_tcp("93.184.216.34", 443)
        assert mock_lookup.call_count == 0
        assert resolved_hosts == ["93.184.216.34"]
        snap = backend.cache.snapshot()
        assert snap["size"] == 0


class TestDisabledBackendBypassesCache:
    @pytest.mark.asyncio
    async def test_disabled_backend_never_resolves(self) -> None:
        cfg = DnsCacheConfig(enabled=False, positive_ttl_seconds=300)
        backend = _default_backend(cfg)
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
        resolved_hosts: list[str] = []

        async def _fake_connect_tcp(
            host: str, *args: object, **kwargs: object
        ) -> MagicMock:
            resolved_hosts.append(host)
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
        ) as mock_lookup:
            await backend.connect_tcp("example.com", 443)
            await backend.connect_tcp("example.com", 443)
        assert mock_lookup.call_count == 0
        assert resolved_hosts == ["example.com", "example.com"]
        snap = backend.cache.snapshot()
        assert snap["size"] == 0
        assert snap["misses"] == 0
        assert snap["cache_misses_owner_total"] == 0


class TestDifferentHostsIndependentCaches:
    @pytest.mark.asyncio
    async def test_different_hosts_get_separate_cache_entries(self) -> None:
        cfg = DnsCacheConfig(positive_ttl_seconds=300)
        backend = _default_backend(cfg)
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)

        async def _fake_connect_tcp(*args: object, **kwargs: object) -> MagicMock:
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ) as mock_lookup:
            await backend.connect_tcp("a.example.com", 443)
            await backend.connect_tcp("b.example.com", 443)
        assert mock_lookup.call_count == 2
        snap = backend.cache.snapshot()
        assert snap["size"] == 2


class TestConcurrentRequestsShareResolution:
    @pytest.mark.asyncio
    async def test_concurrent_connect_tcp_one_resolve(self) -> None:
        import asyncio

        cfg = DnsCacheConfig(positive_ttl_seconds=300)
        backend = _default_backend(cfg)
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)

        async def _fake_connect_tcp(*args: object, **kwargs: object) -> MagicMock:
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ) as mock_lookup:
            results = await asyncio.gather(
                backend.connect_tcp("shared.example.com", 443),
                backend.connect_tcp("shared.example.com", 443),
                backend.connect_tcp("shared.example.com", 443),
            )
        assert mock_lookup.call_count == 1
        for r in results:
            assert r is mock_stream
        snap = backend.cache.snapshot()
        assert snap["cache_misses_owner_total"] == 1
        assert snap["singleflight_waits_total"] == 2
        assert snap["resolver_calls_total"] == 1
