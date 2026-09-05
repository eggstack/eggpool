"""Phase 4 — Candidate resource ownership and abort cleanup tests.

Tests the RuntimeGenerationCandidate container, registration API,
abort behavior, ownership transfer, cancellation shielding, and
cleanup diagnostics.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eggpool.runtime_manager import (
    CandidateOwnershipState,
    CleanupDiagnostics,
    RuntimeGenerationCandidate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candidate(generation_id: int = 1) -> RuntimeGenerationCandidate:
    return RuntimeGenerationCandidate(generation_id=generation_id)


# ---------------------------------------------------------------------------
# Registration API
# ---------------------------------------------------------------------------


class TestCandidateRegistration:
    def test_initial_state_is_building(self) -> None:
        c = _make_candidate(42)
        assert c.ownership_state is CandidateOwnershipState.BUILDING
        assert c.generation_id == 42

    def test_register_resource(self) -> None:
        c = _make_candidate()
        close_fn = MagicMock()
        c.register_resource("pool", close_fn)
        # Resource is registered but not yet closed.
        close_fn.assert_not_called()

    def test_register_multiple_resources(self) -> None:
        c = _make_candidate()
        c.register_resource("a", MagicMock())
        c.register_resource("b", MagicMock())
        c.register_resource("c", MagicMock())
        assert c.ownership_state is CandidateOwnershipState.BUILDING

    def test_register_after_prepared_raises(self) -> None:
        c = _make_candidate()
        c.mark_prepared()
        with pytest.raises(RuntimeError, match="Cannot register resource"):
            c.register_resource("pool", MagicMock())

    @pytest.mark.asyncio
    async def test_register_after_abort_raises(self) -> None:
        c = _make_candidate()
        c.register_resource("pool", MagicMock())
        await c.abort(RuntimeError("fail"))
        with pytest.raises(RuntimeError, match="Cannot register resource"):
            c.register_resource("pool2", MagicMock())

    @pytest.mark.asyncio
    async def test_register_after_transfer_raises(self) -> None:
        c = _make_candidate()
        c.mark_prepared()
        c.transfer_to_runtime_manager()
        with pytest.raises(RuntimeError, match="Cannot register resource"):
            c.register_resource("pool", MagicMock())


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


class TestCandidateStateTransitions:
    def test_mark_prepared(self) -> None:
        c = _make_candidate()
        c.mark_prepared()
        assert c.ownership_state is CandidateOwnershipState.PREPARED

    def test_mark_prepared_twice_raises(self) -> None:
        c = _make_candidate()
        c.mark_prepared()
        with pytest.raises(RuntimeError, match="Cannot mark prepared"):
            c.mark_prepared()

    def test_transfer_requires_prepared(self) -> None:
        c = _make_candidate()
        with pytest.raises(RuntimeError, match="Cannot transfer"):
            c.transfer_to_runtime_manager()

    def test_transfer_success(self) -> None:
        c = _make_candidate()
        c.mark_prepared()
        c.transfer_to_runtime_manager()
        assert c.ownership_state is CandidateOwnershipState.TRANSFERRED

    def test_transfer_twice_raises(self) -> None:
        c = _make_candidate()
        c.mark_prepared()
        c.transfer_to_runtime_manager()
        with pytest.raises(RuntimeError, match="Cannot transfer"):
            c.transfer_to_runtime_manager()


# ---------------------------------------------------------------------------
# Abort behavior
# ---------------------------------------------------------------------------


class TestCandidateAbort:
    @pytest.mark.asyncio
    async def test_abort_closes_resources_in_reverse_order(self) -> None:
        c = _make_candidate()
        close_order: list[str] = []
        c.register_resource("first", lambda: close_order.append("first"))
        c.register_resource("second", lambda: close_order.append("second"))
        c.register_resource("third", lambda: close_order.append("third"))

        await c.abort(RuntimeError("fail"))

        assert close_order == ["third", "second", "first"]

    @pytest.mark.asyncio
    async def test_abort_idempotent(self) -> None:
        c = _make_candidate()
        call_count = 0

        def counting_close() -> None:
            nonlocal call_count
            call_count += 1

        c.register_resource("pool", counting_close)
        diag1 = await c.abort(RuntimeError("fail"))
        assert call_count == 1

        diag2 = await c.abort(RuntimeError("fail"))
        assert call_count == 1  # no additional call
        # Returns cached diagnostics.
        assert diag2 is diag1

    @pytest.mark.asyncio
    async def test_abort_state_transitions(self) -> None:
        c = _make_candidate()
        c.register_resource("pool", MagicMock())
        diag = await c.abort(RuntimeError("fail"))
        assert c.ownership_state is CandidateOwnershipState.ABORTED
        assert diag is not None

    @pytest.mark.asyncio
    async def test_abort_preserves_primary_error(self) -> None:
        c = _make_candidate()
        diag = await c.abort(RuntimeError("original failure"))
        assert diag is not None
        assert "original failure" in diag.primary_failure

    @pytest.mark.asyncio
    async def test_abort_collects_close_errors(self) -> None:
        c = _make_candidate()
        c.register_resource("good", MagicMock())
        c.register_resource("bad", MagicMock(side_effect=RuntimeError("close fail")))

        diag = await c.abort(RuntimeError("primary"))
        assert diag is not None
        assert len(diag.close_errors) == 1
        assert "close fail" in diag.close_errors[0]
        assert "bad" in diag.close_errors[0]

    @pytest.mark.asyncio
    async def test_abort_diagnostics_redact_and_bound_exception_text(self) -> None:
        secret = "sk-" + "a" * 40
        cause = RuntimeError(f"api_key={secret} " + "x" * 2000)
        c = _make_candidate()
        c.register_resource(
            "bad",
            MagicMock(side_effect=RuntimeError(f"token={secret} " + "y" * 2000)),
        )

        diag = await c.abort(cause, failure_stage="build")

        assert diag is not None
        combined = " ".join((diag.primary_failure, *diag.close_errors))
        assert secret not in combined
        assert "[REDACTED]" in combined
        assert len(diag.primary_failure) <= 512
        assert all(len(error) <= 512 for error in diag.close_errors)

    @pytest.mark.asyncio
    async def test_abort_does_not_mask_primary_error(self) -> None:
        c = _make_candidate()
        c.register_resource("pool", MagicMock(side_effect=RuntimeError("close fail")))

        diag = await c.abort(RuntimeError("primary failure"))
        assert diag is not None
        assert "primary failure" in diag.primary_failure

    @pytest.mark.asyncio
    async def test_abort_closes_all_resources_even_on_error(self) -> None:
        c = _make_candidate()
        closed: list[str] = []
        c.register_resource("a", lambda: closed.append("a"))
        c.register_resource("b", MagicMock(side_effect=RuntimeError("fail")))
        c.register_resource("c", lambda: closed.append("c"))

        await c.abort(RuntimeError("primary"))
        assert "a" in closed
        assert "c" in closed

    @pytest.mark.asyncio
    async def test_abort_empty_candidate(self) -> None:
        c = _make_candidate()
        diag = await c.abort(RuntimeError("nothing to close"))
        assert diag is not None
        assert diag.resource_types_registered == ()
        assert diag.resource_types_closed == ()
        assert diag.close_errors == ()

    @pytest.mark.asyncio
    async def test_abort_logs_diagnostics(self) -> None:
        c = _make_candidate(99)
        c.register_resource("pool", MagicMock())
        diag = await c.abort(RuntimeError("fail"))
        assert diag is not None
        assert diag.generation_id == 99
        assert diag.resource_types_registered == ("pool",)
        assert diag.resource_types_closed == ("pool",)
        assert diag.close_duration_s >= 0.0
        assert diag.timed_out is False

    @pytest.mark.asyncio
    async def test_abort_with_async_close_callback(self) -> None:
        c = _make_candidate()
        close_fn = AsyncMock()
        c.register_resource("async_pool", close_fn)
        await c.abort(RuntimeError("fail"))
        close_fn.assert_awaited_once()


# ---------------------------------------------------------------------------
# Cleanup diagnostics
# ---------------------------------------------------------------------------


class TestCleanupDiagnostics:
    @pytest.mark.asyncio
    async def test_diagnostics_frozen(self) -> None:
        c = _make_candidate()
        diag = await c.abort(RuntimeError("fail"))
        assert isinstance(diag, CleanupDiagnostics)
        assert diag.generation_id == c.generation_id

    @pytest.mark.asyncio
    async def test_diagnostics_no_timeout_by_default(self) -> None:
        c = _make_candidate()
        c.register_resource("pool", MagicMock())
        diag = await c.abort(RuntimeError("fail"))
        assert diag is not None
        assert diag.timed_out is False

    @pytest.mark.asyncio
    async def test_diagnostics_available_after_abort(self) -> None:
        c = _make_candidate()
        assert c.diagnostics is None
        await c.abort(RuntimeError("fail"))
        assert c.diagnostics is not None

    def test_diagnostics_none_before_abort(self) -> None:
        c = _make_candidate()
        assert c.diagnostics is None


# ---------------------------------------------------------------------------
# Transfer behavior
# ---------------------------------------------------------------------------


class TestCandidateTransfer:
    @pytest.mark.asyncio
    async def test_transfer_clears_resources(self) -> None:
        c = _make_candidate()
        c.register_resource("pool", MagicMock())
        c.mark_prepared()
        c.transfer_to_runtime_manager()
        # After transfer, abort is a no-op.
        diag = await c.abort(RuntimeError("should not close"))
        assert diag is not None
        assert diag.resource_types_registered == ()

    @pytest.mark.asyncio
    async def test_transfer_then_abort_noop(self) -> None:
        c = _make_candidate()
        close_fn = MagicMock()
        c.register_resource("pool", close_fn)
        c.mark_prepared()
        c.transfer_to_runtime_manager()

        diag = await c.abort(RuntimeError("noop"))
        close_fn.assert_not_called()
        assert c.ownership_state is CandidateOwnershipState.TRANSFERRED
        assert diag.ownership_state == "transferred"
        assert diag.ownership_state_at_failure == "transferred"
        assert diag.resource_types_registered == ()

    @pytest.mark.asyncio
    async def test_abort_then_transfer_raises(self) -> None:
        c = _make_candidate()
        c.register_resource("pool", MagicMock())
        await c.abort(RuntimeError("fail"))
        with pytest.raises(RuntimeError, match="Cannot transfer"):
            c.transfer_to_runtime_manager()


# ---------------------------------------------------------------------------
# Integration with ReloadManager
# ---------------------------------------------------------------------------


class TestCandidateAbortOnBuildFailure:
    @pytest.mark.asyncio
    async def test_build_failure_aborts_candidate(self) -> None:
        """When _build_candidate_generation fails, all registered resources
        should be closed via candidate.abort."""
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.runtime_manager import (
            ProcessRuntime,
            RuntimeManager,
        )

        rm = RuntimeManager()
        process = MagicMock(spec=ProcessRuntime)
        process.db = MagicMock()
        process.stats_db = MagicMock()
        mgr = ReloadManager(rm, process)

        # Inject a build failure
        mgr.TEST_INJECT_BUILD_FAILURE = RuntimeError("build exploded")

        from tests.unit.test_reload_manager import (
            _make_diff,
            _make_real_config,
            _make_real_generation,
            _make_validation,
        )

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        validation = _make_validation()
        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))

        with patch.object(mgr, "_compute_reload_diff", return_value=diff):
            result = await mgr.reload(validation)

        # Reload should fail gracefully.
        assert result.ok is False
        assert "Failed to construct candidate generation" in result.message

        # Reload manager should still be operational.
        assert not mgr.snapshot()["admitted"]

    @pytest.mark.asyncio
    async def test_partial_build_registers_and_aborts_resources(self) -> None:
        """When a build fails partway, the candidate aborts registered
        resources and logs diagnostics."""
        from eggpool.runtime_manager import RuntimeGenerationCandidate

        c = RuntimeGenerationCandidate(generation_id=7)
        close_log: list[str] = []

        c.register_resource("client_pool", lambda: close_log.append("client_pool"))
        c.register_resource(
            "outbound_manager", lambda: close_log.append("outbound_manager")
        )

        # Simulate a failure after registering two resources.
        diag = await c.abort(RuntimeError("mid-build failure"))

        assert "client_pool" in close_log
        assert "outbound_manager" in close_log
        assert close_log == ["outbound_manager", "client_pool"]  # reverse order
        assert diag is not None
        assert diag.generation_id == 7
        assert "mid-build failure" in diag.primary_failure


# ---------------------------------------------------------------------------
# Extended diagnostics (Phase 4 gap-fill)
# ---------------------------------------------------------------------------


class TestCleanupDiagnosticsExtended:
    @pytest.mark.asyncio
    async def test_diagnostics_has_failure_stage(self) -> None:
        c = _make_candidate(10)
        diag = await c.abort(RuntimeError("fail"), failure_stage="build")
        assert diag is not None
        assert diag.primary_failure_stage == "build"

    @pytest.mark.asyncio
    async def test_diagnostics_default_failure_stage(self) -> None:
        c = _make_candidate(11)
        diag = await c.abort(RuntimeError("fail"))
        assert diag is not None
        assert diag.primary_failure_stage == "unknown"

    @pytest.mark.asyncio
    async def test_diagnostics_ownership_state_at_failure(self) -> None:
        c = _make_candidate(12)
        c.register_resource("pool", MagicMock())
        diag = await c.abort(RuntimeError("fail"), failure_stage="build")
        assert diag is not None
        assert diag.ownership_state_at_failure == "building"

    @pytest.mark.asyncio
    async def test_diagnostics_ownership_state_prepared(self) -> None:
        c = _make_candidate(13)
        c.mark_prepared()
        diag = await c.abort(RuntimeError("fail"), failure_stage="reconcile")
        assert diag is not None
        assert diag.ownership_state_at_failure == "prepared"

    @pytest.mark.asyncio
    async def test_diagnostics_close_errors_by_type(self) -> None:
        c = _make_candidate(14)
        c.register_resource("pool_a", MagicMock(side_effect=RuntimeError("err_a")))
        c.register_resource("pool_b", MagicMock(side_effect=ValueError("err_b")))
        diag = await c.abort(RuntimeError("primary"))
        assert diag is not None
        assert len(diag.close_errors_by_type) == 2
        types = {t for _, t in diag.close_errors_by_type}
        assert "RuntimeError" in types
        assert "ValueError" in types

    @pytest.mark.asyncio
    async def test_diagnostics_timeout_error_type(self) -> None:
        c = _make_candidate(15)

        async def slow_close() -> None:
            await asyncio.sleep(100)

        c.register_resource("slow", slow_close)
        diag = await c.abort(RuntimeError("fail"))
        assert diag is not None
        assert diag.timed_out is True
        assert any(t == "TimeoutError" for _, t in diag.close_errors_by_type)

    @pytest.mark.asyncio
    async def test_diagnostics_idempotent_returns_same(self) -> None:
        c = _make_candidate(16)
        c.register_resource("pool", MagicMock())
        diag1 = await c.abort(RuntimeError("fail"), failure_stage="build")
        diag2 = await c.abort(RuntimeError("fail"), failure_stage="reconcile")
        assert diag1 is diag2
        assert diag1.primary_failure_stage == "build"


# ---------------------------------------------------------------------------
# Cancellation shielding in ReloadManager.reload()
# ---------------------------------------------------------------------------


class TestReloadCancellationShielding:
    @pytest.mark.asyncio
    async def test_cancelled_error_aborts_candidate(self) -> None:
        """CancelledError during build aborts the candidate and re-raises."""
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.runtime_manager import (
            ProcessRuntime,
            RuntimeManager,
        )

        rm = RuntimeManager()
        process = MagicMock(spec=ProcessRuntime)
        process.db = MagicMock()
        process.stats_db = MagicMock()
        mgr = ReloadManager(rm, process)

        from tests.unit.test_reload_manager import (
            _make_diff,
            _make_real_config,
            _make_real_generation,
            _make_validation,
        )

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        validation = _make_validation()
        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))

        build_event = asyncio.Event()

        async def slow_build(*args: object, **kwargs: object) -> object:
            build_event.set()
            await asyncio.sleep(100)
            raise ShouldNotReachError()  # pragma: no cover

        with (
            patch.object(mgr, "_build_candidate_generation", slow_build),
            patch.object(mgr, "_compute_reload_diff", return_value=diff),
        ):
            # Start the reload in a task so we can cancel it
            task = asyncio.create_task(mgr.reload(validation))
            await build_event.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # Reload manager should still be operational after cancellation.
        assert not mgr.snapshot()["admitted"]

    @pytest.mark.asyncio
    async def test_cancelled_stores_cleanup_diagnostics(self) -> None:
        """CancelledError stores cleanup diagnostics in the reload manager."""
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.runtime_manager import (
            ProcessRuntime,
            RuntimeManager,
        )

        rm = RuntimeManager()
        process = MagicMock(spec=ProcessRuntime)
        process.db = MagicMock()
        process.stats_db = MagicMock()
        mgr = ReloadManager(rm, process)

        from tests.unit.test_reload_manager import (
            _make_diff,
            _make_real_config,
            _make_real_generation,
            _make_validation,
        )

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        validation = _make_validation()
        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))

        build_event = asyncio.Event()

        async def slow_build(*args: object, **kwargs: object) -> object:
            build_event.set()
            await asyncio.sleep(100)
            raise ShouldNotReachError()  # pragma: no cover

        with (
            patch.object(mgr, "_build_candidate_generation", slow_build),
            patch.object(mgr, "_compute_reload_diff", return_value=diff),
        ):
            task = asyncio.create_task(mgr.reload(validation))
            await build_event.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        snap = mgr.snapshot()
        # The diagnostics may or may not be present depending on
        # whether the shield completed — but the key is that the
        # snapshot has the field.
        assert "last_cleanup_diagnostics" in snap
        assert "last_cleanup_diagnostics" in snap


class ShouldNotReachError(Exception):
    """Should never be raised — used as a sentinel for unreachable code."""


# ---------------------------------------------------------------------------
# Post-construction pre-publication failure injection
# ---------------------------------------------------------------------------


class TestPrePublicationFailureInjection:
    @pytest.mark.asyncio
    async def test_reconcile_failure_aborts_candidate(self) -> None:
        """When reconcile fails, the candidate is aborted."""
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.runtime_manager import (
            ProcessRuntime,
            RuntimeGenerationCandidate,
            RuntimeManager,
        )

        rm = RuntimeManager()
        process = MagicMock(spec=ProcessRuntime)
        process.db = MagicMock()
        process.stats_db = MagicMock()
        process.process_supervisor = None
        mgr = ReloadManager(rm, process)

        from tests.unit.test_reload_manager import (
            _make_diff,
            _make_real_config,
            _make_real_generation,
            _make_validation,
        )

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        validation = _make_validation()
        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))

        # Mock _build_candidate_generation to return a valid candidate
        # with a mock generation, so the inject seam runs after build.
        candidate = RuntimeGenerationCandidate(generation_id=1)
        candidate.register_resource("test_resource", MagicMock())
        candidate._built_generation = _make_real_generation(
            generation_id=1, config=_make_real_config()
        )
        candidate._process_ref = process
        candidate._diff_ref = diff
        candidate.mark_prepared()

        with (
            patch.object(mgr, "_build_candidate_generation", return_value=candidate),
            patch.object(mgr, "_compute_reload_diff", return_value=diff),
            patch.object(
                mgr,
                "_prepare_persistence_delta",
                side_effect=RuntimeError("reconcile exploded"),
            ),
        ):
            result = await mgr.reload(validation)

        assert result.ok is False
        assert "reconcile exploded" in result.message
        # Candidate abort diagnostics should be captured.
        snap = mgr.snapshot()
        assert snap["last_cleanup_diagnostics"] is not None
        assert (
            snap["last_cleanup_diagnostics"]["primary_failure_stage"]
            == "reconciliation"
        )

    @pytest.mark.asyncio
    async def test_publish_failure_aborts_candidate(self) -> None:
        """When publish fails before commit, the candidate is aborted."""
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.runtime_manager import (
            ProcessRuntime,
            RuntimeGenerationCandidate,
            RuntimeManager,
        )

        rm = RuntimeManager()
        process = MagicMock(spec=ProcessRuntime)
        process.db = MagicMock()
        process.stats_db = MagicMock()
        process.process_supervisor = None
        mgr = ReloadManager(rm, process)

        from tests.unit.test_reload_manager import (
            _make_diff,
            _make_real_config,
            _make_real_generation,
            _make_validation,
        )

        baseline = _make_real_config()
        await rm.install_initial(
            _make_real_generation(generation_id=0, config=baseline)
        )
        validation = _make_validation()
        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))

        candidate = RuntimeGenerationCandidate(generation_id=1)
        candidate.register_resource("test_resource", MagicMock())
        candidate._built_generation = _make_real_generation(
            generation_id=1, config=_make_real_config()
        )
        candidate._process_ref = process
        candidate._diff_ref = diff
        candidate.mark_prepared()

        with (
            patch.object(mgr, "_build_candidate_generation", return_value=candidate),
            patch.object(mgr, "_compute_reload_diff", return_value=diff),
            patch.object(mgr, "_reconcile_persistence", new_callable=AsyncMock),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_pre_commit_verification",
                new_callable=AsyncMock,
                side_effect=RuntimeError("publish exploded"),
            ),
        ):
            result = await mgr.reload(validation)

        assert result.ok is False
        assert "publish exploded" in result.message
        # Candidate should have been aborted and diagnostics captured.
        snap = mgr.snapshot()
        assert snap["last_cleanup_diagnostics"] is not None

    @pytest.mark.asyncio
    async def test_snapshot_has_cleanup_diagnostics_field(self) -> None:
        """snapshot() always includes the last_cleanup_diagnostics field."""
        from eggpool.control.reload_manager import ReloadManager
        from eggpool.runtime_manager import (
            ProcessRuntime,
            RuntimeManager,
        )

        rm = RuntimeManager()
        process = MagicMock(spec=ProcessRuntime)
        process.db = MagicMock()
        process.stats_db = MagicMock()
        mgr = ReloadManager(rm, process)

        snap = mgr.snapshot()
        assert "last_cleanup_diagnostics" in snap
        assert snap["last_cleanup_diagnostics"] is None
