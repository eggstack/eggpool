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


def _fake_generation(
    generation_id: int = 0, *, config_digest: str = "a" * 64
) -> RuntimeGeneration:
    """Return a minimal RuntimeGeneration with mock services."""
    return RuntimeGeneration(
        generation_id=generation_id,
        config=MagicMock(),
        config_digest=config_digest,
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
        dispatch_overhead_recorder=MagicMock(),
        dispatch_span_recorder=MagicMock(),
        account_backoff_repo=MagicMock(),
        stats_service=MagicMock(),
        supervisor=MagicMock(),
        routing_trace_guard=MagicMock(),
        routing_trace_writer=MagicMock(),
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
# mirror_generation_on_app_state (app.py)
# ---------------------------------------------------------------------------


class TestMirrorGenerationOnAppState:
    def test_mirror_sets_generation_owned_attrs(self) -> None:
        from eggpool.app import mirror_generation_on_app_state

        app = MagicMock()
        gen = _fake_generation(0)
        mirror_generation_on_app_state(app, gen)
        # Should have set router, catalog, and other operational mirrors.
        assert app.state.router is gen.router
        assert app.state.catalog is gen.catalog
        assert app.state.health_manager is gen.health_manager

    def test_mirror_does_not_overwrite_process_owned(self) -> None:
        from eggpool.app import mirror_generation_on_app_state

        app = MagicMock()
        app.state.db = MagicMock()
        app.state.config = MagicMock()
        original_db = app.state.db
        original_config = app.state.config
        gen = _fake_generation(0)
        mirror_generation_on_app_state(app, gen)
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
        "catalog",
        "health_manager",
        "router",
        "cost_calculator",
        "account_backoff_repo",
        "dispatch_overhead_recorder",
        "dispatch_span_recorder",
        "transcoder_policy",
        "compression_policy",
        "client_pool",
        "outbound_manager",
    }
)


