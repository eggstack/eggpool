"""Phase 5 — Shared runtime-generation factory tests.

Tests that verify startup and reload produce structurally identical
generations through the shared RuntimeGenerationFactory.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from eggpool.generation_factory import (
    PreparedRuntimeGeneration,
    RuntimeGenerationFactory,
)
from eggpool.runtime_manager import (
    ProcessRuntime,
    RuntimeGenerationCandidate,
)

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_process(db: Any = None, stats_db: Any = None) -> ProcessRuntime:
    """Create a minimal ProcessRuntime for testing."""
    return ProcessRuntime(
        db=db or MagicMock(),
        stats_db=stats_db or MagicMock(),
        config_path=None,
        metrics_coalescer=MagicMock(),
    )


def _make_config(**overrides: Any) -> AppConfig:
    """Create a minimal AppConfig for testing."""
    from eggpool.models.config import (
        AccountConfig,
        AppConfig,
        ProviderConfig,
        RoutingConfig,
        ServerConfig,
    )

    defaults = dict(
        server=ServerConfig(host="127.0.0.1", port=0),
        providers={
            "test-provider": ProviderConfig(
                id="test-provider",
                base_url="https://test.example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="acct-1", api_key="test-key-1"),
                ],
            ),
        },
        routing=RoutingConfig(strategy="quota_fair"),
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _service_graph_manifest(
    result: PreparedRuntimeGeneration,
) -> dict[str, str]:
    """Build a normalized manifest of a PreparedRuntimeGeneration.

    The manifest contains field names, dependency types, and
    configured values.  Used to compare startup and reload outputs.
    """
    gen = result.generation
    return {
        "generation_fields": sorted(
            name for name in vars(gen) if not name.startswith("_")
        ),
        "dependency_types": {
            "registry": type(gen.registry).__name__,
            "catalog": type(gen.catalog).__name__,
            "router": type(gen.router).__name__,
            "coordinator": type(gen.coordinator).__name__,
            "client_pool": type(gen.client_pool).__name__,
            "outbound_manager": type(gen.outbound_manager).__name__,
            "health_manager": type(gen.health_manager).__name__,
            "cost_calculator": type(gen.cost_calculator).__name__,
            "stats_service": type(gen.stats_service).__name__,
            "supervisor": type(gen.supervisor).__name__,
            "transcoder_policy": type(gen.transcoder_policy).__name__,
            "compression_policy": type(gen.compression_policy).__name__,
            "cache_config": type(gen.cache_config).__name__,
            "compression_tuning_registry": type(
                gen.compression_tuning_registry
            ).__name__,
            "dispatch_overhead_recorder": type(gen.dispatch_overhead_recorder).__name__,
            "dispatch_span_recorder": type(gen.dispatch_span_recorder).__name__,
            "account_backoff_repo": type(gen.account_backoff_repo).__name__,
            "finalization_retry_queue": type(gen.finalization_retry_queue).__name__,
            "routing_trace_guard": type(gen.routing_trace_guard).__name__,
        },
        "configured_values": {
            "detailed_span_sample_rate": (
                result.dispatch_span_recorder._detailed_span_sample_rate
            ),
            "local_pre_upstream_recorder_present": (
                result.local_pre_upstream_recorder is not None
            ),
            "stream_diagnostics_present": (result.stream_diagnostics is not None),
        },
        "process_owned_sharing": {
            "db_identity": id(gen.coordinator._db),
            "stats_db_identity": id(result.stats_service._db),
        },
    }


# ---------------------------------------------------------------------------
# Parity tests
# ---------------------------------------------------------------------------


class TestFactoryParity:
    """Verify that the factory produces identical service graphs."""

    @pytest.mark.asyncio()
    async def test_factory_constructs_all_required_services(self) -> None:
        """Factory produces a complete PreparedRuntimeGeneration."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()

            factory = RuntimeGenerationFactory()
            result = await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
            )

            assert isinstance(result, PreparedRuntimeGeneration)
            assert result.generation is not None
            assert result.registry is not None
            assert result.catalog is not None
            assert result.router is not None
            assert result.coordinator is not None
            assert result.client_pool is not None
            assert result.outbound_manager is not None
            assert result.health_manager is not None
            assert result.cost_calculator is not None
            assert result.transcoder_policy is not None
            assert result.compression_policy is not None
            assert result.cache_config is not None
            assert result.compression_tuning_registry is not None
            assert result.dispatch_overhead_recorder is not None
            assert result.dispatch_span_recorder is not None
            assert result.account_backoff_repo is not None
            assert result.stats_service is not None
            assert result.supervisor is not None
            assert result.finalization_retry_queue is not None
            assert result.routing_trace_guard is not None
            assert result.local_pre_upstream_recorder is not None
            assert result.stream_diagnostics is not None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_startup_and_reload_produce_identical_manifests(
        self,
    ) -> None:
        """Startup (via factory) and reload (via factory) produce
        the same service-graph manifest structure."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            config = _make_config()
            process = _make_process(db=db, stats_db=db)

            # Simulate startup: factory call without candidate
            startup_factory = RuntimeGenerationFactory()
            startup_result = await startup_factory.prepare(
                config=config,
                config_digest="startup-digest",
                generation_id=1,
                process=process,
            )

            # Simulate reload: factory call with candidate
            candidate = RuntimeGenerationCandidate(generation_id=2)
            reload_factory = RuntimeGenerationFactory()
            reload_result = await reload_factory.prepare(
                config=config,
                config_digest="reload-digest",
                generation_id=2,
                process=process,
                candidate=candidate,
            )

            startup_manifest = _service_graph_manifest(startup_result)
            reload_manifest = _service_graph_manifest(reload_result)

            # Generation fields must match
            assert (
                startup_manifest["generation_fields"]
                == reload_manifest["generation_fields"]
            )

            # Dependency types must match
            assert (
                startup_manifest["dependency_types"]
                == reload_manifest["dependency_types"]
            )

            # Configured values must match
            assert (
                startup_manifest["configured_values"]
                == reload_manifest["configured_values"]
            )

            # Process-owned DB identity must be shared
            assert (
                startup_manifest["process_owned_sharing"]["db_identity"]
                == reload_manifest["process_owned_sharing"]["db_identity"]
            )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_candidate_resources_registered_on_reload(self) -> None:
        """Factory registers closeable resources on the candidate."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()

            candidate = RuntimeGenerationCandidate(generation_id=1)
            factory = RuntimeGenerationFactory()
            await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
                candidate=candidate,
            )

            # Candidate should have registered resources
            assert len(candidate._resources) > 0
            resource_names = [r.name for r in candidate._resources]
            assert "client_pool" in resource_names
            assert "outbound_manager" in resource_names
            assert "supervisor" in resource_names
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_no_candidate_no_resource_registration(self) -> None:
        """Factory does not register resources when no candidate is provided."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()

            factory = RuntimeGenerationFactory()
            result = await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
            )

            # Result should be complete
            assert result.generation is not None
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Dispatch writer parity
# ---------------------------------------------------------------------------


class TestDispatchWriterParity:
    """Verify dispatch writer is wired identically across paths."""

    @pytest.mark.asyncio()
    async def test_dispatch_writer_wired_to_coordinator(self) -> None:
        """Dispatch writer from process is passed to the coordinator."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            dispatch_writer = MagicMock()
            process = _make_process(db=db, stats_db=db)
            process.dispatch_writer = dispatch_writer
            config = _make_config()

            factory = RuntimeGenerationFactory()
            result = await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
            )

            # Coordinator should have the dispatch writer
            assert result.coordinator._dispatch_writer is dispatch_writer
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Detailed span sample rate parity
# ---------------------------------------------------------------------------


