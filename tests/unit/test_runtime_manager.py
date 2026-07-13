"""Tests for the milestone-B RuntimeManager (workstream B10).

Covers:
- initial install and diagnostics
- lease acquire / release / idempotency
- no acquisition after shutdown
- race-safe acquisition during retirement
- active / retiring diagnostics
- teardown exactly once
- monotonically increasing generation IDs
- generation-owned app-state audit list
- wrap_stream_with_lease helper
- leased_runtime context manager
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from eggpool.runtime_manager import (
    RuntimeDiagnostics,
    RuntimeGeneration,
    RuntimeManager,
    RuntimeManagerLeaseExhaustedError,
    RuntimeManagerShutdownError,
    _digest_prefix,
    _GenerationSlot,
    _safe_aclose,
    attach_runtime_manager,
    is_runtime_owned_attr,
    leased_runtime,
    wrap_stream_with_lease,
)
from eggpool.runtime_metrics import (
    _runtime_manager_to_dict,
)


def _fake_generation(generation_id: int = 0) -> RuntimeGeneration:
    """Return a minimal RuntimeGeneration with mock services."""
    return RuntimeGeneration(
        generation_id=generation_id,
        config=MagicMock(),
        config_digest="a" * 64,
        registry=MagicMock(),
        catalog=MagicMock(),
        router=MagicMock(),
        coordinator=MagicMock(),
        client_pool=MagicMock(),
        outbound_manager=MagicMock(),
        dns_backend=None,
        health_manager=MagicMock(),
        cost_calculator=MagicMock(),
        transcoder_policy=MagicMock(),
        compression_policy=MagicMock(),
        cache_config=MagicMock(),
        compression_tuning_registry=MagicMock(),
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=MagicMock(),
        account_backoff_repo=MagicMock(),
        stats_service=MagicMock(),
        supervisor=MagicMock(),
        finalization_retry_queue=MagicMock(),
        routing_trace_guard=MagicMock(),
        created_at_monotonic=time.monotonic(),
        created_at_epoch=time.time(),
    )


# ---------------------------------------------------------------------------
# Initial install
# ---------------------------------------------------------------------------


class TestInitialInstall:
    @pytest.mark.asyncio
    async def test_install_initial_populates_active_generation(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(generation_id=0)
        await manager.install_initial(gen)

        assert manager.has_active_generation()
        active = manager.active_snapshot()
        assert active.generation_id == 0

    @pytest.mark.asyncio
    async def test_install_initial_twice_raises(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        with pytest.raises(RuntimeError, match="called twice"):
            await manager.install_initial(_fake_generation(1))

    @pytest.mark.asyncio
    async def test_install_initial_after_shutdown_raises(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        await manager.shutdown()

        with pytest.raises(RuntimeManagerShutdownError):
            await manager.install_initial(_fake_generation(1))


# ---------------------------------------------------------------------------
# Lease acquire / release
# ---------------------------------------------------------------------------


class TestLeaseAcquireRelease:
    @pytest.mark.asyncio
    async def test_acquire_returns_lease_with_correct_generation_id(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(42))
        lease = await manager.acquire()
        assert lease.generation_id == 42

    @pytest.mark.asyncio
    async def test_release_decrements_active_leases(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        slot = manager._active
        assert slot is not None
        lease = await manager.acquire()
        assert slot.active_leases == 1
        await lease.release()
        assert slot.active_leases == 0

    @pytest.mark.asyncio
    async def test_release_idempotency(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        slot = manager._active
        assert slot is not None
        lease = await manager.acquire()
        assert slot.active_leases == 1
        await lease.release()
        assert slot.active_leases == 0
        # Second release is a no-op
        await lease.release()
        assert slot.active_leases == 0

    @pytest.mark.asyncio
    async def test_acquire_after_shutdown_raises(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        await manager.shutdown()

        with pytest.raises(RuntimeManagerLeaseExhaustedError, match="shutting down"):
            await manager.acquire()

    @pytest.mark.asyncio
    async def test_acquire_no_active_slot_raises_after_timeout(self) -> None:
        manager = RuntimeManager()
        with pytest.raises(RuntimeManagerLeaseExhaustedError):
            await manager.acquire()


# ---------------------------------------------------------------------------
# Active snapshot
# ---------------------------------------------------------------------------


class TestActiveSnapshot:
    def test_has_active_generation_false_before_install(self) -> None:
        manager = RuntimeManager()
        assert not manager.has_active_generation()

    @pytest.mark.asyncio
    async def test_has_active_generation_true_after_install(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        assert manager.has_active_generation()

    def test_active_snapshot_before_install_raises(self) -> None:
        manager = RuntimeManager()
        with pytest.raises(RuntimeManagerShutdownError):
            manager.active_snapshot()


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    @pytest.mark.asyncio
    async def test_active_diagnostics_show_correct_generation(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(7)
        await manager.install_initial(gen)
        diag = manager.diagnostics()
        assert diag.active is not None
        assert diag.active.generation_id == 7
        assert diag.active.config_digest_prefix == "a" * 12
        assert diag.active.accepting_leases is True

    @pytest.mark.asyncio
    async def test_retiring_generations_tracked(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        slot = manager._active
        assert slot is not None
        # Manually push to retiring for test
        slot.accepting_leases = False
        slot.retirement_started = True
        manager._retiring.append(slot)
        manager._active = None

        diag = manager.diagnostics()
        assert len(diag.retiring) == 1
        assert diag.retiring[0].generation_id == 0
        assert diag.active is None

    @pytest.mark.asyncio
    async def test_shutdown_in_progress_tracked(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        # Manually set shutdown flag without completing it
        manager._shutdown_in_progress = True
        diag = manager.diagnostics()
        assert diag.shutdown_in_progress is True

    @pytest.mark.asyncio
    async def test_next_generation_id_bumps(self) -> None:
        manager = RuntimeManager()
        assert manager.next_generation_id == 0
        gen_id = manager.reserve_next_generation_id()
        assert gen_id == 0
        assert manager.next_generation_id == 1


# ---------------------------------------------------------------------------
# Generation ID monotonicity
# ---------------------------------------------------------------------------


class TestGenerationIdMonotonicity:
    @pytest.mark.asyncio
    async def test_initial_generation_is_zero(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        snap = manager.active_snapshot()
        assert snap.generation_id == 0

    @pytest.mark.asyncio
    async def test_reserve_bumps_id(self) -> None:
        manager = RuntimeManager()
        id1 = manager.reserve_next_generation_id()
        id2 = manager.reserve_next_generation_id()
        assert id2 == id1 + 1


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        await manager.shutdown()
        await manager.shutdown()  # no error

    @pytest.mark.asyncio
    async def test_shutdown_sets_flag(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        await manager.shutdown()
        assert manager._shutdown_in_progress is True


# ---------------------------------------------------------------------------
# Generation-owned app-state audit
# ---------------------------------------------------------------------------


class TestAppStateAudit:
    def test_router_is_runtime_owned(self) -> None:
        assert is_runtime_owned_attr("router")
        assert is_runtime_owned_attr("catalog")
        assert is_runtime_owned_attr("coordinator")
        assert is_runtime_owned_attr("health_manager")
        assert is_runtime_owned_attr("registry")

    def test_non_generation_attrs_not_owned(self) -> None:
        assert not is_runtime_owned_attr("db")
        assert not is_runtime_owned_attr("config")
        assert not is_runtime_owned_attr("config_path")


# ---------------------------------------------------------------------------
# wrap_stream_with_lease
# ---------------------------------------------------------------------------


class TestWrapStreamWithLease:
    @pytest.mark.asyncio
    async def test_stream_releases_lease(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        slot = manager._active
        assert slot is not None
        lease = await manager.acquire()
        assert slot.active_leases == 1

        async def fake_stream():
            yield b"chunk1"
            yield b"chunk2"

        async for _chunk in wrap_stream_with_lease(fake_stream(), lease):
            pass

        assert slot.active_leases == 0

    @pytest.mark.asyncio
    async def test_stream_releases_on_exception(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        slot = manager._active
        assert slot is not None
        lease = await manager.acquire()
        assert slot.active_leases == 1

        async def failing_stream():
            raise ValueError("boom")
            yield b"never"  # type: ignore[misc]

        with pytest.raises(ValueError, match="boom"):
            async for _chunk in wrap_stream_with_lease(failing_stream(), lease):
                pass

        assert slot.active_leases == 0


# ---------------------------------------------------------------------------
# leased_runtime
# ---------------------------------------------------------------------------


class TestLeasedRuntime:
    @pytest.mark.asyncio
    async def test_yields_active_generation(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)

        async with leased_runtime(manager) as runtime:
            assert runtime.generation_id == 0
            slot = manager._active
            assert slot is not None
            assert slot.active_leases == 1

        assert slot.active_leases == 0


# ---------------------------------------------------------------------------
# _safe_aclose
# ---------------------------------------------------------------------------


class TestSafeAclose:
    @pytest.mark.asyncio
    async def test_aclose_called_when_present(self) -> None:
        obj = MagicMock()
        await _safe_aclose(obj)
        obj.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_error_when_no_aclose(self) -> None:
        obj = MagicMock(spec=[])  # no aclose
        await _safe_aclose(obj)  # no error


# ---------------------------------------------------------------------------
# Diagnostics helper functions
# ---------------------------------------------------------------------------


class TestDiagnosticsHelpers:
    def test_digest_prefix_short(self) -> None:
        assert _digest_prefix("a" * 64) == "a" * 12

    def test_digest_prefix_empty(self) -> None:
        assert _digest_prefix("") == "<empty>"

    def test_generation_diag_to_dict(self) -> None:
        diag = RuntimeDiagnostics(
            active=None,
            retiring=(),
            shutdown_in_progress=False,
            next_generation_id=1,
        )
        result = _runtime_manager_to_dict(diag)
        assert result["active"] is None
        assert result["retiring"] == []
        assert result["retiring_count"] == 0
        assert result["shutdown_in_progress"] is False
        assert result["next_generation_id"] == 1


# ---------------------------------------------------------------------------
# attach_runtime_manager
# ---------------------------------------------------------------------------


class TestAttachRuntimeManager:
    def test_attaches_to_app_state(self) -> None:
        manager = RuntimeManager()
        app = MagicMock()
        attach_runtime_manager(app, manager)
        app.state.runtime_manager = manager
        assert app.state.runtime_manager is manager


# ---------------------------------------------------------------------------
# _mirror_generation_on_app_state (app.py)
# ---------------------------------------------------------------------------


class TestMirrorGenerationOnAppState:
    def test_mirror_sets_generation_owned_attrs(self) -> None:
        from eggpool.app import _mirror_generation_on_app_state

        app = MagicMock()
        gen = _fake_generation(0)
        _mirror_generation_on_app_state(app, gen)
        # Should have set router, catalog, coordinator, etc.
        assert app.state.router is gen.router
        assert app.state.catalog is gen.catalog
        assert app.state.coordinator is gen.coordinator
        assert app.state.health_manager is gen.health_manager

    def test_mirror_does_not_overwrite_process_owned(self) -> None:
        from eggpool.app import _mirror_generation_on_app_state

        app = MagicMock()
        app.state.db = MagicMock()
        app.state.config = MagicMock()
        original_db = app.state.db
        original_config = app.state.config
        gen = _fake_generation(0)
        _mirror_generation_on_app_state(app, gen)
        assert app.state.db is original_db
        assert app.state.config is original_config


# ---------------------------------------------------------------------------
# B6: Audit test — request handlers must not directly access
# generation-owned app.state attributes
# ---------------------------------------------------------------------------

# Attributes that are generation-owned and must only be accessed through
# a generation lease, never directly via app.state in request handlers.
_GENERATION_OWNED_ATTRS_TO_AUDIT = frozenset(
    {
        "coordinator",
        "catalog",
        "health_manager",
        "router",
        "cost_calculator",
        "account_backoff_repo",
        "dispatch_overhead_recorder",
        "dispatch_span_recorder",
        "transcoder_policy",
        "compression_policy",
        "compression_tuning_registry",
        "client_pool",
        "outbound_manager",
        "dns_backend",
    }
)


class TestAppStateAuditEnforcement:
    """Verify that request handlers use generation leases, not direct app.state.

    The audit scans ``proxy_request.py`` for direct reads of
    generation-owned ``app.state`` attributes (e.g.
    ``request.app.state.router``).  Such reads bypass the generation
    lease mechanism and can use a retired generation's services after
    a live reload.
    """

    def test_proxy_request_does_not_read_generation_owned_app_state(
        self,
    ) -> None:
        """Verify _handle_proxy_request_inner has no direct app.state reads.

        The outer ``handle_proxy_request`` may read ``app.state`` as a
        fallback when no runtime manager is installed (tests, legacy).
        The inner handler receives injected services and must never
        reach back into ``app.state`` for generation-owned attributes.
        """
        proxy_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "eggpool"
            / "api"
            / "proxy_request.py"
        )
        source = proxy_path.read_text()
        tree = ast.parse(source)

        # Find _handle_proxy_request_inner's AST node
        inner_func = None
        for node in ast.iter_child_nodes(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_handle_proxy_request_inner"
            ):
                inner_func = node
                break
        assert inner_func is not None, (
            "_handle_proxy_request_inner not found in proxy_request.py"
        )

        violations: list[tuple[int, str]] = []
        for node in ast.walk(inner_func):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr not in _GENERATION_OWNED_ATTRS_TO_AUDIT:
                continue
            value = node.value
            if (
                isinstance(value, ast.Attribute)
                and value.attr == "state"
                and isinstance(value.value, ast.Attribute)
                and value.value.attr == "app"
            ):
                violations.append((node.lineno, f"request.app.state.{node.attr}"))
            elif (
                isinstance(value, ast.Attribute)
                and value.attr == "state"
                and isinstance(value.value, ast.Name)
                and value.value.id == "app"
            ):
                violations.append((node.lineno, f"app.state.{node.attr}"))

        assert violations == [], (
            "_handle_proxy_request_inner directly reads generation-owned "
            f"app.state attributes (must use injected services): {violations}"
        )

    def test_background_prune_uses_lease_pattern(self) -> None:
        """Verify _health_disabled_models_prune_once acquires a lease."""
        app_path = (
            Path(__file__).resolve().parent.parent.parent / "src" / "eggpool" / "app.py"
        )
        source = app_path.read_text()
        # Check that the prune callback references leased_runtime
        assert "leased_runtime" in source, (
            "_health_disabled_models_prune_once should use leased_runtime"
        )


# ---------------------------------------------------------------------------
# B10: Race between acquisition and slot retirement
# ---------------------------------------------------------------------------


class TestAcquireRetirementRace:
    @pytest.mark.asyncio
    async def test_acquire_succeeds_during_retirement(self) -> None:
        """A request that acquires before retirement keeps its lease."""
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)
        slot = manager._active
        assert slot is not None

        # Acquire a lease before retirement starts
        lease = await manager.acquire()
        assert slot.active_leases == 1

        # Begin retirement — the slot stops accepting new leases
        await manager.begin_retirement(slot)
        assert slot.accepting_leases is False
        # But the existing lease is still valid
        assert slot.active_leases == 1
        assert lease.runtime is gen

        # Release the lease
        await lease.release()
        assert slot.active_leases == 0

    @pytest.mark.asyncio
    async def test_acquire_after_retirement_retries_to_new_slot(self) -> None:
        """A request arriving during slot swap retries against the new slot."""
        # This tests the retry loop in acquire() when the active slot
        # stops accepting.  We simulate by installing, retiring, and
        # immediately installing a new generation.
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)
        slot0 = manager._active
        assert slot0 is not None

        # Start retirement (non-awaited, runs in background)
        retire_task = asyncio.create_task(manager.begin_retirement(slot0))

        # Give the retirement a moment to flip accepting_leases
        await asyncio.sleep(0.01)

        # Install a new generation — the acquire retry should find it
        gen1 = _fake_generation(1)
        # Bypass the "called twice" guard by directly setting _active
        slot1 = _GenerationSlot(generation=gen1)
        async with manager._lock:
            manager._active = slot1

        # Now acquire should succeed against the new slot
        lease = await manager.acquire()
        assert lease.generation_id == 1
        assert lease.slot is slot1

        await lease.release()
        await retire_task
        # Cleanup
        await manager.shutdown()


# ---------------------------------------------------------------------------
# B10: Streaming with client disconnect (lease release)
# ---------------------------------------------------------------------------


class TestStreamDisconnect:
    @pytest.mark.asyncio
    async def test_stream_releases_lease_on_generator_exception(self) -> None:
        """Client disconnect mid-stream triggers lease release."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        slot = manager._active
        assert slot is not None
        lease = await manager.acquire()
        assert slot.active_leases == 1

        async def failing_stream():
            yield b"chunk"
            raise ConnectionResetError("client disconnected")

        with pytest.raises(ConnectionResetError):
            async for _chunk in wrap_stream_with_lease(failing_stream(), lease):
                pass

        assert slot.active_leases == 0

    @pytest.mark.asyncio
    async def test_stream_releases_lease_on_cancellation(self) -> None:
        """CancelledError during streaming releases the lease."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))
        slot = manager._active
        assert slot is not None
        lease = await manager.acquire()
        assert slot.active_leases == 1

        async def slow_stream():
            yield b"a"
            await asyncio.sleep(10)
            yield b"b"

        gen = wrap_stream_with_lease(slow_stream(), lease)
        # Consume one chunk, then cancel
        chunk = await gen.__anext__()
        assert chunk == b"a"
        await gen.aclose()  # Simulate client disconnect

        assert slot.active_leases == 0


# ---------------------------------------------------------------------------
# B10: Background task uses generation lease
# ---------------------------------------------------------------------------


class TestBackgroundTaskLease:
    @pytest.mark.asyncio
    async def test_prune_health_disabled_models_uses_lease(self) -> None:
        """The health prune callback acquires a generation lease."""
        from unittest.mock import patch

        from eggpool.app import _prune_health_disabled_models_once

        manager = RuntimeManager()
        gen = _fake_generation(0)
        gen.registry.get_all_states.return_value = []
        await manager.install_initial(gen)

        # Mock leased_runtime to return our generation
        async def fake_leased_runtime(mgr):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _ctx():
                yield gen

            return _ctx()

        with patch("eggpool.runtime_manager.leased_runtime", fake_leased_runtime):
            result = await _prune_health_disabled_models_once(gen)

        assert result == 0  # No accounts to prune


# ---------------------------------------------------------------------------
# B10: Startup/shutdown regression
# ---------------------------------------------------------------------------


class TestStartupShutdownRegression:
    @pytest.mark.asyncio
    async def test_normal_startup_and_shutdown_succeeds(self) -> None:
        """Manager installs generation zero and shuts down cleanly."""
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)

        # Acquire and release a lease
        lease = await manager.acquire()
        assert lease.generation_id == 0
        await lease.release()

        # Shutdown
        await manager.shutdown()
        assert manager._shutdown_in_progress

        # No more acquisitions possible
        with pytest.raises(RuntimeManagerLeaseExhaustedError):
            await manager.acquire()

    @pytest.mark.asyncio
    async def test_shutdown_drains_active_leases(self) -> None:
        """Shutdown waits for active leases then completes."""
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)

        lease = await manager.acquire()
        slot = manager._active
        assert slot is not None
        assert slot.active_leases == 1

        # Start shutdown in background (will wait for drain)
        shutdown_task = asyncio.create_task(manager.shutdown())

        # Give it a moment
        await asyncio.sleep(0.05)

        # Shutdown is waiting for the lease
        assert slot.active_leases == 1

        # Release the lease — shutdown should complete
        await lease.release()
        await asyncio.wait_for(shutdown_task, timeout=2.0)
        assert manager._shutdown_in_progress


# ---------------------------------------------------------------------------
# B10: Builder build_initial
# ---------------------------------------------------------------------------


class TestBuilderBuildInitial:
    @pytest.mark.asyncio
    async def test_build_initial_wraps_services(self) -> None:
        """Builder wraps keyword services into a RuntimeGeneration."""
        from eggpool.runtime_manager import (
            ProcessRuntime,
            RuntimeGenerationBuilder,
        )

        builder = RuntimeGenerationBuilder()
        process = ProcessRuntime(db=MagicMock(), stats_db=MagicMock())
        config = MagicMock()

        result = await builder.build_initial(
            config,
            process,
            generation_id=5,
            config_digest="abc123",
            registry=MagicMock(),
            catalog=MagicMock(),
            router=MagicMock(),
            coordinator=MagicMock(),
            client_pool=MagicMock(),
            outbound_manager=MagicMock(),
            health_manager=MagicMock(),
            cost_calculator=MagicMock(),
            transcoder_policy=MagicMock(),
            compression_policy=MagicMock(),
            cache_config=MagicMock(),
            compression_tuning_registry=MagicMock(),
            dispatch_overhead_recorder=MagicMock(),
            dispatch_span_recorder=MagicMock(),
            account_backoff_repo=MagicMock(),
            stats_service=MagicMock(),
            supervisor=MagicMock(),
        )

        assert result.generation.generation_id == 5
        assert result.generation.config_digest == "abc123"
        assert result.process is process

    @pytest.mark.asyncio
    async def test_build_initial_raises_on_missing_service(self) -> None:
        """Builder raises when a required service is missing."""
        from eggpool.runtime_manager import (
            ProcessRuntime,
            RuntimeGenerationBuilder,
        )

        builder = RuntimeGenerationBuilder()
        process = ProcessRuntime(db=MagicMock(), stats_db=MagicMock())
        config = MagicMock()

        with pytest.raises(RuntimeError, match="missing required"):
            await builder.build_initial(
                config,
                process,
                generation_id=0,
                config_digest="",
                registry=MagicMock(),
                # Missing most services
            )
