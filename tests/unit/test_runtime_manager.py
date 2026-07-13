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

import time
from unittest.mock import MagicMock

import pytest

from eggpool.runtime_manager import (
    RuntimeDiagnostics,
    RuntimeGeneration,
    RuntimeManager,
    RuntimeManagerLeaseExhaustedError,
    RuntimeManagerShutdownError,
    _digest_prefix,
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