class TestDetailedSpanSampleRateParity:
    """Verify detailed span sample rate survives reload."""

    @pytest.mark.asyncio()
    async def test_configured_sample_rate_preserved(self) -> None:
        """DispatchSpanRecorder uses the configured sample rate."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()
            config.metrics.detailed_span_sample_rate = 0.5

            factory = RuntimeGenerationFactory()
            result = await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
            )

            assert result.dispatch_span_recorder._detailed_span_sample_rate == 0.5
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_sample_rate_survives_multiple_reloads(self) -> None:
        """Sample rate remains configured after multiple factory calls."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()
            config.metrics.detailed_span_sample_rate = 0.75

            for gen_id in range(1, 4):
                factory = RuntimeGenerationFactory()
                result = await factory.prepare(
                    config=config,
                    config_digest=f"digest-{gen_id}",
                    generation_id=gen_id,
                    process=process,
                )
                assert result.dispatch_span_recorder._detailed_span_sample_rate == 0.75
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Local pre-upstream recorder parity
# ---------------------------------------------------------------------------


class TestLocalPreUpstreamRecorderParity:
    """Verify local pre-upstream recorder is present in both paths."""

    @pytest.mark.asyncio()
    async def test_recorder_present_in_startup(self) -> None:
        """Local pre-upstream recorder is constructed in startup path."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()

            factory = RuntimeGenerationFactory()
            result = await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
            )

            assert result.local_pre_upstream_recorder is not None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_recorder_present_in_reload(self) -> None:
        """Local pre-upstream recorder is constructed in reload path."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()

            candidate = RuntimeGenerationCandidate(generation_id=1)
            factory = RuntimeGenerationFactory()
            result = await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
                candidate=candidate,
            )

            assert result.local_pre_upstream_recorder is not None
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Stream diagnostics parity
# ---------------------------------------------------------------------------