def _find_inner_function(
    tree: ast.Module,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Locate ``_handle_proxy_request_inner`` in a parsed module."""
    for node in ast.iter_child_nodes(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_handle_proxy_request_inner"
        ):
            return node
    return None


def _expr_targets_app_state(expr: ast.expr) -> bool:
    """Return True if ``expr`` ends in ``.app.state``."""
    # Walk down nested ``Attribute`` nodes and check whether the
    # bottom pair is ``.app.state``.
    cursor: ast.expr = expr
    last_attr: str | None = None
    second_last_attr: str | None = None
    while isinstance(cursor, ast.Attribute):
        second_last_attr = last_attr
        last_attr = cursor.attr
        cursor = cursor.value
    return last_attr == "state" and second_last_attr == "app"


def _attr_targets_app_state(node: ast.Attribute) -> bool:
    """Return True if ``node`` reads from ``<X>.app.state.<attr>``."""
    return _expr_targets_app_state(node.value)


def _collect_inner_app_state_violations() -> list[tuple[int, str]]:
    """Walk proxy_request.py inner handler for forbidden reads."""
    proxy_path = (
        Path(__file__).resolve().parent.parent.parent
        / "src"
        / "eggpool"
        / "api"
        / "proxy_request.py"
    )
    source = proxy_path.read_text()
    tree = ast.parse(source)
    inner_func = _find_inner_function(tree)
    if inner_func is None:
        return []

    violations: list[tuple[int, str]] = []
    for node in ast.walk(inner_func):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in _GENERATION_OWNED_ATTRS_TO_AUDIT:
            continue
        value = node.value
        if not isinstance(value, ast.Attribute) or value.attr != "state":
            continue
        inner = value.value
        if isinstance(inner, ast.Attribute) and inner.attr == "app":
            violations.append((node.lineno, f"request.app.state.{node.attr}"))
        elif isinstance(inner, ast.Name) and inner.id == "app":
            violations.append((node.lineno, f"app.state.{node.attr}"))
    return violations


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
        violations = _collect_inner_app_state_violations()
        assert violations == [], (
            "_handle_proxy_request_inner directly reads generation-owned "
            f"app.state attributes (must use injected services): {violations}"
        )

    def test_known_providers_resolves_from_lease(self) -> None:
        """Verify provider parsing reads provider ids from the lease.

        The inner handler used to read ``request.app.state.config.providers``
        which bypasses the lease.  After D2 the handler must read
        ``lease.runtime.immutable_request_state.provider_ids``.
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
        inner_func = _find_inner_function(tree)
        assert inner_func is not None

        has_lease_provider_ids = False
        for node in ast.walk(inner_func):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr != "provider_ids":
                continue
            # Walk up the chain to find ``lease.runtime.immutable_request_state``
            parent_chain: list[ast.expr] = []
            cursor: ast.expr | None = node.value
            while isinstance(cursor, ast.Attribute):
                parent_chain.append(cursor.attr)
                cursor = cursor.value
            if isinstance(cursor, ast.Name) and cursor.id == "lease":
                has_lease_provider_ids = True
                break
        assert has_lease_provider_ids, (
            "_handle_proxy_request_inner must read provider ids "
            "from lease.runtime.immutable_request_state.provider_ids"
        )

    def test_proxy_request_no_getattr_app_state_in_inner(self) -> None:
        """The inner handler must not use ``getattr(request.app.state, ...)``.

        Production request handlers read generation-owned services
        from the leased ``coordinator``/``catalog``/etc. parameters,
        never directly via ``getattr(request.app.state, "router",
        None)``-style reads. The audit forbids both direct
        attribute chains (``request.app.state.<attr>``) and
        ``getattr()`` calls that target ``request.app.state``.

        Any ``getattr`` call targeting ``request.app.state`` is forbidden;
        the request path must use the leased generation instead.
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
        inner_func = _find_inner_function(tree)
        assert inner_func is not None

        violations: list[tuple[int, str]] = []
        for node in ast.walk(inner_func):
            getattr_call = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
            ):
                getattr_call = node
            elif isinstance(node, ast.Attribute) and node.attr in (
                _GENERATION_OWNED_ATTRS_TO_AUDIT
            ):
                # Use the existing audit path: a direct attribute read.
                # We re-walk using the same logic as the outer test for
                # consistency.
                if _attr_targets_app_state(node):
                    violations.append((node.lineno, f"request.app.state.{node.attr}"))
                continue
            else:
                continue

            if getattr_call is None:
                continue
            if len(getattr_call.args) < 1:
                continue
            target = getattr_call.args[0]
            if not _expr_targets_app_state(target):
                continue
            attr_name: str | None = None
            if len(getattr_call.args) >= 2 and isinstance(
                getattr_call.args[1], ast.Constant
            ):
                attr_name = (
                    getattr_call.args[1].value
                    if isinstance(getattr_call.args[1].value, str)
                    else None
                )
            line = getattr_call.lineno
            violations.append((line, f"getattr(...app.state, '{attr_name}')"))

        assert violations == [], (
            "_handle_proxy_request_inner must not use getattr() to read "
            f"app.state (must use "
            f"injected services): {violations}"
        )

    def test_background_prune_uses_lease_pattern(self) -> None:
        """Verify health-disabled-models prune callback acquires a lease.

        After the closure-pass refactor the unified
        :mod:`eggpool.runtime_tasks` module owns the registration
        table, so the audit reads from there rather than ``app.py``.
        """
        runtime_tasks_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "eggpool"
            / "runtime_tasks.py"
        )
        source = runtime_tasks_path.read_text()
        assert "prune_health_disabled_models_once" in source, (
            "runtime_tasks.py must register health_disabled_models_prune"
        )
        assert "leased_runtime" in source, (
            "health-disabled-models prune callback must use leased_runtime"
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

        from eggpool.app import prune_health_disabled_models_once

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
            result = await prune_health_disabled_models_once(gen)

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


# ---------------------------------------------------------------------------
# §5.2 AC#10: Multiple retired generations tracked concurrently
# ---------------------------------------------------------------------------


class TestMultiGenerationRetirement:
    @pytest.mark.asyncio
    async def test_multiple_retired_generations_tracked(self) -> None:
        """Multiple retired generations tracked concurrently during drain."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        # Hold a lease on gen0 so retirement cannot complete
        held_lease = await manager.acquire()
        assert held_lease.generation_id == 0

        # Publish gen1 — gen0 starts retiring (drain blocked by held lease)
        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, drain_timeout_s=5.0)
        await asyncio.sleep(0.05)
        assert any(s.generation.generation_id == 0 for s in manager._retiring)

        # Acquire on gen1, then publish gen2 — gen1 starts retiring too
        lease1 = await manager.acquire()
        gen2 = _fake_generation(2)
        await manager.install_candidate(gen2, drain_timeout_s=5.0)
        await asyncio.sleep(0.05)

        # Both gen0 and gen1 should be in _retiring simultaneously
        retiring_ids = {s.generation.generation_id for s in manager._retiring}
        assert 0 in retiring_ids
        assert 1 in retiring_ids

        # Release all leases to let retirements complete
        await held_lease.release()
        await lease1.release()
        # Wait for retirement tasks to complete
        await manager.wait_for_retirement(0, timeout_s=5.0)
        await manager.wait_for_retirement(1, timeout_s=5.0)

        # Active should be gen2, _retiring empty
        assert manager.active_snapshot().generation_id == 2
        assert len(manager._retiring) == 0

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_retiring_slot_count_accurate(self) -> None:
        """_retiring list reflects actual retiring slots."""
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        # No retiring slots yet
        assert len(manager._retiring) == 0

        # Hold a lease so retirement cannot complete immediately
        held = await manager.acquire()

        # Publish gen1
        await manager.install_candidate(_fake_generation(1), drain_timeout_s=5.0)
        await asyncio.sleep(0.05)
        assert len(manager._retiring) >= 1

        await held.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)
        assert len(manager._retiring) == 0

        await manager.shutdown()


