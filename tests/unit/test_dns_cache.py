"""Tests for the DNS cache module."""

from __future__ import annotations

import asyncio
import socket
import time
from unittest.mock import MagicMock, patch

import httpcore
import pytest
from pydantic import ValidationError

from eggpool.models.config import DnsCacheConfig, NetworkConfig
from eggpool.providers.dns_cache import (
    DnsCache,
    DnsCacheKey,
    DnsNetworkBackend,
    NegativeCacheEntry,
    PositiveCacheEntry,
)


def _make_config(
    *,
    enabled: bool = True,
    max_entries: int = 50,
    positive_ttl_seconds: int = 300,
    negative_ttl_seconds: int = 30,
    stale_if_error_seconds: int = 3600,
    prefer_ipv6: bool = False,
    lookup_timeout_seconds: int = 5,
) -> DnsCacheConfig:
    return DnsCacheConfig(
        enabled=enabled,
        max_entries=max_entries,
        positive_ttl_seconds=positive_ttl_seconds,
        negative_ttl_seconds=negative_ttl_seconds,
        stale_if_error_seconds=stale_if_error_seconds,
        prefer_ipv6=prefer_ipv6,
        lookup_timeout_seconds=lookup_timeout_seconds,
    )


class TestDnsCacheKey:
    def test_equality(self) -> None:
        k1 = DnsCacheKey(hostname="example.com", address_family=socket.AF_INET)
        k2 = DnsCacheKey(hostname="example.com", address_family=socket.AF_INET)
        assert k1 == k2

    def test_inequality_hostname(self) -> None:
        k1 = DnsCacheKey(hostname="a.com", address_family=socket.AF_INET)
        k2 = DnsCacheKey(hostname="b.com", address_family=socket.AF_INET)
        assert k1 != k2

    def test_inequality_address_family(self) -> None:
        k1 = DnsCacheKey(hostname="a.com", address_family=socket.AF_INET)
        k2 = DnsCacheKey(hostname="a.com", address_family=socket.AF_INET6)
        assert k1 != k2

    def test_hash_consistency(self) -> None:
        k1 = DnsCacheKey(hostname="example.com", address_family=socket.AF_INET)
        k2 = DnsCacheKey(hostname="example.com", address_family=socket.AF_INET)
        assert hash(k1) == hash(k2)

    def test_hash_inequality(self) -> None:
        k1 = DnsCacheKey(hostname="a.com", address_family=socket.AF_INET)
        k2 = DnsCacheKey(hostname="b.com", address_family=socket.AF_INET)
        assert hash(k1) != hash(k2)

    def test_frozen(self) -> None:
        k = DnsCacheKey(hostname="a.com", address_family=socket.AF_INET)
        with pytest.raises(AttributeError):
            k.hostname = "b.com"  # type: ignore[misc]


