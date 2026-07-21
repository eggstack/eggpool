"""Phase 4 — Candidate resource ownership and abort cleanup tests.

Tests the RuntimeGenerationCandidate container, registration API,
abort behavior, ownership transfer, cancellation shielding, and
cleanup diagnostics.
"""

from __future__ import annotations

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

        await c.abort(RuntimeError("noop"))
        close_fn.assert_not_called()

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
        assert diag.primary_failure == "mid-build failure"