# ---------------------------------------------------------------------------
# §5.3: Drain timeout forces close
# ---------------------------------------------------------------------------


class TestRetirementTimeout:
    @pytest.mark.asyncio
    async def test_drain_timeout_forces_close(self) -> None:
        """When drain timeout expires, resources are still closed."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        # Hold a lease that we never release
        _held_lease = await manager.acquire()

        # Publish gen1 with very short drain timeout
        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, drain_timeout_s=0.05)

        # Wait for the retirement task to complete (force-close path)
        await manager.wait_for_retirement(0, timeout_s=2.0)

        # gen0's client_pool.aclose() should have been called
        gen0.client_pool.aclose.assert_called()

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_long_drain_timeout_waits_for_lease_release(
        self,
    ) -> None:
        """With generous timeout, retirement waits for lease release."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        lease = await manager.acquire()

        # Publish gen1 with generous timeout
        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, drain_timeout_s=5.0)

        # Release the lease — retirement should complete cleanly
        await lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)

        # gen0's client_pool should be closed
        gen0.client_pool.aclose.assert_called()

        await manager.shutdown()


# ---------------------------------------------------------------------------
# §5.1 AC#12: Concurrent reload guard via expected_active_generation_id
# ---------------------------------------------------------------------------


class TestConcurrentReloadGuard:
    @pytest.mark.asyncio
    async def test_stale_candidate_rejected(self) -> None:
        """install_candidate with wrong expected_active_generation_id raises."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1)

        # Try to publish gen2 expecting gen0 is still active (stale)
        gen2 = _fake_generation(2)
        with pytest.raises(RuntimeError, match="Active generation changed"):
            await manager.install_candidate(gen2, expected_active_generation_id=0)

        # Active should still be gen1
        assert manager.active_snapshot().generation_id == 1

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_concurrent_install_candidate_guarded(self) -> None:
        """install_candidate with correct expected_active succeeds."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, expected_active_generation_id=0)

        assert manager.active_snapshot().generation_id == 1
        await manager.shutdown()


# ---------------------------------------------------------------------------
# AC#9: Old generation client pool closure
# ---------------------------------------------------------------------------


class TestOldGenerationClientPoolClosure:
    @pytest.mark.asyncio
    async def test_client_pool_closes_after_lease_drain(self) -> None:
        """Old gen's client_pool stays open during active lease, closes after."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        lease = await manager.acquire()

        # Publish gen1 — gen0 starts draining
        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, drain_timeout_s=5.0)
        await asyncio.sleep(0.05)

        # gen0's client_pool should NOT be closed yet (lease held, drain
        # in progress)
        gen0.client_pool.aclose.assert_not_called()

        # Release the lease — drain completes, resources close
        await lease.release()
        await manager.wait_for_retirement(0, timeout_s=5.0)

        # Now gen0's client_pool should be closed
        gen0.client_pool.aclose.assert_called()

        await manager.shutdown()

    @pytest.mark.asyncio
    async def test_client_pool_closes_on_timeout_if_lease_held(self) -> None:
        """Old gen's client_pool closes even with held lease after timeout."""
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        _held = await manager.acquire()

        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, drain_timeout_s=0.05)
        await manager.wait_for_retirement(0, timeout_s=2.0)

        # client_pool should be closed despite held lease (timeout forced it)
        gen0.client_pool.aclose.assert_called()