class TestDnsCacheConfigParsing:
    def test_defaults(self) -> None:
        cfg = DnsCacheConfig()
        assert cfg.enabled is True
        assert cfg.max_entries == 50
        assert cfg.positive_ttl_seconds == 300
        assert cfg.negative_ttl_seconds == 30
        assert cfg.stale_if_error_seconds == 3600
        assert cfg.prefer_ipv6 is False
        assert cfg.lookup_timeout_seconds == 5

    def test_custom_values(self) -> None:
        cfg = DnsCacheConfig(
            enabled=False,
            max_entries=10,
            positive_ttl_seconds=60,
            negative_ttl_seconds=5,
            stale_if_error_seconds=120,
            prefer_ipv6=True,
            lookup_timeout_seconds=3,
        )
        assert cfg.enabled is False
        assert cfg.max_entries == 10
        assert cfg.positive_ttl_seconds == 60
        assert cfg.negative_ttl_seconds == 5
        assert cfg.stale_if_error_seconds == 120
        assert cfg.prefer_ipv6 is True
        assert cfg.lookup_timeout_seconds == 3

    def test_rejects_zero_max_entries(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(max_entries=0)

    def test_rejects_negative_max_entries(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(max_entries=-1)

    def test_rejects_zero_positive_ttl(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(positive_ttl_seconds=0)

    def test_rejects_negative_positive_ttl(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(positive_ttl_seconds=-1)

    def test_rejects_zero_negative_ttl(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(negative_ttl_seconds=0)

    def test_rejects_negative_negative_ttl(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(negative_ttl_seconds=-1)

    def test_allows_zero_stale_if_error(self) -> None:
        cfg = DnsCacheConfig(stale_if_error_seconds=0)
        assert cfg.stale_if_error_seconds == 0

    def test_rejects_negative_stale_if_error(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(stale_if_error_seconds=-1)

    def test_rejects_zero_lookup_timeout(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(lookup_timeout_seconds=0)

    def test_rejects_negative_lookup_timeout(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(lookup_timeout_seconds=-1)

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            DnsCacheConfig(unknown_field="foo")  # type: ignore[call-arg]


class TestNetworkConfigDnsCache:
    def test_default_dns_cache(self) -> None:
        cfg = NetworkConfig()
        assert isinstance(cfg.dns_cache, DnsCacheConfig)
        assert cfg.dns_cache.enabled is True

    def test_custom_dns_cache(self) -> None:
        dns_cfg = DnsCacheConfig(enabled=False, max_entries=5)
        cfg = NetworkConfig(dns_cache=dns_cfg)
        assert cfg.dns_cache.enabled is False
        assert cfg.dns_cache.max_entries == 5


class TestPositiveLookupCached:
    @pytest.mark.asyncio
    async def test_positive_lookup_returns_addresses(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            result = await cache.resolve("example.com", socket.AF_INET)
        assert result == ["1.2.3.4"]

    @pytest.mark.asyncio
    async def test_positive_lookup_cached_until_ttl(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ) as mock_lookup:
            result1 = await cache.resolve("example.com", socket.AF_INET)
            result2 = await cache.resolve("example.com", socket.AF_INET)
        assert result1 == ["1.2.3.4"]
        assert result2 == ["1.2.3.4"]
        assert mock_lookup.call_count == 1

    @pytest.mark.asyncio
    async def test_positive_lookup_expires_after_ttl(self) -> None:
        cfg = _make_config(positive_ttl_seconds=10, stale_if_error_seconds=0)
        cache = DnsCache(cfg)
        fake_time = [1000.0]

        def _monotonic() -> float:
            return fake_time[0]

        with (
            patch(
                "eggpool.providers.dns_cache.time.monotonic",
                side_effect=_monotonic,
            ),
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
            ) as mock_lookup,
        ):
            await cache.resolve("example.com", socket.AF_INET)
            fake_time[0] += 20
            await cache.resolve("example.com", socket.AF_INET)
        assert mock_lookup.call_count == 2

    @pytest.mark.asyncio
    async def test_positive_lookup_re_resolves_after_expiry(self) -> None:
        cfg = _make_config(positive_ttl_seconds=10, stale_if_error_seconds=0)
        cache = DnsCache(cfg)
        fake_time = [1000.0]
        call_count = 0

        def _monotonic() -> float:
            return fake_time[0]

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(socket.AF_INET, 0, 0, "", ("1.1.1.1", 0))]
            return [(socket.AF_INET, 0, 0, "", ("2.2.2.2", 0))]

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
            result1 = await cache.resolve("example.com", socket.AF_INET)
            fake_time[0] += 20
            result2 = await cache.resolve("example.com", socket.AF_INET)
        assert result1 == ["1.1.1.1"]
        assert result2 == ["2.2.2.2"]


class TestEffectiveTtlCap:
    @pytest.mark.asyncio
    async def test_stale_if_error_controls_extra_window(self) -> None:
        cfg = _make_config(positive_ttl_seconds=10, stale_if_error_seconds=3600)
        cache = DnsCache(cfg)
        fake_time = [1000.0]

        def _monotonic() -> float:
            return fake_time[0]

        with (
            patch(
                "eggpool.providers.dns_cache.time.monotonic",
                side_effect=_monotonic,
            ),
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
            ),
        ):
            await cache.resolve("example.com", socket.AF_INET)
            fake_time[0] += 20
            result = await cache.resolve("example.com", socket.AF_INET)
        assert result == ["1.2.3.4"]
        assert cache.stale_hits == 1

    @pytest.mark.asyncio
    async def test_zero_stale_if_error_means_no_stale_window(self) -> None:
        cfg = _make_config(positive_ttl_seconds=10, stale_if_error_seconds=0)
        cache = DnsCache(cfg)
        fake_time = [1000.0]

        def _monotonic() -> float:
            return fake_time[0]

        with (
            patch(
                "eggpool.providers.dns_cache.time.monotonic",
                side_effect=_monotonic,
            ),
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
            ) as mock_lookup,
        ):
            await cache.resolve("example.com", socket.AF_INET)
            fake_time[0] += 20
            await cache.resolve("example.com", socket.AF_INET)
        assert mock_lookup.call_count == 2


class TestNegativeLookupCached:
    @pytest.mark.asyncio
    async def test_negative_lookup_cached(self) -> None:
        cfg = _make_config(negative_ttl_seconds=300)
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("not found"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("bad.host", socket.AF_INET)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("not found"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("bad.host", socket.AF_INET)

    @pytest.mark.asyncio
    async def test_negative_lookup_expires_after_ttl(self) -> None:
        cfg = _make_config(negative_ttl_seconds=10)
        cache = DnsCache(cfg)
        fake_time = [1000.0]
        call_count = 0

        def _monotonic() -> float:
            return fake_time[0]

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise socket.gaierror("not found")
            return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))]

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
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("bad.host", socket.AF_INET)
            fake_time[0] += 20
            result = await cache.resolve("bad.host", socket.AF_INET)
        assert result == ["1.2.3.4"]
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_negative_lookup_counts_negative_hits(self) -> None:
        cfg = _make_config(negative_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            side_effect=socket.gaierror("not found"),
        ):
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("bad.host", socket.AF_INET)
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("bad.host", socket.AF_INET)
        assert cache.negative_hits == 1


class TestNegativeLookupRecovery:
    @pytest.mark.asyncio
    async def test_negative_lookup_recovers_after_resolver_succeeds(self) -> None:
        cfg = _make_config(negative_ttl_seconds=10)
        cache = DnsCache(cfg)
        fake_time = [1000.0]
        call_count = 0

        def _monotonic() -> float:
            return fake_time[0]

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise socket.gaierror("not found")
            return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))]

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
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("bad.host", socket.AF_INET)
            fake_time[0] += 20
            result = await cache.resolve("bad.host", socket.AF_INET)
        assert result == ["1.2.3.4"]


class TestStaleIfError:
    @pytest.mark.asyncio
    async def test_stale_entry_used_when_resolution_fails(self) -> None:
        cfg = _make_config(
            positive_ttl_seconds=10,
            negative_ttl_seconds=10,
            stale_if_error_seconds=3600,
        )
        cache = DnsCache(cfg)
        fake_time = [1000.0]
        call_count = 0

        def _monotonic() -> float:
            return fake_time[0]

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))]
            raise socket.gaierror("resolver down")

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
            result1 = await cache.resolve("example.com", socket.AF_INET)
            fake_time[0] += 20
            result2 = await cache.resolve("example.com", socket.AF_INET)
        assert result1 == ["1.2.3.4"]
        assert result2 == ["1.2.3.4"]
        assert cache.stale_hits == 1

    @pytest.mark.asyncio
    async def test_stale_entry_not_used_after_stale_window_expires(self) -> None:
        cfg = _make_config(
            positive_ttl_seconds=10,
            negative_ttl_seconds=10,
            stale_if_error_seconds=10,
        )
        cache = DnsCache(cfg)
        fake_time = [1000.0]
        call_count = 0

        def _monotonic() -> float:
            return fake_time[0]

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))]
            raise socket.gaierror("resolver down")

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
            await cache.resolve("example.com", socket.AF_INET)
            fake_time[0] += 30
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("example.com", socket.AF_INET)
        assert cache.stale_hits == 0

    @pytest.mark.asyncio
    async def test_stale_window_starts_after_positive_ttl(self) -> None:
        cfg = _make_config(
            positive_ttl_seconds=10,
            negative_ttl_seconds=10,
            stale_if_error_seconds=10,
        )
        cache = DnsCache(cfg)
        fake_time = [1000.0]
        call_count = 0

        def _monotonic() -> float:
            return fake_time[0]

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))]
            raise socket.gaierror("resolver down")

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
            await cache.resolve("example.com", socket.AF_INET)
            # expires_at=1010, stale_until=1020
            # T=1015: expired but within stale window
            fake_time[0] += 15
            result = await cache.resolve("example.com", socket.AF_INET)
            assert result == ["1.2.3.4"]
            assert cache.stale_hits == 1
            # T=1025: expired and stale window expired
            fake_time[0] += 10
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("example.com", socket.AF_INET)
            assert cache.stale_hits == 1


