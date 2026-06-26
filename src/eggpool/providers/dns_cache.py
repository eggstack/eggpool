"""Bounded in-memory DNS cache with singleflight deduplication."""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpcore

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eggpool.models.config import DnsCacheConfig


@dataclass(frozen=True, slots=True)
class DnsCacheKey:
    hostname: str
    address_family: int

    def __hash__(self) -> int:
        return hash((self.hostname, self.address_family))


@dataclass(frozen=True, slots=True)
class PositiveCacheEntry:
    addresses: list[str]
    expires_at: float
    stale_until: float


@dataclass(frozen=True, slots=True)
class NegativeCacheEntry:
    error_class: type[Exception]
    error_message: str
    expires_at: float


class DnsCache:
    _MAX_TRACKED_HOSTS = 256

    def __init__(self, config: DnsCacheConfig) -> None:
        self._config = config
        self._cache: collections.OrderedDict[
            DnsCacheKey, PositiveCacheEntry | NegativeCacheEntry
        ] = collections.OrderedDict()
        self._singleflight: dict[DnsCacheKey, asyncio.Future[list[str] | None]] = {}
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0
        self.negative_hits = 0
        self.stale_hits = 0
        self.evictions = 0
        self._per_host: dict[tuple[str, int], dict[str, int]] = {}
        self._resolution_errors: dict[tuple[str, int, str], int] = {}
        self._lookup_timeout_s: float | None = (
            float(config.lookup_timeout_seconds)
            if config.lookup_timeout_seconds
            else None
        )

    def _record(self, key: DnsCacheKey, field: str) -> None:
        counter_key = (key.hostname, key.address_family)
        if counter_key not in self._per_host:
            if len(self._per_host) >= self._MAX_TRACKED_HOSTS:
                return
            self._per_host[counter_key] = {
                "hits": 0,
                "misses": 0,
                "negative_hits": 0,
                "stale_hits": 0,
            }
        self._per_host[counter_key][field] += 1

    def _record_error(
        self, hostname: str, address_family: int, error_kind: str
    ) -> None:
        error_key = (hostname, address_family, error_kind)
        if len(self._resolution_errors) >= self._MAX_TRACKED_HOSTS:
            return
        self._resolution_errors[error_key] = (
            self._resolution_errors.get(error_key, 0) + 1
        )

    @property
    def entries(self) -> dict[str, int]:
        positive = sum(
            1 for v in self._cache.values() if isinstance(v, PositiveCacheEntry)
        )
        return {"positive": positive, "negative": len(self._cache) - positive}

    def snapshot(self) -> dict[str, object]:
        by_host: dict[str, dict[str, int]] = {}
        for (host, fam), counters in self._per_host.items():
            fam_label = "ipv4" if fam == socket.AF_INET else "ipv6"
            label = f"{host}/{fam_label}"
            by_host[label] = dict(counters)
        resolution_errors: dict[str, int] = {}
        for (host, fam, kind), count in self._resolution_errors.items():
            fam_label = "ipv4" if fam == socket.AF_INET else "ipv6"
            label = f"{host}/{fam_label}/{kind}"
            resolution_errors[label] = count
        return {
            "hits": self.hits,
            "misses": self.misses,
            "negative_hits": self.negative_hits,
            "stale_hits": self.stale_hits,
            "evictions": self.evictions,
            "size": len(self._cache),
            "entries": self.entries,
            "resolution_errors": resolution_errors,
            "by_host": by_host,
        }

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def _is_ip(self, hostname: str) -> bool:
        try:
            ipaddress.ip_address(hostname)
            return True
        except ValueError:
            return False

    def _normalize(self, hostname: str) -> str | None:
        if not hostname:
            return None
        if self._is_ip(hostname):
            return None
        return hostname.lower()

    async def resolve(
        self,
        hostname: str,
        address_family: int,
    ) -> list[str] | None:
        normalized = self._normalize(hostname)
        if normalized is None:
            return None

        key = DnsCacheKey(hostname=normalized, address_family=address_family)
        async with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                self._cache.move_to_end(key)
                now = time.monotonic()

                if isinstance(entry, PositiveCacheEntry):
                    if now < entry.expires_at:
                        self.hits += 1
                        self._record(key, "hits")
                        return entry.addresses
                    if now < entry.stale_until:
                        self.stale_hits += 1
                        self._record(key, "stale_hits")
                        return entry.addresses
                    del self._cache[key]
                else:
                    if now < entry.expires_at:
                        self.negative_hits += 1
                        self._record(key, "negative_hits")
                        raise entry.error_class(entry.error_message)
                    del self._cache[key]

            self.misses += 1
            self._record(key, "misses")

            is_owner = key not in self._singleflight
            if is_owner:
                future = asyncio.get_event_loop().create_future()
                self._singleflight[key] = future
            else:
                future = self._singleflight[key]

        if not is_owner:
            return await future

        try:
            coro = self._dns_lookup(key.hostname, key.address_family)
            if self._lookup_timeout_s is not None:
                addresses = await asyncio.wait_for(coro, timeout=self._lookup_timeout_s)
            else:
                addresses = await coro
        except TimeoutError:
            error_msg = f"DNS lookup timed out after {self._lookup_timeout_s}s"
            self._record_error(key.hostname, key.address_family, "timeout")
            self._store_negative(key, httpcore.ConnectTimeout, error_msg)
            async with self._lock:
                self._singleflight.pop(key, None)
            exc = httpcore.ConnectTimeout(error_msg)
            if not future.cancelled():
                future.set_exception(exc)
            raise exc from None
        except Exception as exc:
            async with self._lock:
                self._singleflight.pop(key, None)
            if not future.cancelled():
                future.set_exception(exc)
            raise
        async with self._lock:
            self._singleflight.pop(key, None)
        if not future.cancelled():
            future.set_result(addresses)
        return addresses

    async def _dns_lookup(
        self,
        hostname: str,
        address_family: int,
    ) -> list[str]:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                socket.getaddrinfo,
                hostname,
                None,
                address_family,
            )
            addresses: list[str] = [str(r[4][0]) for r in result]
        except socket.gaierror as exc:
            msg = str(exc)
            self._record_error(hostname, address_family, "dns_resolution")
            self._store_negative(
                DnsCacheKey(hostname=hostname, address_family=address_family),
                httpcore.ConnectError,
                msg,
            )
            raise httpcore.ConnectError(msg) from exc
        except TimeoutError as exc:
            msg = str(exc)
            self._record_error(hostname, address_family, "timeout")
            self._store_negative(
                DnsCacheKey(hostname=hostname, address_family=address_family),
                httpcore.ConnectTimeout,
                msg,
            )
            raise httpcore.ConnectTimeout(msg) from exc
        except OSError as exc:
            msg = str(exc)
            self._record_error(hostname, address_family, "os_error")
            self._store_negative(
                DnsCacheKey(hostname=hostname, address_family=address_family),
                httpcore.ConnectError,
                msg,
            )
            raise httpcore.ConnectError(msg) from exc

        if addresses:
            self._store_positive(
                DnsCacheKey(hostname=hostname, address_family=address_family),
                addresses,
            )
            return addresses
        self._record_error(hostname, address_family, "empty_response")
        self._store_negative(
            DnsCacheKey(hostname=hostname, address_family=address_family),
            httpcore.ConnectError,
        )
        return []

    def _store_positive(
        self,
        key: DnsCacheKey,
        addresses: list[str],
    ) -> None:
        now = time.monotonic()
        expires_at = now + self._config.positive_ttl_seconds
        entry = PositiveCacheEntry(
            addresses=addresses,
            expires_at=expires_at,
            stale_until=expires_at + self._config.stale_if_error_seconds,
        )
        self._evict_if_needed()
        self._cache[key] = entry
        self._cache.move_to_end(key)

    def _store_negative(
        self,
        key: DnsCacheKey,
        error_class: type[Exception],
        error_message: str = "",
    ) -> None:
        now = time.monotonic()
        entry = NegativeCacheEntry(
            error_class=error_class,
            error_message=error_message,
            expires_at=now + self._config.negative_ttl_seconds,
        )
        self._evict_if_needed()
        self._cache[key] = entry
        self._cache.move_to_end(key)

    def _evict_if_needed(self) -> None:
        while len(self._cache) >= self._config.max_entries:
            self._cache.popitem(last=False)
            self.evictions += 1


class DnsNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(
        self,
        config: DnsCacheConfig,
        wrapped: httpcore.AsyncNetworkBackend,
    ) -> None:
        self._cache = DnsCache(config)
        self._wrapped = wrapped
        self._address_family = (
            socket.AF_INET6 if config.prefer_ipv6 else socket.AF_UNSPEC
        )

    @property
    def cache(self) -> DnsCache:
        return self._cache

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: (Iterable[httpcore.SOCKET_OPTION] | None) = None,
    ) -> httpcore.AsyncNetworkStream:
        if self._cache.enabled:
            addresses = await self._cache.resolve(host, self._address_family)
            resolved = addresses[0] if addresses else host
        else:
            resolved = host
        return await self._wrapped.connect_tcp(
            resolved,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: (Iterable[httpcore.SOCKET_OPTION] | None) = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._wrapped.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._wrapped.sleep(seconds)