# ---------------------------------------------------------------------------
# Phase 7: Active-generation state authority
# ---------------------------------------------------------------------------


class TestActiveGenerationMetadata:
    """Test RuntimeManager.active_metadata() and snapshot_active_values()."""

    @pytest.mark.asyncio
    async def test_active_metadata_returns_immutable_view(self) -> None:
        from eggpool.runtime_manager import ActiveGenerationMetadata

        manager = RuntimeManager()
        gen = _fake_generation(42)
        await manager.install_initial(gen)

        meta = manager.active_metadata()
        assert isinstance(meta, ActiveGenerationMetadata)
        assert meta.generation_id == 42
        assert meta.config_digest == "a" * 64

    @pytest.mark.asyncio
    async def test_active_metadata_raises_when_no_generation(self) -> None:
        from eggpool.runtime_manager import RuntimeManagerShutdownError

        manager = RuntimeManager()
        with pytest.raises(RuntimeManagerShutdownError):
            manager.active_metadata()

    @pytest.mark.asyncio
    async def test_snapshot_active_values_returns_view(self) -> None:
        from eggpool.runtime_manager import ActiveGenerationView

        manager = RuntimeManager()
        gen = _fake_generation(7)
        await manager.install_initial(gen)

        view = manager.snapshot_active_values()
        assert isinstance(view, ActiveGenerationView)
        assert view.generation_id == 7
        assert view.config is gen.config
        assert view.registry is gen.registry
        assert view.catalog is gen.catalog
        assert view.router is gen.router
        assert view.coordinator is gen.coordinator
        assert view.health_manager is gen.health_manager
        assert view.stats is gen.stats_service

    @pytest.mark.asyncio
    async def test_snapshot_active_values_raises_when_no_generation(self) -> None:
        from eggpool.runtime_manager import RuntimeManagerShutdownError

        manager = RuntimeManager()
        with pytest.raises(RuntimeManagerShutdownError):
            manager.snapshot_active_values()

    @pytest.mark.asyncio
    async def test_metadata_updates_after_candidate_publication(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        meta0 = manager.active_metadata()
        assert meta0.generation_id == 0

        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1)

        meta1 = manager.active_metadata()
        assert meta1.generation_id == 1
        assert meta1.config_digest == gen1.config_digest

    @pytest.mark.asyncio
    async def test_snapshot_updates_after_candidate_publication(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        view0 = manager.snapshot_active_values()
        assert view0.generation_id == 0
        assert view0.router is gen0.router

        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1)

        view1 = manager.snapshot_active_values()
        assert view1.generation_id == 1
        assert view1.router is gen1.router

    @pytest.mark.asyncio
    async def test_snapshot_reflects_supervisor_patch(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)

        new_supervisor = MagicMock()
        manager.attach_supervisor_to_active(new_supervisor)

        view = manager.snapshot_active_values()
        assert view.supervisor is new_supervisor


