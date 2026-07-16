"""Tests for bounded resource plateau checks (Milestone F12).

Verifies that:

- DNS cache has bounded capacity and reports utilisation.
- Provider client pool reports provider count.
- Stream diagnostics ring buffers are bounded.
- Missing providers degrade gracefully to ``enabled: False``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from eggpool.providers.dns_cache import DnsCache, DnsCacheKey, PositiveCacheEntry
from eggpool.request.stream_diagnostics import (
    STREAM_OUTCOME_COMPLETED,
    StreamDiagnostics,
)
from eggpool.runtime_metrics import RuntimeMetricsService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeDnsConfig:
    def __init__(
        self,
        *,
        max_entries: int = 64,
        positive_ttl_seconds: int = 300,
        negative_ttl_seconds: int = 10,
        stale_if_error_seconds: int = 60,
        enabled: bool = True,
        prefer_ipv6: bool = False,
        lookup_timeout_seconds: float | None = None,
    ) -> None:
        self.max_entries = max_entries
        self.positive_ttl_seconds = positive_ttl_seconds
        self.negative_ttl_seconds = negative_ttl_seconds
        self.stale_if_error_seconds = stale_if_error_seconds
        self.enabled = enabled
        self.prefer_ipv6 = prefer_ipv6
        self.lookup_timeout_seconds = lookup_timeout_seconds


class _FakeDnsBackend:
    def __init__(self, cache: DnsCache) -> None:
        self.cache = cache


class _FakeProviderClientPool:
    def __init__(self, providers: dict[str, Any]) -> None:
        self._providers = providers

    def snapshot(self) -> dict[str, Any]:
        return {"providers": dict(self._providers)}


class _StubConfig:
    """Minimal config stub for RuntimeMetricsService."""

    class _Server:
        threads: int = 4

    class _Database:
        path: str = ":memory:"
        wal: bool = True
        synchronous: str = "full"
        busy_timeout_ms: int = 5000
        worker_threads: int = 2

    class _Metrics:
        write_mode: str = "balanced"

    class _Routing:
        trace = SimpleNamespace(mode="off")

    server = _Server()
    database = _Database()
    metrics = _Metrics()
    routing = _Routing()


class _StubDB:
    """Minimal DB stub so snapshot does not touch a real connection."""

    def __init__(self) -> None:
        self._conn = None

    def contention_snapshot(self) -> dict[str, Any]:
        return {}

    async def execute_pragma(self, *a: Any, **kw: Any) -> list[Any]:
        return []

    async def fetch_one(self, *a: Any, **kw: Any) -> Any:
        return None


# ---------------------------------------------------------------------------
# DNS cache boundedness
# ---------------------------------------------------------------------------


class TestDnsCacheBoundedness:
    """DNS cache respects configured max_entries and reports plateau data."""

    def test_cache_never_exceeds_max_entries(self) -> None:
        config = _FakeDnsConfig(max_entries=4)
        cache = DnsCache(config)
        for i in range(10):
            cache._cache[
                DnsCacheKey(hostname=f"host{i}.example.com", address_family=0)
            ] = PositiveCacheEntry(
                addresses=["127.0.0.1"],
                expires_at=999999.0,
                stale_until=999999.0,
            )
            if len(cache._cache) > config.max_entries:
                cache._evict_if_needed()
        assert len(cache._cache) <= config.max_entries

    def test_snapshot_reports_max_entries_and_size(self) -> None:
        config = _FakeDnsConfig(max_entries=32)
        cache = DnsCache(config)
        cache._cache[DnsCacheKey(hostname="a.example.com", address_family=0)] = (
            PositiveCacheEntry(
                addresses=["1.2.3.4"],
                expires_at=999999.0,
                stale_until=999999.0,
            )
        )
        snap = cache.snapshot()
        assert snap["max_entries"] == 32
        assert snap["size"] == 1

    def test_plateau_utilisation_percentage(self) -> None:
        config = _FakeDnsConfig(max_entries=100)
        cache = DnsCache(config)
        for i in range(25):
            cache._cache[DnsCacheKey(hostname=f"h{i}.com", address_family=0)] = (
                PositiveCacheEntry(
                    addresses=["127.0.0.1"],
                    expires_at=999999.0,
                    stale_until=999999.0,
                )
            )
        backend = _FakeDnsBackend(cache)
        assert backend.cache.snapshot()["size"] == 25
        assert backend.cache.snapshot()["max_entries"] == 100


# ---------------------------------------------------------------------------
# Client pool plateau
# ---------------------------------------------------------------------------


class TestClientPoolPlateau:
    """Provider client pool reports bounded provider count."""

    def test_empty_pool(self) -> None:
        pool = _FakeProviderClientPool({})
        snap = pool.snapshot()
        assert snap["providers"] == {}

    def test_multiple_providers(self) -> None:
        pool = _FakeProviderClientPool(
            {"openai": SimpleNamespace(), "anthropic": SimpleNamespace()}
        )
        snap = pool.snapshot()
        assert len(snap["providers"]) == 2
        assert "openai" in snap["providers"]
        assert "anthropic" in snap["providers"]


# ---------------------------------------------------------------------------
# Stream diagnostics ring buffers
# ---------------------------------------------------------------------------


class TestStreamDiagnosticsRingBufferBoundedness:
    """Ring histogram buffers do not grow beyond configured capacity."""

    def test_completed_histogram_bounded(self) -> None:
        sd = StreamDiagnostics(histogram_capacity=10)
        for i in range(50):
            sd.record_outcome(STREAM_OUTCOME_COMPLETED, elapsed_ms=i)
        snap = sd.snapshot()
        completed = snap["completed_ms"]
        assert completed["sample_count"] <= 10
        assert completed["max_ms"] is not None

    def test_client_cancel_histogram_bounded(self) -> None:
        sd = StreamDiagnostics(histogram_capacity=8)
        for i in range(30):
            sd.record_outcome(
                "client_cancelled",
                elapsed_ms=i * 10,
            )
        snap = sd.snapshot()
        cancel = snap["client_cancel_ms"]
        assert cancel["sample_count"] <= 8

    def test_finalizer_timeout_histogram_bounded(self) -> None:
        sd = StreamDiagnostics(histogram_capacity=5)
        for i in range(20):
            sd.record_finalizer_timeout(elapsed_ms=i * 100)
        snap = sd.snapshot()
        ft = snap["finalizer_timeout_ms"]
        assert ft["sample_count"] <= 5

    def test_histogram_capacity_zero_samples(self) -> None:
        sd = StreamDiagnostics(histogram_capacity=10)
        snap = sd.snapshot()
        assert snap["completed_ms"]["sample_count"] == 0
        assert snap["client_cancel_ms"]["sample_count"] == 0
        assert snap["finalizer_timeout_ms"]["sample_count"] == 0


# ---------------------------------------------------------------------------
# RuntimeMetricsService resource_plateaus integration
# ---------------------------------------------------------------------------


class TestResourcePlateausSnapshot:
    """Verify the resource_plateaus section from RuntimeMetricsService."""

    def _make_service(self, **kwargs: Any) -> RuntimeMetricsService:
        """Build a minimal RuntimeMetricsService with only plateau deps."""
        defaults: dict[str, Any] = {
            "config": _StubConfig(),
            "db": _StubDB(),
            "stats_db": None,
            "supervisor": None,
            "task_monitor": None,
            "router": None,
            "health_manager": None,
            "started_monotonic": 0.0,
            "started_epoch": 0.0,
        }
        defaults.update(kwargs)
        return RuntimeMetricsService(**defaults)

    def test_plateaus_section_present_with_no_backends(self) -> None:
        svc = self._make_service()
        probe_errors: list[str] = []
        plateaus = svc._snapshot_resource_plateaus(probe_errors)
        assert "resource_plateaus" not in plateaus  # key is nested
        assert plateaus["dns_cache"]["enabled"] is False
        assert plateaus["provider_client_pool"]["enabled"] is False
        assert plateaus["stream_diagnostics"]["enabled"] is False

    def test_plateaus_with_dns_backend(self) -> None:
        config = _FakeDnsConfig(max_entries=128)
        cache = DnsCache(config)
        cache._cache[DnsCacheKey(hostname="a.com", address_family=0)] = (
            PositiveCacheEntry(
                addresses=["1.2.3.4"],
                expires_at=999999.0,
                stale_until=999999.0,
            )
        )
        backend = _FakeDnsBackend(cache)
        svc = self._make_service(dns_backend=backend)
        probe_errors: list[str] = []
        plateaus = svc._snapshot_resource_plateaus(probe_errors)
        dns = plateaus["dns_cache"]
        assert dns["enabled"] is True
        assert dns["max_entries"] == 128
        assert dns["current_size"] == 1
        assert dns["utilisation_pct"] is not None
        assert dns["utilisation_pct"] < 1.0

    def test_plateaus_with_client_pool(self) -> None:
        pool = _FakeProviderClientPool({"openai": 1, "anthropic": 2})
        svc = self._make_service(provider_client_pool=pool)
        probe_errors: list[str] = []
        plateaus = svc._snapshot_resource_plateaus(probe_errors)
        cpp = plateaus["provider_client_pool"]
        assert cpp["enabled"] is True
        assert cpp["provider_count"] == 2
        assert set(cpp["providers"]) == {"openai", "anthropic"}

    def test_plateaus_with_stream_diagnostics(self) -> None:
        sd = StreamDiagnostics(histogram_capacity=128)
        sd.record_outcome(STREAM_OUTCOME_COMPLETED, elapsed_ms=42)
        sd.record_outcome("client_cancelled", elapsed_ms=10)
        sd.record_finalizer_timeout(elapsed_ms=500)
        svc = self._make_service(stream_diagnostics=sd)
        probe_errors: list[str] = []
        plateaus = svc._snapshot_resource_plateaus(probe_errors)
        sdp = plateaus["stream_diagnostics"]
        assert sdp["enabled"] is True
        assert sdp["completed_histogram_capacity"] == 256
        assert sdp["completed_histogram_samples"] >= 1
        assert sdp["client_cancel_histogram_samples"] >= 1
        assert sdp["finalizer_timeout_histogram_samples"] >= 1

    def test_plateaus_no_probe_errors_on_missing_deps(self) -> None:
        svc = self._make_service()
        probe_errors: list[str] = []
        svc._snapshot_resource_plateaus(probe_errors)
        assert probe_errors == []