class TestLruEviction:
    @pytest.mark.asyncio
    async def test_lru_eviction_removes_oldest(self) -> None:
        cfg = _make_config(max_entries=2, positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await cache.resolve("a.com", socket.AF_INET)
            await cache.resolve("b.com", socket.AF_INET)
            await cache.resolve("c.com", socket.AF_INET)
        assert cache.evictions == 1
        assert len(cache._cache) == 2
        snap = cache.snapshot()
        assert snap["evictions"] == 1

    @pytest.mark.asyncio
    async def test_lru_access_prevents_eviction(self) -> None:
        cfg = _make_config(max_entries=2, positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await cache.resolve("a.com", socket.AF_INET)
            await cache.resolve("b.com", socket.AF_INET)
            await cache.resolve("a.com", socket.AF_INET)
            await cache.resolve("c.com", socket.AF_INET)
        assert cache.evictions == 1
        result = await cache.resolve("a.com", socket.AF_INET)
        assert result == ["1.2.3.4"]

    @pytest.mark.asyncio
    async def test_lru_eviction_for_negative_entries(self) -> None:
        cfg = _make_config(max_entries=1, negative_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            side_effect=socket.gaierror("fail"),
        ):
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("a.com", socket.AF_INET)
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("b.com", socket.AF_INET)
        assert cache.evictions == 1

    @pytest.mark.asyncio
    async def test_lru_eviction_mixed_entry_types(self) -> None:
        cfg = _make_config(
            max_entries=2, positive_ttl_seconds=300, negative_ttl_seconds=300
        )
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await cache.resolve("a.com", socket.AF_INET)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("fail"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("b.com", socket.AF_INET)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("5.6.7.8", 0))],
        ):
            await cache.resolve("c.com", socket.AF_INET)
        assert cache.evictions == 1
        assert len(cache._cache) == 2

    @pytest.mark.asyncio
    async def test_eviction_reason_capacity(self) -> None:
        cfg = _make_config(max_entries=2, positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await cache.resolve("a.com", socket.AF_INET)
            await cache.resolve("b.com", socket.AF_INET)
            await cache.resolve("c.com", socket.AF_INET)
        snap = cache.snapshot()
        assert snap["evictions_by_reason"]["capacity"] == 1
        assert snap["evictions_by_reason"]["ttl_expiry"] == 0

    @pytest.mark.asyncio
    async def test_eviction_reason_ttl_expiry(self) -> None:
        cfg = _make_config(positive_ttl_seconds=10, stale_if_error_seconds=0)
        cache = DnsCache(cfg)
        fake_time = [1000.0]

        def _monotonic() -> float:
            return fake_time[0]

        with (
            patch(
                "eggpool.providers.dns_cache.time.monotonic",
                side_effect=_monotonic,
            ),
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
            ),
        ):
            await cache.resolve("a.com", socket.AF_INET)
            assert cache._evictions_by_reason["ttl_expiry"] == 0
            fake_time[0] += 20
            await cache.resolve("a.com", socket.AF_INET)
        assert cache._evictions_by_reason["ttl_expiry"] == 1
        assert cache._evictions_by_reason["capacity"] == 0