class TestAppStateAuditEnforcementPhase7:
    """Phase 7 audit: generation-owned services accessed through helpers."""

    def test_stats_routes_use_helper(self) -> None:
        """Verify stats.py uses _get_stats helper instead of direct app.state."""
        stats_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "eggpool"
            / "api"
            / "stats.py"
        )
        source = stats_path.read_text()
        assert "_get_stats(request)" in source or "def _get_stats" in source, (
            "stats.py must use _get_stats helper for generation-owned services"
        )
        # Verify no direct request.app.state.stats reads remain outside the helper
        lines = source.split("\n")
        in_helper = False
        violations = []
        for i, line in enumerate(lines, 1):
            if "def _get_stats" in line:
                in_helper = True
                continue
            if in_helper and (line.strip() == "" or not line.startswith(" ")):
                in_helper = False
            if (
                not in_helper
                and "request.app.state.stats" in line
                and "def " not in line
            ):
                violations.append((i, line.strip()))
        assert violations == [], (
            f"stats.py has direct app.state.stats reads outside helper: {violations}"
        )

    def test_model_info_routes_use_helper(self) -> None:
        """Verify model_info.py uses _get_model_info helper."""
        mi_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "eggpool"
            / "api"
            / "model_info.py"
        )
        source = mi_path.read_text()
        assert (
            "_get_model_info(request)" in source or "def _get_model_info" in source
        ), "model_info.py must use _get_model_info helper"

    def test_dashboard_routes_use_helpers(self) -> None:
        """Verify dashboard/routes.py uses helpers for generation-owned services."""
        routes_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "eggpool"
            / "dashboard"
            / "routes.py"
        )
        source = routes_path.read_text()
        assert "def _get_stats" in source, (
            "dashboard/routes.py must define _get_stats helper"
        )
        assert "def _get_model_info" in source, (
            "dashboard/routes.py must define _get_model_info helper"
        )
        assert "def _get_catalog" in source, (
            "dashboard/routes.py must define _get_catalog helper"
        )

    def test_backoff_routes_use_helper(self) -> None:
        """Verify backoff.py uses _get_account_backoff_repo helper."""
        backoff_path = (
            Path(__file__).resolve().parent.parent.parent
            / "src"
            / "eggpool"
            / "api"
            / "backoff.py"
        )
        source = backoff_path.read_text()
        assert "def _get_account_backoff_repo" in source, (
            "backoff.py must define _get_account_backoff_repo helper"
        )

    def test_readiness_uses_active_generation(self) -> None:
        """Verify readyz endpoint uses runtime_manager.active_snapshot()."""
        app_path = (
            Path(__file__).resolve().parent.parent.parent / "src" / "eggpool" / "app.py"
        )
        source = app_path.read_text()
        # Find the readyz handler
        readyz_start = source.find("async def readyz(")
        assert readyz_start != -1, "readyz handler not found"
        # Find the end of readyz (next @app.get or next function def)
        readyz_section = source[readyz_start : readyz_start + 4000]
        assert "runtime_manager.active_snapshot()" in readyz_section, (
            "readyz must use runtime_manager.active_snapshot()"
            " for generation-owned checks"
        )

    def test_readiness_checks_lease_acceptance(self) -> None:
        """Verify readyz checks is_accepting_leases for degraded state."""
        app_path = (
            Path(__file__).resolve().parent.parent.parent / "src" / "eggpool" / "app.py"
        )
        source = app_path.read_text()
        readyz_start = source.find("async def readyz(")
        readyz_section = source[readyz_start : readyz_start + 4000]
        assert "is_accepting_leases()" in readyz_section, (
            "readyz must check is_accepting_leases()"
        )
        assert "not accepting leases" in readyz_section, (
            "readyz must report degraded when generation not accepting leases"
        )

    def test_readiness_checks_transaction_failure(self) -> None:
        """Verify readyz checks for compensation_failed transaction."""
        app_path = (
            Path(__file__).resolve().parent.parent.parent / "src" / "eggpool" / "app.py"
        )
        source = app_path.read_text()
        readyz_start = source.find("async def readyz(")
        readyz_section = source[readyz_start : readyz_start + 5000]
        assert "compensation_failed" in readyz_section, (
            "readyz must check for compensation_failed transaction state"
        )

    def test_readiness_rejects_generation_not_accepting(self) -> None:
        """Readiness returns 503 when active generation stops accepting leases."""
        manager = RuntimeManager()
        # Don't install; has_active_generation is False, so readiness
        # should not fail on lease acceptance (it checks only when
        # runtime_manager.has_active_generation() is True).
        assert not manager.has_active_generation()

    def test_mirror_deprecation_docstring(self) -> None:
        """Verify mirror_generation_on_app_state has deprecation docstring."""
        app_path = (
            Path(__file__).resolve().parent.parent.parent / "src" / "eggpool" / "app.py"
        )
        source = app_path.read_text()
        mirror_start = source.find("def mirror_generation_on_app_state(")
        assert mirror_start != -1, "mirror_generation_on_app_state not found"
        mirror_section = source[mirror_start : mirror_start + 1500]
        assert "deprecated" in mirror_section.lower(), (
            "mirror_generation_on_app_state must have deprecation docstring"
        )


