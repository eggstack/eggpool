"""Bounded in-memory DNS cache with singleflight deduplication."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import ipaddress
import logging
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpcore

if TYPE_CHECKING:
    from collections.abc import Iterable

    from eggpool.models.config import DnsCacheConfig

logger = logging.getLogger(__name__)


def _address_family_label(family: int) -> str:
    """Return a human-readable label for a socket address family."""
    if family == socket.AF_INET:
        return "ipv4"
    if family == socket.AF_INET6:
        return "ipv6"
    return "any" if family == socket.AF_UNSPEC else f"family_{family}"


def _consume_future_exception(future: asyncio.Future[list[str] | None]) -> None:
    """Mark singleflight exceptions observed when no waiter consumes them."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        future.exception()


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
        # Logical counters (backward-compatible)
        self.hits = 0
        self.misses = 0  # legacy: now equals cache_misses_owner
        self.negative_hits = 0
        self.stale_hits = 0
        self.evictions = 0
        # New precise counters
        self.cache_hits = 0
        self.cache_misses_owner = 0
        self.singleflight_waits = 0
        self.resolver_calls = 0
        self.resolver_successes = 0
        self.resolver_errors = 0
        self._evictions_by_reason: dict[str, int] = {"capacity": 0, "ttl_expiry": 0}
        self._per_host: dict[tuple[str, int], dict[str, int]] = {}
        self._resolution_errors: dict[tuple[str, int, str], int] = {}
        self._lookup_timeout_s: float | None = (
            float(config.lookup_timeout_seconds)
            if config.lookup_timeout_seconds
            else None
        )
        logger.debug(
            "DNS cache init: enabled=%s max_entries=%d positive_ttl=%ds "
            "negative_ttl=%ds stale_if_error=%ds",
            config.enabled,
            config.max_entries,
            config.positive_ttl_seconds,
            config.negative_ttl_seconds,
            config.stale_if_error_seconds,
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
            fam_label = _address_family_label(fam)
            label = f"{host}/{fam_label}"
            by_host[label] = dict(counters)
        resolution_errors: dict[str, int] = {}
        for (host, fam, kind), count in self._resolution_errors.items():
            fam_label = _address_family_label(fam)
            label = f"{host}/{fam_label}/{kind}"
            resolution_errors[label] = count
        hosts = self._snapshot_hosts()
        # Derived metrics
        logical_calls = (
            self.cache_hits
            + self.cache_misses_owner
            + self.singleflight_waits
            + self.negative_hits
            + self.stale_hits
        )
        hit_denom = max(1, self.cache_hits + self.cache_misses_owner)
        cache_hit_rate = self.cache_hits / hit_denom
        dns_suppression_rate = (
            self.cache_hits
            + self.singleflight_waits
            + self.negative_hits
            + self.stale_hits
        ) / max(1, logical_calls)
        resolver_calls_per_logical = self.resolver_calls / max(1, logical_calls)
        # Worst missers: top hosts by owner misses or resolver calls
        worst: list[dict[str, object]] = []
        now_mono = time.monotonic()
        for (host, fam), counters in self._per_host.items():
            owner_misses = counters.get("misses", 0)
            if owner_misses > 0:
                hits_count = counters.get("hits", 0)
                sf_waits = owner_misses  # approximate from per-host misses
                entry = self._cache.get(DnsCacheKey(hostname=host, address_family=fam))
                expires_in = -1.0
                if entry is not None and isinstance(entry, PositiveCacheEntry):
                    expires_in = max(0.0, entry.expires_at - now_mono)
                worst.append(
                    {
                        "host": host,
                        "family": _address_family_label(fam),
                        "owner_misses": owner_misses,
                        "resolver_calls": owner_misses,
                        "hits": hits_count,
                        "singleflight_waits": sf_waits,
                        "expires_in_seconds": round(expires_in, 1),
                    }
                )
        worst.sort(key=lambda w: w["owner_misses"], reverse=True)  # type: ignore[no-any-return]
        worst_missers = worst[:20]
        return {
            "max_entries": self._config.max_entries,
            # Legacy fields (backward-compatible)
            "hits": self.hits,
            "misses": self.misses,
            "negative_hits": self.negative_hits,
            "stale_hits": self.stale_hits,
            "evictions": self.evictions,
            "evictions_by_reason": dict(self._evictions_by_reason),
            "size": len(self._cache),
            "entries": self.entries,
            "resolution_errors": resolution_errors,
            "by_host": by_host,
            "hosts": hosts,
            # New precise counters
            "cache_hits_total": self.cache_hits,
            "cache_misses_owner_total": self.cache_misses_owner,
            "singleflight_waits_total": self.singleflight_waits,
            "negative_hits_total": self.negative_hits,
            "stale_hits_total": self.stale_hits,
            "resolver_calls_total": self.resolver_calls,
            "resolver_successes_total": self.resolver_successes,
            "resolver_errors_total": self.resolver_errors,
            # Derived rates
            "cache_hit_rate": round(cache_hit_rate, 4),
            "dns_suppression_rate": round(dns_suppression_rate, 4),
            "resolver_calls_per_logical_resolve": round(resolver_calls_per_logical, 4),
            # Diagnostics
            "worst_missers": worst_missers,
        }

    def _snapshot_hosts(self) -> list[dict[str, object]]:
        """Build per-host entry metadata for diagnostics."""
        now = time.monotonic()
        hosts: list[dict[str, object]] = []
        for key, entry in self._cache.items():
            fam_label = _address_family_label(key.address_family)
            if isinstance(entry, PositiveCacheEntry):
                expires_in = max(0.0, entry.expires_at - now)
                stale_available = now < entry.stale_until
                hosts.append(
                    {
                        "host": key.hostname,
                        "family": fam_label,
                        "state": "positive",
                        "expires_in_seconds": round(expires_in, 1),
                        "stale_available": stale_available,
                        "last_error_kind": None,
                    }
                )
            else:
                expires_in = max(0.0, entry.expires_at - now)
                error_kind = entry.error_class.__name__ if entry.error_class else None
                hosts.append(
                    {
                        "host": key.hostname,
                        "family": fam_label,
                        "state": "negative",
                        "expires_in_seconds": round(expires_in, 1),
                        "stale_available": False,
                        "last_error_kind": error_kind,
                    }
                )
        return hosts

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
        stale_fallback: PositiveCacheEntry | None = None
        async with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                self._cache.move_to_end(key)
                now = time.monotonic()

                if isinstance(entry, PositiveCacheEntry):
                    if now < entry.expires_at:
                        self.hits += 1
                        self.cache_hits += 1
                        self._record(key, "hits")
                        return entry.addresses
                    if now < entry.stale_until:
                        stale_fallback = entry
                        del self._cache[key]
                    else:
                        del self._cache[key]
                        self._evictions_by_reason["ttl_expiry"] += 1
                else:
                    if now < entry.expires_at:
                        self.negative_hits += 1
                        self._record(key, "negative_hits")
                        raise entry.error_class(entry.error_message)
                    del self._cache[key]
                    self._evictions_by_reason["ttl_expiry"] += 1

            # Determine singleflight ownership AFTER cache lookup
            is_owner = key not in self._singleflight
            if is_owner:
                self.cache_misses_owner += 1
                self.misses += 1  # legacy compatibility
                self._record(key, "misses")
                future = asyncio.get_event_loop().create_future()
                future.add_done_callback(_consume_future_exception)
                self._singleflight[key] = future
            else:
                self.singleflight_waits += 1
                future = self._singleflight[key]

        if not is_owner:
            return await future

        try:
            self.resolver_calls += 1
            coro = self._dns_lookup(key.hostname, key.address_family)
            if self._lookup_timeout_s is not None:
                addresses = await asyncio.wait_for(coro, timeout=self._lookup_timeout_s)
            else:
                addresses = await coro
        except TimeoutError:
            self.resolver_errors += 1
            error_msg = f"DNS lookup timed out after {self._lookup_timeout_s}s"
            self._record_error(key.hostname, key.address_family, "timeout")
            self._store_negative(key, httpcore.ConnectTimeout, error_msg)
            logger.debug(
                "DNS resolver error for %s: timeout after %ss",
                key.hostname,
                self._lookup_timeout_s,
            )
            async with self._lock:
                self._singleflight.pop(key, None)
                fallback_addresses = self._restore_stale_fallback(key, stale_fallback)
            if fallback_addresses is not None:
                if not future.cancelled():
                    future.set_result(fallback_addresses)
                return fallback_addresses
            exc = httpcore.ConnectTimeout(error_msg)
            if not future.cancelled():
                future.set_exception(exc)
            raise exc from None
        except Exception as exc:
            self.resolver_errors += 1
            async with self._lock:
                self._singleflight.pop(key, None)
                fallback_addresses = self._restore_stale_fallback(key, stale_fallback)
            if fallback_addresses is not None:
                logger.debug(
                    "DNS cache stale-if-error hit for %s (%s)",
                    key.hostname,
                    _address_family_label(key.address_family),
                )
                if not future.cancelled():
                    future.set_result(fallback_addresses)
                return fallback_addresses
            if not future.cancelled():
                future.set_exception(exc)
            raise
        self.resolver_successes += 1
        async with self._lock:
            self._singleflight.pop(key, None)
        if not future.cancelled():
            future.set_result(addresses)
        return addresses

    def _restore_stale_fallback(
        self,
        key: DnsCacheKey,
        fallback: PositiveCacheEntry | None,
    ) -> list[str] | None:
        if fallback is None:
            return None
        self.stale_hits += 1
        self._record(key, "stale_hits")
        self._cache.pop(key, None)
        self._cache[key] = fallback
        self._cache.move_to_end(key)
        return fallback.addresses

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
            logger.debug("DNS resolver error for %s: %s", hostname, msg)
            raise httpcore.ConnectError(msg) from exc
        except TimeoutError as exc:
            msg = str(exc)
            self._record_error(hostname, address_family, "timeout")
            self._store_negative(
                DnsCacheKey(hostname=hostname, address_family=address_family),
                httpcore.ConnectTimeout,
                msg,
            )
            logger.debug("DNS resolver timeout for %s: %s", hostname, msg)
            raise httpcore.ConnectTimeout(msg) from exc
        except OSError as exc:
            msg = str(exc)
            self._record_error(hostname, address_family, "os_error")
            self._store_negative(
                DnsCacheKey(hostname=hostname, address_family=address_family),
                httpcore.ConnectError,
                msg,
            )
            logger.debug("DNS resolver OS error for %s: %s", hostname, msg)
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
            self._evictions_by_reason["capacity"] += 1
        if self.evictions > 0 and self.evictions % 10 == 0:
            logger.debug("DNS cache: %d total evictions", self.evictions)


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