class TestIpBypass:
    @pytest.mark.asyncio
    async def test_ipv4_bypasses_cache(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        result = await cache.resolve("1.2.3.4", socket.AF_INET)
        assert result is None
        assert cache.misses == 0

    @pytest.mark.asyncio
    async def test_ipv6_bypasses_cache(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        result = await cache.resolve("::1", socket.AF_INET)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_hostname_bypasses_cache(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        result = await cache.resolve("", socket.AF_INET)
        assert result is None

    @pytest.mark.asyncio
    async def test_ipv4_mapped_v6_bypasses_cache(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        result = await cache.resolve("::ffff:127.0.0.1", socket.AF_INET)
        assert result is None


class TestDisabledCache:
    def test_enabled_flag_stored_in_config(self) -> None:
        cfg = _make_config(enabled=False)
        cache = DnsCache(cfg)
        assert cache._config.enabled is False

    @pytest.mark.asyncio
    async def test_disabled_backend_bypasses_cache(self) -> None:
        cfg = _make_config(enabled=False, positive_ttl_seconds=300)
        wrapped = MagicMock(spec=httpcore.AsyncNetworkBackend)
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)

        async def _fake_connect_tcp(*args: object, **kwargs: object) -> MagicMock:
            return mock_stream

        wrapped.connect_tcp = _fake_connect_tcp
        backend = DnsNetworkBackend(cfg, wrapped)
        captured_host: list[str] = []

        async def _capture_connect_tcp(
            host: str, *args: object, **kwargs: object
        ) -> MagicMock:
            captured_host.append(host)
            return mock_stream

        wrapped.connect_tcp = _capture_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ) as mock_lookup:
            await backend.connect_tcp("example.com", 443)
            await backend.connect_tcp("example.com", 443)
        assert captured_host == ["example.com", "example.com"]
        assert mock_lookup.call_count == 0

    def test_enabled_flag_defaults_true(self) -> None:
        cfg = _make_config()
        assert cfg.enabled is True


class TestConcurrentResolution:
    @pytest.mark.asyncio
    async def test_different_hosts_do_not_block(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)

        def _mock_getaddrinfo(host, port, family):
            return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))]

        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            side_effect=_mock_getaddrinfo,
        ):
            results = await asyncio.gather(
                cache.resolve("a.com", socket.AF_INET),
                cache.resolve("b.com", socket.AF_INET),
                cache.resolve("c.com", socket.AF_INET),
            )
        assert results[0] == ["1.2.3.4"]
        assert results[1] == ["1.2.3.4"]
        assert results[2] == ["1.2.3.4"]

    @pytest.mark.asyncio
    async def test_concurrent_different_hosts_all_resolve(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        call_count = 0

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            raise socket.gaierror(f"fail-{host}")

        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            side_effect=_mock_getaddrinfo,
        ):
            results = await asyncio.gather(
                cache.resolve("a.com", socket.AF_INET),
                cache.resolve("b.com", socket.AF_INET),
                return_exceptions=True,
            )
        for r in results:
            assert isinstance(r, httpcore.ConnectError)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_singleflight_deduplicates_sequential_lookups(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        call_count = 0

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))]

        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            side_effect=_mock_getaddrinfo,
        ):
            await cache.resolve("example.com", socket.AF_INET)
            await cache.resolve("example.com", socket.AF_INET)
            await cache.resolve("example.com", socket.AF_INET)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_singleflight_deduplicates(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        call_count = 0

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))]

        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            side_effect=_mock_getaddrinfo,
        ):
            results = await asyncio.gather(
                cache.resolve("example.com", socket.AF_INET),
                cache.resolve("example.com", socket.AF_INET),
                cache.resolve("example.com", socket.AF_INET),
                cache.resolve("example.com", socket.AF_INET),
                cache.resolve("example.com", socket.AF_INET),
                cache.resolve("example.com", socket.AF_INET),
                cache.resolve("example.com", socket.AF_INET),
                cache.resolve("example.com", socket.AF_INET),
                cache.resolve("example.com", socket.AF_INET),
                cache.resolve("example.com", socket.AF_INET),
            )
        assert call_count == 1
        for r in results:
            assert r == ["1.2.3.4"]

    @pytest.mark.asyncio
    async def test_concurrent_singleflight_on_failure(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        call_count = 0

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            raise socket.gaierror("resolver down")

        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            side_effect=_mock_getaddrinfo,
        ):
            results = await asyncio.gather(
                cache.resolve("fail.com", socket.AF_INET),
                cache.resolve("fail.com", socket.AF_INET),
                cache.resolve("fail.com", socket.AF_INET),
                return_exceptions=True,
            )
        assert call_count == 1
        for r in results:
            assert isinstance(r, httpcore.ConnectError)