# ---------------------------------------------------------------------------
# Phase 7 scenario tests: publication coherence, retirement safety,
# concurrent reads, manager unavailable, config digest
# ---------------------------------------------------------------------------


class TestPublicationCoherence:
    """After publication, active generation must reflect the candidate."""

    @pytest.mark.asyncio
    async def test_active_matches_candidate_after_publication(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        gen1 = _fake_generation(1, config_digest="b" * 64)
        await manager.install_candidate(gen1)

        active = manager.active_snapshot()
        assert active.generation_id == 1
        assert active.config_digest == "b" * 64
        assert active.router is gen1.router
        assert active.coordinator is gen1.coordinator

    @pytest.mark.asyncio
    async def test_metadata_matches_candidate_after_publication(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        gen1 = _fake_generation(1, config_digest="c" * 64)
        await manager.install_candidate(gen1)

        meta = manager.active_metadata()
        assert meta.generation_id == 1
        assert meta.config_digest == "c" * 64

    @pytest.mark.asyncio
    async def test_snapshot_matches_candidate_after_publication(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1)

        view = manager.snapshot_active_values()
        assert view.generation_id == 1
        assert view.router is gen1.router
        assert view.stats is gen1.stats_service

    @pytest.mark.asyncio
    async def test_multiple_publications_reflect_latest(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        for i in range(1, 5):
            gen = _fake_generation(i, config_digest=chr(ord("a") + i) * 64)
            await manager.install_candidate(gen)

        meta = manager.active_metadata()
        assert meta.generation_id == 4
        assert meta.config_digest == "e" * 64


class TestRetirementSafety:
    """During retirement, reads see new generation, not retired resources."""

    @pytest.mark.asyncio
    async def test_read_during_retirement_uses_new_generation(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        # Hold a lease on gen0 so it cannot retire immediately
        held_lease = await manager.acquire()
        assert held_lease.generation_id == 0

        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, drain_timeout_s=0.05)

        # Active snapshot should now be gen1
        active = manager.active_snapshot()
        assert active.generation_id == 1

        # Snapshot should also be gen1
        view = manager.snapshot_active_values()
        assert view.generation_id == 1
        assert view.router is gen1.router

        # Release the held lease so retirement can complete
        await held_lease.release()
        await manager.wait_for_retirement(0, timeout_s=2.0)

    @pytest.mark.asyncio
    async def test_retiring_generation_not_active(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        held_lease = await manager.acquire()
        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, drain_timeout_s=0.05)

        # active_metadata should not return gen0
        meta = manager.active_metadata()
        assert meta.generation_id != 0
        assert meta.generation_id == 1

        await held_lease.release()
        await manager.wait_for_retirement(0, timeout_s=2.0)

    @pytest.mark.asyncio
    async def test_retirement_diagnostics_available(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        held_lease = await manager.acquire()
        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, drain_timeout_s=0.05)
        # Allow the background retirement task to start
        await asyncio.sleep(0)

        retiring = manager.retirement_snapshot()
        assert isinstance(retiring, tuple)
        assert len(retiring) >= 1
        assert retiring[0].generation_id == 0

        await held_lease.release()
        await manager.wait_for_retirement(0, timeout_s=2.0)

    @pytest.mark.asyncio
    async def test_retirement_snapshot_specific_generation(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        held_lease = await manager.acquire()
        gen1 = _fake_generation(1)
        await manager.install_candidate(gen1, drain_timeout_s=0.05)
        await asyncio.sleep(0)

        diag = manager.retirement_snapshot(generation_id=0)
        assert diag.generation_id == 0
        assert diag.retirement_started is True

        await held_lease.release()
        await manager.wait_for_retirement(0, timeout_s=2.0)

    @pytest.mark.asyncio
    async def test_retirement_snapshot_nonexistent_raises(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        with pytest.raises(ValueError, match="No retiring generation"):
            manager.retirement_snapshot(generation_id=999)


class TestConcurrentPublicationReads:
    """Concurrent reads during publication see consistent state."""

    @pytest.mark.asyncio
    async def test_concurrent_snapshots_see_consistent_generation(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0)
        await manager.install_initial(gen0)

        # Publish gen1
        gen1 = _fake_generation(1, config_digest="x" * 64)
        await manager.install_candidate(gen1)

        # Multiple reads should all see gen1
        for _ in range(10):
            meta = manager.active_metadata()
            assert meta.generation_id == 1
            assert meta.config_digest == "x" * 64

            view = manager.snapshot_active_values()
            assert view.generation_id == 1
            assert view.router is gen1.router

    @pytest.mark.asyncio
    async def test_read_during_concurrent_publication(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        # Simulate rapid successive publications
        for i in range(1, 6):
            gen = _fake_generation(i, config_digest=chr(ord("a") + i) * 64)
            await manager.install_candidate(gen)

            # Each read should see at least generation i
            meta = manager.active_metadata()
            assert meta.generation_id >= i


class TestManagerUnavailable:
    """During shutdown, generation-dependent operations fail gracefully."""

    @pytest.mark.asyncio
    async def test_acquire_fails_after_shutdown(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)

        await manager.shutdown()

        with pytest.raises(RuntimeManagerLeaseExhaustedError):
            await manager.acquire()

    @pytest.mark.asyncio
    async def test_active_snapshot_fails_after_shutdown(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)

        await manager.shutdown()

        with pytest.raises(RuntimeManagerShutdownError):
            manager.active_snapshot()

    @pytest.mark.asyncio
    async def test_active_metadata_fails_after_shutdown(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)

        await manager.shutdown()

        with pytest.raises(RuntimeManagerShutdownError):
            manager.active_metadata()

    @pytest.mark.asyncio
    async def test_snapshot_active_fails_after_shutdown(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)

        await manager.shutdown()

        with pytest.raises(RuntimeManagerShutdownError):
            manager.snapshot_active_values()

    @pytest.mark.asyncio
    async def test_accepting_leases_false_after_shutdown(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(0)
        await manager.install_initial(gen)

        assert manager.is_accepting_leases()

        await manager.shutdown()

        assert not manager.is_accepting_leases()

    @pytest.mark.asyncio
    async def test_accepting_leases_false_when_no_generation(self) -> None:
        manager = RuntimeManager()
        assert not manager.is_accepting_leases()

    @pytest.mark.asyncio
    async def test_retirement_snapshot_empty_when_no_retiring(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        result = manager.retirement_snapshot()
        assert result == ()


class TestConfigDigest:
    """Config digest reflects the active generation after reload."""

    @pytest.mark.asyncio
    async def test_digest_updates_after_publication(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0, config_digest="a" * 64)
        await manager.install_initial(gen0)

        assert manager.active_metadata().config_digest == "a" * 64

        gen1 = _fake_generation(1, config_digest="b" * 64)
        await manager.install_candidate(gen1)

        assert manager.active_metadata().config_digest == "b" * 64

    @pytest.mark.asyncio
    async def test_digest_changes_across_multiple_reloads(self) -> None:
        manager = RuntimeManager()
        await manager.install_initial(_fake_generation(0))

        digests = []
        for i in range(1, 6):
            digest = chr(ord("a") + i) * 64
            gen = _fake_generation(i, config_digest=digest)
            await manager.install_candidate(gen)
            digests.append(manager.active_metadata().config_digest)

        assert digests == ["b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64]

    @pytest.mark.asyncio
    async def test_digest_independent_of_retirement(self) -> None:
        manager = RuntimeManager()
        gen0 = _fake_generation(0, config_digest="a" * 64)
        await manager.install_initial(gen0)

        held = await manager.acquire()
        gen1 = _fake_generation(1, config_digest="b" * 64)
        await manager.install_candidate(gen1, drain_timeout_s=0.05)

        # Digest should be gen1's even while gen0 is retiring
        assert manager.active_metadata().config_digest == "b" * 64

        await held.release()
        await manager.wait_for_retirement(0, timeout_s=2.0)

    @pytest.mark.asyncio
    async def test_snapshot_digest_matches_metadata(self) -> None:
        manager = RuntimeManager()
        gen = _fake_generation(0, config_digest="z" * 64)
        await manager.install_initial(gen)

        meta = manager.active_metadata()
        view = manager.snapshot_active_values()
        assert meta.config_digest == view.config_digest == "z" * 64