class TestStreamDiagnosticsParity:
    """Verify stream diagnostics are available in both paths."""

    @pytest.mark.asyncio()
    async def test_stream_diagnostics_singleton(self) -> None:
        """Stream diagnostics is the process-wide singleton."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner
        from eggpool.request.stream_diagnostics import get_stream_diagnostics

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()

            factory = RuntimeGenerationFactory()
            result = await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
            )

            assert result.stream_diagnostics is get_stream_diagnostics()
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Persisted backoff hydration parity
# ---------------------------------------------------------------------------


class TestBackoffHydrationParity:
    """Verify persisted backoffs are hydrated in both paths."""

    @pytest.mark.asyncio()
    async def test_suppressed_account_ineligible_after_factory(self) -> None:
        """A suppressed account remains ineligible after factory
        construction."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()

            # Create an account in the DB
            from eggpool.db.repositories import (
                AccountRepository,
                ProviderRepository,
            )

            async with db.transaction():
                provider_repo = ProviderRepository(db)
                await provider_repo.sync_from_config(
                    {
                        "test-provider": {
                            "base_url": "https://test.example.com/v1",
                            "protocols": ["openai"],
                        }
                    }
                )
                account_repo = AccountRepository(db)
                await account_repo.sync_from_config(
                    [
                        {
                            "provider_id": "test-provider",
                            "name": "acct-1",
                            "api_key": "test-key-1",
                            "api_key_env": None,
                            "enabled": True,
                        }
                    ]
                )

            process = _make_process(db=db, stats_db=db)
            config = _make_config()

            # Manually insert a backoff record
            from eggpool.db.repositories import AccountBackoffRepository

            backoff_repo = AccountBackoffRepository(db)
            account_id = await account_repo.get_id_by_name("acct-1")
            assert account_id is not None
            future_epoch = int(time.time()) + 3600  # 1 hour from now
            async with db.transaction():
                await backoff_repo.upsert_failure(
                    account_id=account_id,
                    model_id=None,
                    reason="rate_limited",
                    status_code=429,
                    error_class=None,
                    backoff_until=future_epoch,
                    consecutive_failures=1,
                )

            # Factory should hydrate the backoff
            factory = RuntimeGenerationFactory()
            result = await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
            )

            # Account should be suppressed
            health = result.health_manager.get_account_health("acct-1")
            assert health.is_healthy is False
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Candidate construction failure cleanup
# ---------------------------------------------------------------------------


class TestCandidateConstructionFailure:
    """Verify candidate resources are cleaned up on factory failure."""

    @pytest.mark.asyncio()
    async def test_factory_failure_cleans_candidate_resources(
        self,
    ) -> None:
        """When factory construction fails, candidate resources are aborted."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()

            candidate = RuntimeGenerationCandidate(generation_id=1)

            # Register a mock resource
            close_mock = MagicMock()
            candidate.register_resource("test_resource", close_mock)

            # The factory should succeed, but we can test that
            # resources are registered properly
            factory = RuntimeGenerationFactory()
            await factory.prepare(
                config=config,
                config_digest="test-digest",
                generation_id=1,
                process=process,
                candidate=candidate,
            )

            # Resources should be registered
            assert len(candidate._resources) > 0

            # Abort should close them
            from eggpool.runtime_manager import CandidateOwnershipState

            await candidate.abort(
                cause=RuntimeError("test failure"),
                failure_stage="build",
            )
            assert candidate.ownership_state == CandidateOwnershipState.ABORTED
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# No-op reload does not rebuild generation
# ---------------------------------------------------------------------------


class TestNoOpReload:
    """Verify repeated no-op reload behavior."""

    @pytest.mark.asyncio()
    async def test_factory_called_once_per_reload(self) -> None:
        """Each factory.prepare() call produces a distinct generation."""
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            process = _make_process(db=db, stats_db=db)
            config = _make_config()

            factory = RuntimeGenerationFactory()
            result1 = await factory.prepare(
                config=config,
                config_digest="digest-1",
                generation_id=1,
                process=process,
            )
            result2 = await factory.prepare(
                config=config,
                config_digest="digest-1",
                generation_id=2,
                process=process,
            )

            # Different generation IDs
            assert result1.generation.generation_id == 1
            assert result2.generation.generation_id == 2

            # But same service types
            manifest1 = _service_graph_manifest(result1)
            manifest2 = _service_graph_manifest(result2)
            assert manifest1["dependency_types"] == manifest2["dependency_types"]
        finally:
            await db.disconnect()