class TestSnapshot:
    def test_initial_snapshot(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        snap = cache.snapshot()
        assert snap == {
            "max_entries": 50,
            "hits": 0,
            "misses": 0,
            "negative_hits": 0,
            "stale_hits": 0,
            "evictions": 0,
            "evictions_by_reason": {"capacity": 0, "ttl_expiry": 0},
            "size": 0,
            "entries": {"positive": 0, "negative": 0},
            "resolution_errors": {},
            "by_host": {},
            "hosts": [],
        }

    @pytest.mark.asyncio
    async def test_snapshot_after_operations(self) -> None:
        cfg = _make_config(
            max_entries=2,
            positive_ttl_seconds=300,
            negative_ttl_seconds=300,
        )
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await cache.resolve("a.com", socket.AF_INET)
            await cache.resolve("a.com", socket.AF_INET)
        snap = cache.snapshot()
        assert snap["hits"] == 1
        assert snap["misses"] == 1
        assert snap["size"] == 1

    @pytest.mark.asyncio
    async def test_snapshot_size_after_eviction(self) -> None:
        cfg = _make_config(max_entries=3, positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            for i in range(5):
                await cache.resolve(f"{chr(97 + i)}.com", socket.AF_INET)
        snap = cache.snapshot()
        assert snap["size"] == 3
        assert snap["evictions"] == 2

    @pytest.mark.asyncio
    async def test_snapshot_negative_hits(self) -> None:
        cfg = _make_config(negative_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            side_effect=socket.gaierror("fail"),
        ):
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("bad.host", socket.AF_INET)
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("bad.host", socket.AF_INET)
        snap = cache.snapshot()
        assert snap["negative_hits"] == 1

    @pytest.mark.asyncio
    async def test_snapshot_entries_breakdown(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300, negative_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await cache.resolve("a.com", socket.AF_INET)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("fail"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("b.com", socket.AF_INET)
        snap = cache.snapshot()
        assert snap["entries"] == {"positive": 1, "negative": 1}

    @pytest.mark.asyncio
    async def test_snapshot_resolution_errors(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("name error"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("fail.com", socket.AF_INET)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=TimeoutError("slow"),
            ),
            pytest.raises(httpcore.ConnectTimeout),
        ):
            await cache.resolve("slow.com", socket.AF_INET)
        snap = cache.snapshot()
        errs = snap["resolution_errors"]
        assert errs["fail.com/ipv4/dns_resolution"] == 1
        assert errs["slow.com/ipv4/timeout"] == 1

    @pytest.mark.asyncio
    async def test_snapshot_by_host_negative_and_stale(self) -> None:
        cfg = _make_config(
            positive_ttl_seconds=10,
            negative_ttl_seconds=300,
            stale_if_error_seconds=10,
        )
        cache = DnsCache(cfg)
        fake_time = [1000.0]
        call_count = 0

        def _monotonic() -> float:
            return fake_time[0]

        def _mock_getaddrinfo(host, port, family):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return [(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))]
            raise socket.gaierror("down")

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
            await cache.resolve("host.com", socket.AF_INET)
            fake_time[0] += 15
            await cache.resolve("host.com", socket.AF_INET)
            fake_time[0] += 10
            with pytest.raises(httpcore.ConnectError):
                await cache.resolve("host.com", socket.AF_INET)
        snap = cache.snapshot()
        host_stats = snap["by_host"]["host.com/ipv4"]
        assert host_stats["stale_hits"] == 1
        assert host_stats["misses"] == 2

    @pytest.mark.asyncio
    async def test_snapshot_hosts_positive_entry(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300, stale_if_error_seconds=60)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await cache.resolve("test.com", socket.AF_INET)
        snap = cache.snapshot()
        hosts = snap["hosts"]
        assert len(hosts) == 1
        entry = hosts[0]
        assert entry["host"] == "test.com"
        assert entry["family"] == "ipv4"
        assert entry["state"] == "positive"
        assert entry["expires_in_seconds"] > 0
        assert entry["stale_available"] is True
        assert entry["last_error_kind"] is None

    @pytest.mark.asyncio
    async def test_snapshot_hosts_negative_entry(self) -> None:
        cfg = _make_config(negative_ttl_seconds=300)
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("name error"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("fail.com", socket.AF_INET)
        snap = cache.snapshot()
        hosts = snap["hosts"]
        assert len(hosts) == 1
        entry = hosts[0]
        assert entry["host"] == "fail.com"
        assert entry["family"] == "ipv4"
        assert entry["state"] == "negative"
        assert entry["expires_in_seconds"] > 0
        assert entry["stale_available"] is False
        assert entry["last_error_kind"] == "ConnectError"

    @pytest.mark.asyncio
    async def test_snapshot_hosts_empty_when_no_entries(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        snap = cache.snapshot()
        assert snap["hosts"] == []


class TestPreferIpv6:
    def test_prefer_ipv6_default_false(self) -> None:
        cfg = _make_config()
        backend = DnsNetworkBackend(cfg, MagicMock(spec=httpcore.AsyncNetworkBackend))
        assert backend._address_family == socket.AF_UNSPEC

    def test_prefer_ipv6_sets_af_inet6(self) -> None:
        cfg = _make_config(prefer_ipv6=True)
        backend = DnsNetworkBackend(cfg, MagicMock(spec=httpcore.AsyncNetworkBackend))
        assert backend._address_family == socket.AF_INET6

    @pytest.mark.asyncio
    async def test_prefer_ipv6_uses_ipv6_family(self) -> None:
        cfg = _make_config(prefer_ipv6=True)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET6, 0, 0, "", ("::1", 0))],
        ) as mock_lookup:
            result = await cache.resolve("example.com", socket.AF_INET6)
        assert result == ["::1"]
        mock_lookup.assert_called_once_with("example.com", None, socket.AF_INET6)


class TestLookupTimeout:
    @pytest.mark.asyncio
    async def test_lookup_timeout_wraps_dns_lookup(self) -> None:
        cfg = _make_config(lookup_timeout_seconds=1)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            result = await cache.resolve("example.com", socket.AF_INET)
        assert result == ["1.2.3.4"]

    @pytest.mark.asyncio
    async def test_lookup_timeout_fires_on_slow_lookup(self) -> None:
        cfg = _make_config(lookup_timeout_seconds=1)
        cache = DnsCache(cfg)

        async def _slow_lookup(*args: object) -> list[object]:
            await asyncio.sleep(10)
            return []  # pragma: no cover

        with (
            patch.object(cache, "_dns_lookup", side_effect=_slow_lookup),
            pytest.raises(httpcore.ConnectTimeout),
        ):
            await cache.resolve("slow.com", socket.AF_INET)
        snap = cache.snapshot()
        assert snap["resolution_errors"]["slow.com/ipv4/timeout"] == 1

    def test_lookup_timeout_none_when_zero(self) -> None:
        cfg = _make_config(lookup_timeout_seconds=5)
        cache = DnsCache(cfg)
        assert cache._lookup_timeout_s == 5.0

    def test_lowercases_hostname(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        assert cache._normalize("EXAMPLE.COM") == "example.com"
        assert cache._normalize("Example.Com") == "example.com"

    def test_returns_none_for_ip(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        assert cache._normalize("1.2.3.4") is None
        assert cache._normalize("::1") is None

    def test_returns_none_for_empty(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        assert cache._normalize("") is None

    def test_preserves_lowercase(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        assert cache._normalize("abc.com") == "abc.com"

    def test_returns_none_for_ipv4_mapped(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        assert cache._normalize("::ffff:10.0.0.1") is None

    def test_returns_none_for_loopback(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        assert cache._normalize("127.0.0.1") is None

    def test_returns_none_for_link_local(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        assert cache._normalize("169.254.1.1") is None


class TestDnsNetworkBackend:
    def _make_backend(self, config: DnsCacheConfig | None = None) -> DnsNetworkBackend:
        if config is None:
            config = _make_config()
        wrapped = MagicMock(spec=httpcore.AsyncNetworkBackend)
        return DnsNetworkBackend(config, wrapped)

    @pytest.mark.asyncio
    async def test_connect_tcp_resolves_host(self) -> None:
        backend = self._make_backend()
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
        backend._wrapped.connect_tcp = MagicMock(return_value=mock_stream)

        async def _fake_connect_tcp(*args: object, **kwargs: object) -> MagicMock:
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            result = await backend.connect_tcp("example.com", 443, timeout=5.0)
        assert result is mock_stream

    @pytest.mark.asyncio
    async def test_connect_tcp_fallback_to_original_host(self) -> None:
        cfg = _make_config()
        wrapped = MagicMock(spec=httpcore.AsyncNetworkBackend)
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)

        async def _fake_connect_tcp(*args: object, **kwargs: object) -> MagicMock:
            return mock_stream

        wrapped.connect_tcp = _fake_connect_tcp
        backend = DnsNetworkBackend(cfg, wrapped)
        result = await backend.connect_tcp("1.2.3.4", 80)
        assert result is mock_stream

    @pytest.mark.asyncio
    async def test_connect_unix_socket_delegates(self) -> None:
        backend = self._make_backend()
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)

        async def _fake_connect_unix(*args: object, **kwargs: object) -> MagicMock:
            return mock_stream

        backend._wrapped.connect_unix_socket = _fake_connect_unix
        result = await backend.connect_unix_socket("/tmp/test.sock", timeout=1.0)
        assert result is mock_stream

    @pytest.mark.asyncio
    async def test_sleep_delegates(self) -> None:
        backend = self._make_backend()
        sleep_called_with: list[float] = []

        async def _fake_sleep(seconds: float) -> None:
            sleep_called_with.append(seconds)

        backend._wrapped.sleep = _fake_sleep
        await backend.sleep(1.5)
        assert sleep_called_with == [1.5]

    def test_cache_property(self) -> None:
        backend = self._make_backend()
        assert isinstance(backend.cache, DnsCache)
        assert backend.cache is backend._cache

    def test_cache_exposes_config(self) -> None:
        cfg = _make_config(max_entries=10)
        backend = self._make_backend(cfg)
        assert backend.cache._config.max_entries == 10

    @pytest.mark.asyncio
    async def test_connect_tcp_uses_first_resolved_address(self) -> None:
        backend = self._make_backend()
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
        captured_host: list[str] = []

        async def _fake_connect_tcp(
            host: str, *args: object, **kwargs: object
        ) -> MagicMock:
            captured_host.append(host)
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, 0, 0, "", ("1.1.1.1", 0)),
                (socket.AF_INET, 0, 0, "", ("2.2.2.2", 0)),
            ],
        ):
            await backend.connect_tcp("multi.com", 443)
        assert captured_host == ["1.1.1.1"]

    @pytest.mark.asyncio
    async def test_connect_tcp_passes_all_params(self) -> None:
        backend = self._make_backend()
        mock_stream = MagicMock(spec=httpcore.AsyncNetworkStream)
        call_kwargs: dict[str, object] = {}

        async def _fake_connect_tcp(
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: object = None,
        ) -> MagicMock:
            call_kwargs["host"] = host
            call_kwargs["port"] = port
            call_kwargs["timeout"] = timeout
            call_kwargs["local_address"] = local_address
            return mock_stream

        backend._wrapped.connect_tcp = _fake_connect_tcp
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await backend.connect_tcp(
                "example.com",
                8080,
                timeout=10.0,
                local_address="0.0.0.0",
                socket_options=[],
            )
        assert call_kwargs["host"] == "1.2.3.4"
        assert call_kwargs["port"] == 8080
        assert call_kwargs["timeout"] == 10.0
        assert call_kwargs["local_address"] == "0.0.0.0"


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_gaierror_raises_connect_error(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("name resolution failed"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("bad.host", socket.AF_INET)

    @pytest.mark.asyncio
    async def test_timeout_error_raises_connect_timeout(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=TimeoutError("timed out"),
            ),
            pytest.raises(httpcore.ConnectTimeout),
        ):
            await cache.resolve("slow.host", socket.AF_INET)

    @pytest.mark.asyncio
    async def test_os_error_raises_connect_error(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=OSError("network unreachable"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("bad.host", socket.AF_INET)

    @pytest.mark.asyncio
    async def test_empty_result_stored_as_negative(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[],
        ):
            result = await cache.resolve("noaddrs.host", socket.AF_INET)
        assert result == []
        key = DnsCacheKey(hostname="noaddrs.host", address_family=socket.AF_INET)
        assert key in cache._cache
        assert isinstance(cache._cache[key], NegativeCacheEntry)

    @pytest.mark.asyncio
    async def test_successful_result_stored_as_positive(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await cache.resolve("ok.host", socket.AF_INET)
        key = DnsCacheKey(hostname="ok.host", address_family=socket.AF_INET)
        assert key in cache._cache
        entry = cache._cache[key]
        assert isinstance(entry, PositiveCacheEntry)
        assert entry.addresses == ["1.2.3.4"]
        assert entry.expires_at > time.monotonic()

    @pytest.mark.asyncio
    async def test_gaierror_stores_negative_cache_entry(self) -> None:
        cfg = _make_config(negative_ttl_seconds=300)
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("boom"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("fail.host", socket.AF_INET)
        key = DnsCacheKey(hostname="fail.host", address_family=socket.AF_INET)
        assert key in cache._cache
        entry = cache._cache[key]
        assert isinstance(entry, NegativeCacheEntry)
        assert entry.error_class is httpcore.ConnectError
        assert entry.error_message == "boom"

    @pytest.mark.asyncio
    async def test_timeout_stores_negative_cache_entry(self) -> None:
        cfg = _make_config(negative_ttl_seconds=300)
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=TimeoutError("slow"),
            ),
            pytest.raises(httpcore.ConnectTimeout),
        ):
            await cache.resolve("slow.host", socket.AF_INET)
        key = DnsCacheKey(hostname="slow.host", address_family=socket.AF_INET)
        assert key in cache._cache
        entry = cache._cache[key]
        assert isinstance(entry, NegativeCacheEntry)
        assert entry.error_class is httpcore.ConnectTimeout

    @pytest.mark.asyncio
    async def test_os_error_stores_negative_cache_entry(self) -> None:
        cfg = _make_config(negative_ttl_seconds=300)
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=OSError("down"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("down.host", socket.AF_INET)
        key = DnsCacheKey(hostname="down.host", address_family=socket.AF_INET)
        assert key in cache._cache
        entry = cache._cache[key]
        assert isinstance(entry, NegativeCacheEntry)
        assert entry.error_class is httpcore.ConnectError


class TestPositiveCacheEntry:
    def test_frozen(self) -> None:
        entry = PositiveCacheEntry(
            addresses=["1.2.3.4"],
            expires_at=100.0,
            stale_until=200.0,
        )
        with pytest.raises(AttributeError):
            entry.addresses = ["5.6.7.8"]  # type: ignore[misc]

    def test_addresses_is_list(self) -> None:
        entry = PositiveCacheEntry(
            addresses=["a", "b"],
            expires_at=100.0,
            stale_until=200.0,
        )
        assert entry.addresses == ["a", "b"]

    def test_stale_until_after_expires(self) -> None:
        entry = PositiveCacheEntry(
            addresses=["1.2.3.4"],
            expires_at=100.0,
            stale_until=200.0,
        )
        assert entry.stale_until > entry.expires_at


class TestNegativeCacheEntry:
    def test_frozen(self) -> None:
        entry = NegativeCacheEntry(
            error_class=httpcore.ConnectError,
            error_message="fail",
            expires_at=100.0,
        )
        with pytest.raises(AttributeError):
            entry.error_message = "other"  # type: ignore[misc]

    def test_error_class_is_type(self) -> None:
        entry = NegativeCacheEntry(
            error_class=httpcore.ConnectError,
            error_message="",
            expires_at=100.0,
        )
        assert entry.error_class is httpcore.ConnectError
        assert issubclass(entry.error_class, Exception)


class TestMultiAddressResolution:
    @pytest.mark.asyncio
    async def test_returns_all_addresses(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, 0, 0, "", ("1.1.1.1", 0)),
                (socket.AF_INET, 0, 0, "", ("2.2.2.2", 0)),
            ],
        ):
            result = await cache.resolve("multi.com", socket.AF_INET)
        assert result == ["1.1.1.1", "2.2.2.2"]

    @pytest.mark.asyncio
    async def test_single_address_wrapped_in_list(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            result = await cache.resolve("single.com", socket.AF_INET)
        assert result == ["1.2.3.4"]
        assert isinstance(result, list)


class TestHostnameCaseNormalization:
    @pytest.mark.asyncio
    async def test_case_insensitive_caching(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ) as mock_lookup:
            await cache.resolve("Example.Com", socket.AF_INET)
            await cache.resolve("example.com", socket.AF_INET)
        assert mock_lookup.call_count == 1

    @pytest.mark.asyncio
    async def test_case_insensitive_miss(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ) as mock_lookup:
            await cache.resolve("ABC.COM", socket.AF_INET)
            await cache.resolve("abc.com", socket.AF_INET)
        assert mock_lookup.call_count == 1


class TestAddressFamilySeparation:
    @pytest.mark.asyncio
    async def test_different_families_are_separate_keys(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ) as mock_lookup:
            await cache.resolve("example.com", socket.AF_INET)
            await cache.resolve("example.com", socket.AF_INET6)
        assert mock_lookup.call_count == 2

    @pytest.mark.asyncio
    async def test_same_family_shares_cache(self) -> None:
        cfg = _make_config(positive_ttl_seconds=300)
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ) as mock_lookup:
            await cache.resolve("example.com", socket.AF_INET)
            await cache.resolve("example.com", socket.AF_INET)
        assert mock_lookup.call_count == 1


class TestCacheKeyInCache:
    @pytest.mark.asyncio
    async def test_positive_entry_key_matches_after_store(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with patch(
            "eggpool.providers.dns_cache.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.2.3.4", 0))],
        ):
            await cache.resolve("test.com", socket.AF_INET)
        key = DnsCacheKey(hostname="test.com", address_family=socket.AF_INET)
        assert key in cache._cache
        assert isinstance(cache._cache[key], PositiveCacheEntry)

    @pytest.mark.asyncio
    async def test_negative_entry_key_matches_after_store(self) -> None:
        cfg = _make_config()
        cache = DnsCache(cfg)
        with (
            patch(
                "eggpool.providers.dns_cache.socket.getaddrinfo",
                side_effect=socket.gaierror("nope"),
            ),
            pytest.raises(httpcore.ConnectError),
        ):
            await cache.resolve("fail.com", socket.AF_INET)
        key = DnsCacheKey(hostname="fail.com", address_family=socket.AF_INET)
        assert key in cache._cache
        assert isinstance(cache._cache[key], NegativeCacheEntry)
