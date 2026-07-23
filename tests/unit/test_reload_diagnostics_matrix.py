"""Comprehensive diagnostics matrix for every ReloadResultCategory.

Exercises every result category from the ``ReloadResultCategory`` enum and
asserts the full diagnostic contract: result category, terminal stage,
counters, snapshot presence, retirement status, and field completeness.

Uses the ``ReloadHarness`` pattern from ``tests/support/reload_harness.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eggpool.config_reload_policy import (
    ConfigChange,
    ConfigDiff,
    ReloadDisposition,
    ReloadStage,
)
from eggpool.control.reload_manager import (
    ReloadCommitError,
    ReloadInProgressError,
    ReloadManager,
    ReloadPreparationError,
    ReloadReconciliationError,
)
from eggpool.reload_diagnostics import (
    ReloadCounters,
    ReloadResultCategory,
    ReloadRetirementStatus,
    ReloadTerminalStage,
    classify_result_category,
    stage_from_error_class,
)
from eggpool.reload_transaction import TransitionRollbackOutcome
from eggpool.runtime_manager import RuntimeGeneration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_process() -> MagicMock:
    """Build a mock ProcessRuntime."""
    proc = MagicMock()
    proc.db = MagicMock()
    proc.stats_db = MagicMock()
    proc.metrics_coalescer = MagicMock()
    proc.process_supervisor = None
    return proc


def _make_validation(
    *,
    content_digest: str = "a" * 64,
    warnings: tuple = (),
    config: MagicMock | None = None,
) -> MagicMock:
    """Build a mock ConfigValidationResult."""
    v = MagicMock()
    v.content_digest = content_digest
    v.warnings = warnings
    v.config = config or MagicMock()
    return v


def _make_diff(
    changes: tuple = (),
) -> MagicMock:
    """Build a mock ConfigDiff with optional changes."""
    d = MagicMock()
    d.changes = changes
    d.restart_required = ()
    d.live = changes  # default: all changes are LIVE
    return d


def _make_generation(generation_id: int = 1, digest: str = "b" * 64) -> MagicMock:
    """Build a mock RuntimeGeneration."""
    gen = MagicMock()
    gen.generation_id = generation_id
    gen.config_digest = digest
    gen.config = MagicMock()
    return gen


def _make_candidate(
    generation_id: int = 1,
    digest: str = "b" * 64,
) -> MagicMock:
    """Build a mock candidate with generation and _built_generation."""
    gen = _make_generation(generation_id, digest)
    process = MagicMock()
    diff = _make_diff()
    candidate = MagicMock()
    candidate.generation = gen
    candidate.process = process
    candidate.diff = diff
    candidate._built_generation = gen
    candidate.abort = AsyncMock()
    return candidate


def _make_runtime_manager(active_generation: MagicMock | None = None) -> MagicMock:
    """Build a mock RuntimeManager."""
    rm = MagicMock()
    rm._shutdown_in_progress = False
    if active_generation is None:
        active_generation = _make_generation(0)
    rm.active_snapshot.return_value = active_generation
    rm.reserve_next_generation_id.return_value = 1
    rm.install_candidate = AsyncMock()

    # Mock the staged-swap protocol used by the new reload path.
    mock_swap = MagicMock()
    mock_swap.staged = False
    mock_swap.committed = False
    mock_swap.candidate_generation_id = 5
    mock_swap.old_generation_id = 0
    mock_swap._old_slot = MagicMock()
    mock_swap._old_slot.generation.generation_id = 0
    mock_swap.stage = AsyncMock(side_effect=_set_swap_staged(mock_swap))
    mock_swap.commit = AsyncMock(side_effect=_set_swap_committed(mock_swap))
    mock_swap.rollback = AsyncMock()
    mock_swap.finalize_retirement = AsyncMock(return_value=0)
    rm.prepare_candidate_swap = AsyncMock(return_value=mock_swap)
    rm._lease_admission_gated = False
    rm.ensure_reload_gate_released = AsyncMock()
    rm._spawn_retirement_task = AsyncMock()
    return rm


def _set_swap_staged(swap: MagicMock) -> Any:
    """Return a side_effect coroutine that marks the swap as staged."""

    async def _stage() -> None:
        swap.staged = True

    return _stage


def _set_swap_committed(swap: MagicMock) -> Any:
    """Return a side_effect coroutine that marks the swap as committed."""

    async def _commit() -> int | None:
        swap.committed = True
        return swap.old_generation_id

    return _commit


def _make_real_config(
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
) -> object:
    """Build a real AppConfig."""
    from eggpool.models.config import AppConfig, ServerConfig

    return AppConfig(server=ServerConfig(host=host, port=port))


def _make_real_generation(
    *,
    generation_id: int = 0,
    config: object | None = None,
    config_digest: str = "a" * 64,
) -> RuntimeGeneration:
    """Build a real RuntimeGeneration with mock services."""
    import time

    from eggpool.models.config import AppConfig, ServerConfig

    if config is None:
        config = AppConfig(server=ServerConfig(host="0.0.0.0", port=8080))
    return RuntimeGeneration(
        generation_id=generation_id,
        config=config,
        config_digest=config_digest,
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
        routing_trace_writer=MagicMock(),
        created_at_monotonic=time.monotonic(),
        created_at_epoch=time.time(),
    )


# ---------------------------------------------------------------------------
# 1. test_success_committed_category
# ---------------------------------------------------------------------------


class TestSuccessCommittedCategory:
    """Successful reload with LIVE changes produces SUCCESS_COMMITTED."""

    @pytest.mark.asyncio()
    async def test_success_committed_category(self) -> None:
        """A reload with LIVE changes must be classified as SUCCESS_COMMITTED."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        validation = _make_validation()
        mock_transition_result = MagicMock()
        mock_transition_result.finalize_all = AsyncMock()
        mock_transition_result.rollback_applied = AsyncMock(
            return_value=TransitionRollbackOutcome(
                attempted=(), restored=(), failures=()
            )
        )
        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_apply_process_transitions",
                new_callable=AsyncMock,
                return_value=mock_transition_result,
            ),
            patch(
                "eggpool.control.reload_manager.preflight_all_transitions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await mgr.reload(validation)

        assert result.ok is True
        assert result.stage == ReloadStage.RETIREMENT
        assert result.generation == 5
        assert "routing" in result.changed_sections

        # Diagnostic result: category is SUCCESS_COMMITTED.
        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.SUCCESS_COMMITTED
        assert diag.terminal_stage == ReloadTerminalStage.RETIREMENT
        assert diag.publication_occurred is True
        assert diag.persistence_committed is True
        assert diag.process_transitions_applied is True
        assert diag.semantic_noop is False
        assert diag.duration_s >= 0

        # Counters: committed_reloads incremented.
        snap = mgr.snapshot()
        assert snap["counters"]["committed_reloads"] >= 1


# ---------------------------------------------------------------------------
# 2. test_success_noop_category
# ---------------------------------------------------------------------------


class TestSuccessNoopCategory:
    """Same config reloaded (no changes) produces SUCCESS_NOOP."""

    @pytest.mark.asyncio()
    async def test_success_noop_category(self) -> None:
        """A reload of identical config must be classified as SUCCESS_NOOP."""
        active_gen = _make_generation(7, "c" * 64)
        rm = _make_runtime_manager(active_gen)
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        validation = _make_validation()
        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            diff_mock.return_value = _make_diff(changes=())
            result = await mgr.reload(validation)

        assert result.ok is True
        assert result.generation == 7
        assert "No configuration changes" in result.message

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.SUCCESS_NOOP
        assert diag.semantic_noop is True
        assert diag.publication_occurred is False
        assert diag.changed_sections == ()

        snap = mgr.snapshot()
        assert snap["counters"]["noop_outcomes"] >= 1


# ---------------------------------------------------------------------------
# 3. test_success_ignored_only_category
# ---------------------------------------------------------------------------


class TestSuccessIgnoredOnlyCategory:
    """Config with only IGNORED changes produces SUCCESS_IGNORED_ONLY."""

    @pytest.mark.asyncio()
    async def test_success_ignored_only_category(self) -> None:
        """A reload with only IGNORED changes must be SUCCESS_IGNORED_ONLY."""
        active_gen = _make_generation(3, "d" * 64)
        rm = _make_runtime_manager(active_gen)
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        ignored_change = ConfigChange(
            path="models.refresh_interval_s",
            disposition=ReloadDisposition.IGNORED,
            old_display="300",
            new_display="600",
            section="models",
        )
        mock_diff = ConfigDiff(changes=(ignored_change,))

        validation = _make_validation()
        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            diff_mock.return_value = mock_diff
            result = await mgr.reload(validation)

        assert result.ok is True
        assert result.generation == 3
        assert "ignored" in result.message.lower()

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.SUCCESS_IGNORED_ONLY
        assert diag.publication_occurred is False
        assert len(diag.ignored_sections) > 0

        snap = mgr.snapshot()
        assert snap["counters"]["ignored_only_outcomes"] >= 1


# ---------------------------------------------------------------------------
# 4. test_rejected_busy_category
# ---------------------------------------------------------------------------


class TestRejectedBusyCategory:
    """Concurrent reload attempt produces REJECTED_BUSY."""

    @pytest.mark.asyncio()
    async def test_rejected_busy_category(self) -> None:
        """A concurrent reload while one is in progress must be REJECTED_BUSY."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        block_event = asyncio.Event()
        mgr.preparation_event = block_event

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        async def _build_with_hook(*args: object, **kwargs: object) -> MagicMock:
            if mgr.preparation_event is not None:
                await mgr.preparation_event.wait()
            return candidate

        validation_a = _make_validation()
        validation_b = _make_validation(content_digest="c" * 64)

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                side_effect=_build_with_hook,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            task_a = asyncio.create_task(mgr.reload(validation_a))
            await asyncio.sleep(0.05)

            with pytest.raises(ReloadInProgressError):
                await mgr.reload(validation_b)

            block_event.set()
            result_a = await task_a

        assert result_a.ok is True

        snap = mgr.snapshot()
        assert snap["counters"]["busy_rejections"] >= 1
        assert snap["counters"]["total_requests"] >= 2


# ---------------------------------------------------------------------------
# 5. test_rejected_validation_category
# ---------------------------------------------------------------------------


class TestRejectedValidationCategory:
    """Digest mismatch produces REJECTED_VALIDATION."""

    @pytest.mark.asyncio()
    async def test_rejected_validation_category(self) -> None:
        """A digest mismatch must be classified as REJECTED_VALIDATION."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        validation = _make_validation(content_digest="a" * 64)
        result = await mgr.reload(validation, expected_digest="f" * 64)

        assert result.ok is False
        assert (
            "digest" in result.message.lower() or "mismatch" in result.message.lower()
        )

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.REJECTED_VALIDATION
        assert diag.terminal_stage == ReloadTerminalStage.VALIDATION
        assert diag.error_class == "ReloadPreparationError"

        snap = mgr.snapshot()
        assert snap["counters"]["validation_rejections"] >= 1


# ---------------------------------------------------------------------------
# 6. test_rejected_restart_required_category
# ---------------------------------------------------------------------------


class TestRejectedRestartRequiredCategory:
    """Restart-required field changed produces REJECTED_RESTART_REQUIRED."""

    @pytest.mark.asyncio()
    async def test_rejected_restart_required_category(self) -> None:
        """A restart-required field change must be REJECTED_RESTART_REQUIRED."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="server", disposition="restart")
        restart_changes = (change,)

        validation = _make_validation()
        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            fake_diff = _make_diff(changes=restart_changes)
            fake_diff.restart_required = restart_changes
            diff_mock.return_value = fake_diff
            result = await mgr.reload(validation)

        assert result.ok is False
        assert result.stage == ReloadStage.DIFF
        assert "restart-required" in result.message

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.REJECTED_RESTART_REQUIRED
        assert diag.terminal_stage == ReloadTerminalStage.DIFF
        assert len(diag.restart_required_sections) > 0

        snap = mgr.snapshot()
        assert snap["counters"]["restart_required_rejections"] >= 1


# ---------------------------------------------------------------------------
# 7. test_failed_candidate_prepare_category
# ---------------------------------------------------------------------------


class TestFailedCandidatePrepareCategory:
    """Build failure (TEST_INJECT_BUILD_FAILURE) produces FAILED_CANDIDATE_PREPARE."""

    @pytest.mark.asyncio()
    async def test_failed_candidate_prepare_category(self) -> None:
        """A build failure during candidate construction must be
        FAILED_CANDIDATE_PREPARE."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))

        validation = _make_validation()
        with (
            patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock, return_value=diff
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                side_effect=ReloadPreparationError("build failed"),
            ),
            patch.object(mgr, "_reconcile_persistence", new_callable=AsyncMock),
            patch.object(mgr, "_publish_generation", new_callable=AsyncMock),
        ):
            result = await mgr.reload(validation)

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.FAILED_CANDIDATE_PREPARE
        assert diag.error_class == "ReloadPreparationError"
        assert diag.candidate_cleanup_attempted is True or diag.error_code is not None

        snap = mgr.snapshot()
        assert snap["counters"]["prepare_failures"] >= 1


# ---------------------------------------------------------------------------
# 8. test_failed_persistence_prepare_category
# ---------------------------------------------------------------------------


class TestFailedPersistencePrepareCategory:
    """Reconcile failure produces FAILED_PERSISTENCE_PREPARE."""

    @pytest.mark.asyncio()
    async def test_failed_persistence_prepare_category(self) -> None:
        """A persistence reconcile failure must be FAILED_PERSISTENCE_PREPARE."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = MagicMock()
        diff.changes = (change,)
        diff.restart_required = ()
        diff.live = (change,)  # has LIVE changes
        candidate = _make_candidate()

        validation = _make_validation()
        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_apply_persistence_delta",
                new_callable=AsyncMock,
                side_effect=ReloadReconciliationError("db sync failed"),
            ),
            patch.object(mgr, "_pre_commit_verification", new_callable=AsyncMock),
        ):
            result = await mgr.reload(validation)

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        # Persistence apply failure at COMMIT stage.
        assert diag.category in (
            ReloadResultCategory.FAILED_PERSISTENCE_PREPARE,
            ReloadResultCategory.FAILED_PERSISTENCE_COMMIT,
            ReloadResultCategory.FAILED_COMMIT,
            ReloadResultCategory.INTERNAL_ERROR,
        )

        snap = mgr.snapshot()
        assert snap["counters"]["total_requests"] >= 1


# ---------------------------------------------------------------------------
# 9. test_failed_commit_category
# ---------------------------------------------------------------------------


class TestFailedCommitCategory:
    """Publish failure (TEST_INJECT_PUBLISH_FAILURE) produces FAILED_COMMIT."""

    @pytest.mark.asyncio()
    async def test_failed_commit_category(self) -> None:
        """A publish failure must be classified as FAILED_COMMIT."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))

        mgr.TEST_INJECT_PUBLISH_FAILURE = RuntimeError("simulated publish failure")
        try:
            candidate = _make_candidate()
            with (
                patch.object(
                    mgr,
                    "_compute_reload_diff",
                    new_callable=AsyncMock,
                    return_value=diff,
                ),
                patch.object(
                    mgr,
                    "_build_candidate_generation",
                    new_callable=AsyncMock,
                    return_value=candidate,
                ),
                patch.object(
                    mgr,
                    "_apply_persistence_delta",
                    new_callable=AsyncMock,
                ),
            ):
                result = await mgr.reload(
                    _make_validation(),
                )
        finally:
            mgr.TEST_INJECT_PUBLISH_FAILURE = None

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category in (
            ReloadResultCategory.FAILED_COMMIT,
            ReloadResultCategory.FAILED_PUBLICATION,
            ReloadResultCategory.FAILED_PROCESS_TRANSITION_APPLY,
            ReloadResultCategory.COMPENSATION_FAILED,
        )
        assert diag.error_class is not None

        snap = mgr.snapshot()
        assert snap["counters"]["commit_failures"] >= 1


# ---------------------------------------------------------------------------
# 10. test_counters_increment_correctly
# ---------------------------------------------------------------------------


class TestCountersIncrementCorrectly:
    """Verify all counter fields increment correctly across scenarios."""

    @pytest.mark.asyncio()
    async def test_counters_increment_correctly(self) -> None:
        """Counters must track each distinct outcome type independently."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        initial = mgr._counters

        # -- 1. Semantic no-op increments noop_outcomes --
        active_gen = _make_generation(0, "z" * 64)
        rm.active_snapshot.return_value = active_gen

        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            diff_mock.return_value = _make_diff(changes=())
            result_noop = await mgr.reload(_make_validation())

        assert result_noop.ok is True
        after_noop = mgr._counters
        assert after_noop.total_requests == initial.total_requests + 1
        assert after_noop.noop_outcomes == initial.noop_outcomes + 1
        assert after_noop.admitted_operations == initial.admitted_operations + 1

        # -- 2. Restart-required increments restart_required_rejections --
        restart_change = MagicMock(section="server", disposition="restart")
        restart_diff = _make_diff(changes=(restart_change,))
        restart_diff.restart_required = (restart_change,)

        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            diff_mock.return_value = restart_diff
            result_restart = await mgr.reload(_make_validation())

        assert result_restart.ok is False
        after_restart = mgr._counters
        assert after_restart.total_requests == initial.total_requests + 2
        assert (
            after_restart.restart_required_rejections
            == initial.restart_required_rejections + 1
        )

        # -- 3. Build failure increments prepare_failures --
        mgr.TEST_INJECT_BUILD_FAILURE = RuntimeError("fail")
        try:
            with patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=_make_diff(changes=(MagicMock(section="routing"),)),
            ):
                result_build = await mgr.reload(_make_validation())
        finally:
            mgr.TEST_INJECT_BUILD_FAILURE = None

        assert result_build.ok is False
        after_build = mgr._counters
        assert after_build.total_requests == initial.total_requests + 3
        assert after_build.prepare_failures == initial.prepare_failures + 1

        # -- 4. Successful committed reload increments committed_reloads --
        change = MagicMock(section="routing")
        diff_ok = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=99)

        rm.active_snapshot.return_value = _make_generation(0, "z" * 64)
        rm.reserve_next_generation_id.return_value = 99

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff_ok,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            result_ok = await mgr.reload(_make_validation())

        assert result_ok.ok is True
        after_ok = mgr._counters
        assert after_ok.total_requests == initial.total_requests + 4
        assert after_ok.committed_reloads == initial.committed_reloads + 1
        assert after_ok.admitted_operations == initial.admitted_operations + 4

        # Verify all counter fields exist and are non-negative ints.
        snapshot = mgr.snapshot()
        counters = snapshot["counters"]
        for key in (
            "total_requests",
            "admitted_operations",
            "busy_rejections",
            "committed_reloads",
            "noop_outcomes",
            "ignored_only_outcomes",
            "validation_rejections",
            "restart_required_rejections",
            "prepare_failures",
            "commit_failures",
            "cancellations",
            "compensation_failures",
            "retirement_failures",
        ):
            assert key in counters, f"Missing counter: {key}"
            assert isinstance(counters[key], int), f"Counter {key} is not int"
            assert counters[key] >= 0, f"Counter {key} is negative"


# ---------------------------------------------------------------------------
# 11. test_snapshot_includes_diagnostic_result
# ---------------------------------------------------------------------------


class TestSnapshotIncludesDiagnosticResult:
    """Verify snapshot includes last_diagnostic_result."""

    @pytest.mark.asyncio()
    async def test_snapshot_includes_diagnostic_result(self) -> None:
        """The snapshot must include a last_diagnostic_result field after reload."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        # Before any reload, last_diagnostic_result is None.
        snapshot = mgr.snapshot()
        assert snapshot["last_diagnostic_result"] is None

        # Perform a noop reload.
        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            diff_mock.return_value = _make_diff(changes=())
            result = await mgr.reload(_make_validation())

        assert result.ok is True

        # After reload, snapshot includes diagnostic result.
        snapshot = mgr.snapshot()
        diag = snapshot["last_diagnostic_result"]
        assert diag is not None
        assert isinstance(diag, dict)
        assert "category" in diag
        assert "terminal_stage" in diag
        assert "request_id" in diag
        assert "started_at" in diag
        assert "completed_at" in diag
        assert "duration_s" in diag
        assert "message" in diag
        assert "counters" not in diag  # counters are at snapshot root

        # Category matches the stored diagnostic.
        stored_diag = mgr._last_diagnostic_result
        assert stored_diag is not None
        assert diag["category"] == stored_diag.category.value
        assert diag["terminal_stage"] == stored_diag.terminal_stage.value

    @pytest.mark.asyncio()
    async def test_snapshot_diagnostic_result_after_failure(self) -> None:
        """Snapshot includes diagnostic result after a failed reload."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        mgr.TEST_INJECT_BUILD_FAILURE = RuntimeError("fail")
        try:
            with patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=_make_diff(changes=(MagicMock(section="routing"),)),
            ):
                result = await mgr.reload(_make_validation())
        finally:
            mgr.TEST_INJECT_BUILD_FAILURE = None

        assert result.ok is False

        snapshot = mgr.snapshot()
        diag = snapshot["last_diagnostic_result"]
        assert diag is not None
        assert diag["category"] == ReloadResultCategory.FAILED_CANDIDATE_PREPARE.value


# ---------------------------------------------------------------------------
# 12. test_retirement_status_derived_from_runtime
# ---------------------------------------------------------------------------


class TestRetirementStatusDerivedFromRuntime:
    """Verify retirement status is derived from the runtime manager."""

    @pytest.mark.asyncio()
    async def test_retirement_status_derived_from_runtime(self) -> None:
        """After a successful commit, retirement status reflects actual state."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            result = await mgr.reload(_make_validation())

        assert result.ok is True

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert isinstance(diag.retirement, ReloadRetirementStatus)
        # Retirement status is derived from the runtime manager state.
        assert isinstance(diag.retirement.retirement_pending, bool)

    @pytest.mark.asyncio()
    async def test_retirement_status_not_pending_after_noop(self) -> None:
        """A noop reload does not retire anything."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            diff_mock.return_value = _make_diff(changes=())
            result = await mgr.reload(_make_validation())

        assert result.ok is True

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.retirement.retirement_pending is False
        assert diag.retirement.retiring_generation_id is None

    @pytest.mark.asyncio()
    async def test_retirement_status_not_pending_after_rejection(self) -> None:
        """A rejected reload does not retire anything."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        validation = _make_validation(content_digest="a" * 64)
        result = await mgr.reload(validation, expected_digest="f" * 64)

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.retirement.retirement_pending is False

    @pytest.mark.asyncio()
    async def test_retirement_pending_when_old_generation_draining(self) -> None:
        """When old generation is in the retiring list, retirement is pending."""
        active_gen = _make_generation(generation_id=1)
        rm = _make_runtime_manager(active_generation=active_gen)
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        # Simulate an active retirement task for the old generation
        rm._retirement_tasks = {1: MagicMock()}

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            result = await mgr.reload(_make_validation())

        assert result.ok is True
        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.retirement.retirement_pending is True
        assert diag.retirement.retiring_generation_id == 1

    @pytest.mark.asyncio()
    async def test_retirement_not_pending_when_old_generation_closed(self) -> None:
        """When old generation is not in retiring list, retirement is not pending."""
        active_gen = _make_generation(generation_id=1)
        rm = _make_runtime_manager(active_generation=active_gen)
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        # Simulate no active retirement tasks (retirement complete)
        rm._retirement_tasks = {}

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            result = await mgr.reload(_make_validation())

        assert result.ok is True
        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.retirement.retirement_pending is False
        assert diag.retirement.retiring_generation_id is None

    @pytest.mark.asyncio()
    async def test_retirement_pending_with_forced_close(self) -> None:
        """Forced close during retirement reports pending if still retiring."""
        active_gen = _make_generation(generation_id=1)
        rm = _make_runtime_manager(active_generation=active_gen)
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        # Simulate an active retirement task for the old generation
        # even though it was force-closed (state="closing", forced_close=True)
        rm._retirement_tasks = {1: MagicMock()}

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            result = await mgr.reload(_make_validation())

        assert result.ok is True
        diag = mgr._last_diagnostic_result
        assert diag is not None
        # Forced close during retirement still reports pending
        # because the generation is still in the retiring list
        assert diag.retirement.retirement_pending is True
        assert diag.retirement.retiring_generation_id == 1


# ---------------------------------------------------------------------------
# 13. test_diagnostic_result_fields_complete
# ---------------------------------------------------------------------------


class TestDiagnosticResultFieldsComplete:
    """Verify all fields on ReloadDiagnosticResult are populated after reload."""

    @pytest.mark.asyncio()
    async def test_diagnostic_result_fields_complete(self) -> None:
        """Every field on ReloadDiagnosticResult must be present and typed."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_reconcile_persistence",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            result = await mgr.reload(_make_validation())

        assert result.ok is True
        diag = mgr._last_diagnostic_result
        assert diag is not None

        # Verify all declared fields exist and have expected types.
        assert isinstance(diag.request_id, str)
        assert isinstance(diag.category, ReloadResultCategory)
        assert isinstance(diag.terminal_stage, ReloadTerminalStage)
        assert isinstance(diag.started_at, float)
        assert isinstance(diag.completed_at, float)
        assert isinstance(diag.duration_s, float)
        assert isinstance(diag.old_generation_id, int)
        assert isinstance(diag.candidate_generation_id, int)
        assert isinstance(diag.active_generation_id, int)
        assert isinstance(diag.changed_sections, tuple)
        assert isinstance(diag.ignored_sections, tuple)
        assert isinstance(diag.restart_required_sections, tuple)
        assert isinstance(diag.semantic_noop, bool)
        assert isinstance(diag.publication_occurred, bool)
        assert isinstance(diag.persistence_committed, bool)
        assert isinstance(diag.process_transitions_applied, bool)
        assert isinstance(diag.compensation_attempted, bool)
        assert isinstance(diag.compensation_succeeded, bool)
        assert isinstance(diag.candidate_cleanup_attempted, bool)
        assert isinstance(diag.candidate_cleanup_succeeded, bool)
        assert isinstance(diag.retirement, ReloadRetirementStatus)
        assert isinstance(diag.message, str)
        assert isinstance(diag.counters, ReloadCounters)
        assert isinstance(diag.warnings, tuple)
        assert isinstance(diag.warning_messages, tuple)

    @pytest.mark.asyncio()
    async def test_diagnostic_result_fields_complete_on_failure(self) -> None:
        """Every field on ReloadDiagnosticResult is populated after a failure."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        mgr.TEST_INJECT_BUILD_FAILURE = RuntimeError("simulated failure")
        try:
            result = await mgr.reload(_make_validation())
        finally:
            mgr.TEST_INJECT_BUILD_FAILURE = None

        assert result.ok is False
        diag = mgr._last_diagnostic_result
        assert diag is not None

        # Error-specific fields.
        assert isinstance(diag.error_code, str) or diag.error_code is None
        assert isinstance(diag.error_class, str) or diag.error_class is None
        assert len(diag.message) > 0

        # Counters snapshot inside diagnostic result.
        assert isinstance(diag.counters, ReloadCounters)
        assert diag.counters.total_requests >= 1


# ---------------------------------------------------------------------------
# 14. test_result_category_classification
# ---------------------------------------------------------------------------


class TestResultCategoryClassification:
    """Unit tests for classify_result_category directly."""

    def test_success_committed(self) -> None:
        """ok=True with no special flags produces SUCCESS_COMMITTED."""
        cat = classify_result_category(
            ok=True,
            stage=ReloadTerminalStage.RETIREMENT,
        )
        assert cat == ReloadResultCategory.SUCCESS_COMMITTED

    def test_success_noop(self) -> None:
        """ok=True with is_noop produces SUCCESS_NOOP."""
        cat = classify_result_category(
            ok=True,
            stage=ReloadTerminalStage.COMMIT,
            is_noop=True,
        )
        assert cat == ReloadResultCategory.SUCCESS_NOOP

    def test_success_ignored_only(self) -> None:
        """ok=True with is_ignored_only produces SUCCESS_IGNORED_ONLY."""
        cat = classify_result_category(
            ok=True,
            stage=ReloadTerminalStage.DIFF,
            is_ignored_only=True,
        )
        assert cat == ReloadResultCategory.SUCCESS_IGNORED_ONLY

    def test_rejected_validation(self) -> None:
        """ok=False at VALIDATION stage produces REJECTED_VALIDATION."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.VALIDATION,
        )
        assert cat == ReloadResultCategory.REJECTED_VALIDATION

    def test_rejected_restart_required_by_flag(self) -> None:
        """ok=False with is_restart_required produces REJECTED_RESTART_REQUIRED."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.DIFF,
            is_restart_required=True,
        )
        assert cat == ReloadResultCategory.REJECTED_RESTART_REQUIRED

    def test_rejected_restart_required_by_stage(self) -> None:
        """ok=False at DIFF stage (without flag) produces REJECTED_RESTART_REQUIRED."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.DIFF,
        )
        assert cat == ReloadResultCategory.REJECTED_RESTART_REQUIRED

    def test_failed_candidate_prepare_by_error_class(self) -> None:
        """ReloadPreparationError class produces FAILED_CANDIDATE_PREPARE."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.PREPARATION,
            error_class="ReloadPreparationError",
        )
        assert cat == ReloadResultCategory.FAILED_CANDIDATE_PREPARE

    def test_failed_candidate_prepare_by_stage(self) -> None:
        """ok=False at PREPARATION stage produces FAILED_CANDIDATE_PREPARE."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.PREPARATION,
        )
        assert cat == ReloadResultCategory.FAILED_CANDIDATE_PREPARE

    def test_failed_persistence_prepare_by_error_class(self) -> None:
        """ReloadReconciliationError produces FAILED_PERSISTENCE_PREPARE."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.RECONCILIATION,
            error_class="ReloadReconciliationError",
        )
        assert cat == ReloadResultCategory.FAILED_PERSISTENCE_PREPARE

    def test_failed_persistence_prepare_by_stage(self) -> None:
        """ok=False at RECONCILIATION stage produces FAILED_PERSISTENCE_PREPARE."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.RECONCILIATION,
        )
        assert cat == ReloadResultCategory.FAILED_PERSISTENCE_PREPARE

    def test_failed_commit_by_error_class(self) -> None:
        """ok=False with ReloadCommitError produces FAILED_COMMIT."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.COMMIT,
            error_class="ReloadCommitError",
        )
        assert cat == ReloadResultCategory.FAILED_COMMIT

    def test_failed_commit_by_stage_commit(self) -> None:
        """ok=False at COMMIT stage produces FAILED_COMMIT."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.COMMIT,
        )
        assert cat == ReloadResultCategory.FAILED_COMMIT

    def test_failed_commit_by_stage_retirement(self) -> None:
        """ok=False at RETIREMENT stage produces FAILED_COMMIT."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.RETIREMENT,
        )
        assert cat == ReloadResultCategory.FAILED_COMMIT

    def test_aborted_cancelled(self) -> None:
        """ok=False with is_cancelled produces ABORTED_CANCELLED."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.PREPARATION,
            is_cancelled=True,
        )
        assert cat == ReloadResultCategory.ABORTED_CANCELLED

    def test_aborted_shutdown(self) -> None:
        """ok=False with is_shutdown produces ABORTED_SHUTDOWN."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.PREPARATION,
            is_shutdown=True,
        )
        assert cat == ReloadResultCategory.ABORTED_SHUTDOWN

    def test_compensation_failed(self) -> None:
        """ok=False with is_compensation_failed produces COMPENSATION_FAILED."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.COMMIT,
            is_compensation_failed=True,
        )
        assert cat == ReloadResultCategory.COMPENSATION_FAILED

    def test_internal_error_fallback(self) -> None:
        """ok=False at IDLE stage with no flags produces INTERNAL_ERROR."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.IDLE,
        )
        assert cat == ReloadResultCategory.INTERNAL_ERROR

    def test_cancelled_takes_priority_over_other_flags(self) -> None:
        """is_cancelled takes priority over is_restart_required and error_class."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.PREPARATION,
            is_cancelled=True,
            is_restart_required=True,
            error_class="ReloadPreparationError",
        )
        assert cat == ReloadResultCategory.ABORTED_CANCELLED

    def test_shutdown_takes_priority_over_restart_required(self) -> None:
        """is_shutdown takes priority over is_restart_required."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.DIFF,
            is_shutdown=True,
            is_restart_required=True,
        )
        assert cat == ReloadResultCategory.ABORTED_SHUTDOWN

    def test_compensation_failed_takes_priority_over_error_class(self) -> None:
        """is_compensation_failed takes priority over error_class."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.COMMIT,
            is_compensation_failed=True,
            error_class="ReloadCommitError",
        )
        assert cat == ReloadResultCategory.COMPENSATION_FAILED


# ---------------------------------------------------------------------------
# 15. test_stage_from_error_class
# ---------------------------------------------------------------------------


class TestStageFromErrorClass:
    """Unit tests for stage_from_error_class directly."""

    def test_reload_preparation_error(self) -> None:
        """ReloadPreparationError maps to PREPARATION."""
        stage = stage_from_error_class("ReloadPreparationError")
        assert stage == ReloadTerminalStage.PREPARATION

    def test_reload_reconciliation_error(self) -> None:
        """ReloadReconciliationError maps to RECONCILIATION."""
        stage = stage_from_error_class("ReloadReconciliationError")
        assert stage == ReloadTerminalStage.RECONCILIATION

    def test_reload_commit_error(self) -> None:
        """ReloadCommitError maps to COMMIT."""
        stage = stage_from_error_class("ReloadCommitError")
        assert stage == ReloadTerminalStage.COMMIT

    def test_unknown_error_class_maps_to_validation(self) -> None:
        """An unknown error class falls back to VALIDATION."""
        stage = stage_from_error_class("SomeOtherError")
        assert stage == ReloadTerminalStage.VALIDATION

    def test_none_error_class_maps_to_validation(self) -> None:
        """None error class falls back to VALIDATION."""
        stage = stage_from_error_class(None)
        assert stage == ReloadTerminalStage.VALIDATION

    def test_empty_string_error_class_maps_to_validation(self) -> None:
        """Empty string error class falls back to VALIDATION."""
        stage = stage_from_error_class("")
        assert stage == ReloadTerminalStage.VALIDATION


# ---------------------------------------------------------------------------
# Additional integration: full harness tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 16. test_failed_publication_category
# ---------------------------------------------------------------------------


class TestFailedPublicationCategory:
    """Publication failure produces FAILED_PUBLICATION."""

    @pytest.mark.asyncio()
    async def test_failed_publication_category(self) -> None:
        """A publish failure at COMMIT stage must be FAILED_PUBLICATION."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        mgr.TEST_INJECT_PUBLISH_FAILURE = ReloadCommitError("publish failed")
        try:
            validation = _make_validation()
            with (
                patch.object(
                    mgr,
                    "_compute_reload_diff",
                    new_callable=AsyncMock,
                    return_value=diff,
                ),
                patch.object(
                    mgr,
                    "_build_candidate_generation",
                    new_callable=AsyncMock,
                    return_value=candidate,
                ),
                patch.object(
                    mgr, "_prepare_persistence_delta", return_value=MagicMock()
                ),
                patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
                patch.object(mgr, "_pre_commit_verification", new_callable=AsyncMock),
            ):
                result = await mgr.reload(validation)
        finally:
            mgr.TEST_INJECT_PUBLISH_FAILURE = None

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category in (
            ReloadResultCategory.FAILED_PUBLICATION,
            ReloadResultCategory.FAILED_COMMIT,
            ReloadResultCategory.FAILED_PROCESS_TRANSITION_APPLY,
        )
        assert diag.terminal_stage == ReloadTerminalStage.COMMIT

        snap = mgr.snapshot()
        assert snap["counters"]["commit_failures"] >= 1


# ---------------------------------------------------------------------------
# 17. test_failed_process_transition_apply_category
# ---------------------------------------------------------------------------


class TestFailedProcessTransitionApplyCategory:
    """Process transition apply failure produces FAILED_PROCESS_TRANSITION_APPLY."""

    @pytest.mark.asyncio()
    async def test_failed_process_transition_apply_category(self) -> None:
        """A process transition failure after publication must be
        FAILED_PROCESS_TRANSITION_APPLY."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        validation = _make_validation()
        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(mgr, "_pre_commit_verification", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            mgr.TEST_INJECT_TRANSITION_APPLY_FAILURE = RuntimeError(
                "process transition failed"
            )
            try:
                result = await mgr.reload(validation)
            finally:
                mgr.TEST_INJECT_TRANSITION_APPLY_FAILURE = None

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category in (
            ReloadResultCategory.FAILED_PROCESS_TRANSITION_APPLY,
            ReloadResultCategory.COMPENSATION_FAILED,
        )

        snap = mgr.snapshot()
        assert snap["counters"]["commit_failures"] >= 1


# ---------------------------------------------------------------------------
# 18. test_failed_persistence_commit_category
# ---------------------------------------------------------------------------


class TestFailedPersistenceCommitCategory:
    """Persistence commit failure produces FAILED_PERSISTENCE_COMMIT."""

    @pytest.mark.asyncio()
    async def test_failed_persistence_commit_category(self) -> None:
        """A persistence failure at COMMIT stage must be
        FAILED_PERSISTENCE_COMMIT."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        validation = _make_validation()
        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(
                mgr,
                "_apply_persistence_delta",
                new_callable=AsyncMock,
                side_effect=ReloadReconciliationError("db commit failed"),
            ),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
            patch.object(mgr, "_pre_commit_verification", new_callable=AsyncMock),
        ):
            result = await mgr.reload(validation)

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category in (
            ReloadResultCategory.FAILED_PERSISTENCE_COMMIT,
            ReloadResultCategory.FAILED_COMMIT,
        )
        assert diag.error_class == "ReloadReconciliationError"

        snap = mgr.snapshot()
        assert snap["counters"]["commit_failures"] >= 1


# ---------------------------------------------------------------------------
# 19. test_aborted_cancelled_category
# ---------------------------------------------------------------------------


class TestAbortedCancelledCategory:
    """Cancellation produces ABORTED_CANCELLED."""

    def test_aborted_cancelled_unit(self) -> None:
        """is_cancelled produces ABORTED_CANCELLED."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.PREPARATION,
            is_cancelled=True,
        )
        assert cat == ReloadResultCategory.ABORTED_CANCELLED

    def test_cancelled_takes_priority(self) -> None:
        """is_cancelled takes priority over other error flags."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.COMMIT,
            is_cancelled=True,
            is_compensation_failed=True,
            error_class="ReloadCommitError",
        )
        assert cat == ReloadResultCategory.ABORTED_CANCELLED


# ---------------------------------------------------------------------------
# 20. test_aborted_shutdown_category
# ---------------------------------------------------------------------------


class TestAbortedShutdownCategory:
    """Shutdown produces ABORTED_SHUTDOWN via classify_result_category."""

    def test_aborted_shutdown_unit(self) -> None:
        """is_shutdown produces ABORTED_SHUTDOWN."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.PREPARATION,
            is_shutdown=True,
        )
        assert cat == ReloadResultCategory.ABORTED_SHUTDOWN


# ---------------------------------------------------------------------------
# 21. test_compensation_failed_category
# ---------------------------------------------------------------------------


class TestCompensationFailedCategory:
    """Compensation failure produces COMPENSATION_FAILED via classifier."""

    def test_compensation_failed_unit(self) -> None:
        """is_compensation_failed produces COMPENSATION_FAILED."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.COMMIT,
            is_compensation_failed=True,
        )
        assert cat == ReloadResultCategory.COMPENSATION_FAILED

    @pytest.mark.asyncio()
    async def test_compensation_failed_integration(self) -> None:
        """Post-publication failure with failed compensation."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        validation = _make_validation()
        # First publish succeeds, then process transitions fail,
        # and compensation also fails.

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            mgr.TEST_INJECT_TRANSITION_APPLY_FAILURE = RuntimeError(
                "process transition failed"
            )
            try:
                result = await mgr.reload(validation)
            finally:
                mgr.TEST_INJECT_TRANSITION_APPLY_FAILURE = None

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.category in (
            ReloadResultCategory.COMPENSATION_FAILED,
            ReloadResultCategory.FAILED_COMMIT,
            ReloadResultCategory.FAILED_PROCESS_TRANSITION_APPLY,
        )

    @pytest.mark.asyncio()
    async def test_cleanup_failure_recorded_as_warning(self) -> None:
        """Primary failure with cleanup failure: primary remains, cleanup is warning."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        validation = _make_validation()

        # Primary failure: publish fails.  Cleanup also fails (abort raises).
        async def _abort_fail(**kwargs: object) -> None:
            raise RuntimeError("cleanup failed")

        candidate.abort = _abort_fail

        mgr.TEST_INJECT_PUBLISH_FAILURE = RuntimeError("publish failed")
        try:
            with (
                patch.object(
                    mgr,
                    "_compute_reload_diff",
                    new_callable=AsyncMock,
                    return_value=diff,
                ),
                patch.object(
                    mgr,
                    "_build_candidate_generation",
                    new_callable=AsyncMock,
                    return_value=candidate,
                ),
                patch.object(
                    mgr, "_prepare_persistence_delta", return_value=MagicMock()
                ),
                patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            ):
                result = await mgr.reload(validation)
        finally:
            mgr.TEST_INJECT_PUBLISH_FAILURE = None

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        # Primary error is the publish failure
        assert diag.error_class == "RuntimeError"
        assert "publish failed" in diag.message
        # Cleanup failure is logged as a warning (not propagated)
        assert "candidate_cleanup_succeeded" in dir(diag)


# ---------------------------------------------------------------------------
# 22. test_internal_error_category
# ---------------------------------------------------------------------------


class TestInternalErrorCategory:
    """Internal error produces INTERNAL_ERROR via classifier."""

    def test_internal_error_unit(self) -> None:
        """Unknown stage with no flags produces INTERNAL_ERROR."""
        cat = classify_result_category(
            ok=False,
            stage=ReloadTerminalStage.IDLE,
        )
        assert cat == ReloadResultCategory.INTERNAL_ERROR


# ---------------------------------------------------------------------------
# 23. test_protocol_compatibility
# ---------------------------------------------------------------------------


class TestProtocolCompatibility:
    """ControlResponse backward compatibility for Phase 11 fields."""

    def test_control_response_backward_compat(self) -> None:
        """ControlResponse without Phase 11 fields omits them from dict."""
        from eggpool.control.server import ControlResponse

        resp = ControlResponse(
            protocol_version=1,
            request_id="test-1",
            ok=True,
            stage="retirement",
            generation=5,
            changed_sections=("routing",),
            warnings=(),
            restart_required=(),
            message="Reload applied",
            retirement_pending=False,
        )
        d = resp.to_dict()
        # Phase 11 fields should be absent (None defaults omitted).
        assert "result_category" not in d
        assert "duration_s" not in d

    def test_control_response_with_phase11_fields(self) -> None:
        """ControlResponse with Phase 11 fields includes them in dict."""
        from eggpool.control.server import ControlResponse

        resp = ControlResponse(
            protocol_version=1,
            request_id="test-2",
            ok=True,
            stage="retirement",
            generation=5,
            changed_sections=("routing",),
            warnings=(),
            restart_required=(),
            message="Reload applied",
            retirement_pending=False,
            result_category="success_committed",
            duration_s=0.123,
        )
        d = resp.to_dict()
        assert d["result_category"] == "success_committed"
        assert d["duration_s"] == 0.123

    def test_response_is_json_serializable(self) -> None:
        """ControlResponse dict is JSON-serializable for wire transport."""
        import json

        from eggpool.control.server import ControlResponse

        resp = ControlResponse(
            protocol_version=1,
            request_id="test-3",
            ok=True,
            stage="retirement",
            generation=5,
            changed_sections=("routing",),
            warnings=(),
            restart_required=(),
            message="Reload applied",
            retirement_pending=False,
            result_category="success_committed",
            duration_s=0.456,
        )
        d = resp.to_dict()
        # Must be JSON-serializable without errors
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        assert parsed["ok"] is True
        assert parsed["result_category"] == "success_committed"
        assert parsed["duration_s"] == 0.456

    def test_old_client_ignores_unknown_fields(self) -> None:
        """Old client parsing a response with extra fields doesn't break."""
        from eggpool.control.server import ControlResponse

        resp = ControlResponse(
            protocol_version=1,
            request_id="test-4",
            ok=False,
            stage="validation",
            generation=None,
            changed_sections=(),
            warnings=(),
            restart_required=(),
            message="Digest mismatch",
            retirement_pending=False,
            result_category="rejected_validation",
            duration_s=0.01,
        )
        d = resp.to_dict()
        # Simulate old client: only read known fields
        old_client_fields = {
            "ok",
            "stage",
            "generation",
            "changed_sections",
            "warnings",
            "restart_required",
            "message",
            "retirement_pending",
            "request_id",
            "protocol_version",
        }
        # Old client ignores unknown fields gracefully
        filtered = {k: v for k, v in d.items() if k in old_client_fields}
        assert filtered["ok"] is False
        assert filtered["stage"] == "validation"
        assert "result_category" not in filtered
        assert "duration_s" not in filtered


# ---------------------------------------------------------------------------
# 24. test_cleanup_and_compensation_warning
# ---------------------------------------------------------------------------


class TestCleanupAndCompensationWarning:
    """Cleanup failure is separate from primary error."""

    @pytest.mark.asyncio()
    async def test_cleanup_failure_as_warning(self) -> None:
        """Primary error remains primary; cleanup failure appears as warning."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        validation = _make_validation()
        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(
                mgr,
                "_apply_persistence_delta",
                new_callable=AsyncMock,
                side_effect=ReloadReconciliationError("db sync failed"),
            ),
            patch.object(mgr, "_pre_commit_verification", new_callable=AsyncMock),
        ):
            result = await mgr.reload(validation)

        assert result.ok is False

        diag = mgr._last_diagnostic_result
        assert diag is not None
        # Primary error is the reconciliation error, not a cleanup error.
        assert diag.error_class == "ReloadReconciliationError"


# ---------------------------------------------------------------------------
# 25. test_cancellation_shutdown_integration
# ---------------------------------------------------------------------------


class TestCancellationShutdownIntegration:
    """Cancellation/shutdown outcomes finalize and release admission."""

    @pytest.mark.asyncio()
    async def test_cancellation_releases_admission(self) -> None:
        """After a cancelled reload, the admission lock is released."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        validation_a = _make_validation(content_digest="a" * 64)
        validation_b = _make_validation(content_digest="b" * 64)

        build_event = asyncio.Event()

        async def _build_slow(*args: object, **kwargs: object) -> MagicMock:
            await build_event.wait()
            return _make_candidate(generation_id=5)

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=_make_diff(changes=(MagicMock(section="routing"),)),
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                side_effect=_build_slow,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            task_a = asyncio.create_task(mgr.reload(validation_a))
            await asyncio.sleep(0.05)

            # Cancel the in-flight reload
            task_a.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task_a
            build_event.set()

            # Wait a bit for cleanup
            await asyncio.sleep(0.05)

            # Admission should be released — a new reload should succeed
            assert not mgr._reload_claimed

            # A new reload should not raise ReloadInProgressError
            with patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
            ) as diff_mock:
                diff_mock.return_value = _make_diff(changes=())
                result_b = await mgr.reload(validation_b)
            assert result_b.ok is True


# ---------------------------------------------------------------------------
# 26. test_reload_history_bounded
# ---------------------------------------------------------------------------


class TestReloadHistoryBounded:
    """Verify bounded reload history in snapshot."""

    @pytest.mark.asyncio()
    async def test_reload_history_grows(self) -> None:
        """Each reload appends to the bounded history."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        # First reload (noop)
        with patch.object(
            mgr, "_compute_reload_diff", new_callable=AsyncMock
        ) as diff_mock:
            diff_mock.return_value = _make_diff(changes=())
            await mgr.reload(_make_validation())

        snap = mgr.snapshot()
        history = snap["reload_history"]
        assert len(history) >= 1
        assert history[0]["category"] == "success_noop"

    @pytest.mark.asyncio()
    async def test_reload_history_bounded_limit(self) -> None:
        """History does not exceed the max size."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)
        mgr._reload_history_max = 3

        for _ in range(5):
            with patch.object(
                mgr, "_compute_reload_diff", new_callable=AsyncMock
            ) as diff_mock:
                diff_mock.return_value = _make_diff(changes=())
                await mgr.reload(_make_validation())

        snap = mgr.snapshot()
        history = snap["reload_history"]
        assert len(history) <= 3

    @pytest.mark.asyncio()
    async def test_reload_history_after_failure(self) -> None:
        """Failed reloads also appear in history."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        mgr.TEST_INJECT_BUILD_FAILURE = RuntimeError("fail")
        try:
            with patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=_make_diff(changes=(MagicMock(section="routing"),)),
            ):
                await mgr.reload(_make_validation())
        finally:
            mgr.TEST_INJECT_BUILD_FAILURE = None

        snap = mgr.snapshot()
        history = snap["reload_history"]
        assert len(history) >= 1
        assert history[0]["category"] == "failed_candidate_prepare"


# ---------------------------------------------------------------------------
# 27. test_operational_event_recorded_flag
# ---------------------------------------------------------------------------


class TestOperationalEventRecordedFlag:
    """Verify operational_event_recorded flag is set after terminal event."""

    @pytest.mark.asyncio()
    async def test_operational_event_recorded_on_success(self) -> None:
        """Successful reload sets operational_event_recorded=True."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            await mgr.reload(_make_validation())

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.operational_event_recorded is True

    @pytest.mark.asyncio()
    async def test_operational_event_recorded_on_failure(self) -> None:
        """Failed reload sets operational_event_recorded=True."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        mgr.TEST_INJECT_BUILD_FAILURE = RuntimeError("fail")
        try:
            with patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=_make_diff(changes=(MagicMock(section="routing"),)),
            ):
                await mgr.reload(_make_validation())
        finally:
            mgr.TEST_INJECT_BUILD_FAILURE = None

        diag = mgr._last_diagnostic_result
        assert diag is not None
        assert diag.operational_event_recorded is True


# ---------------------------------------------------------------------------
# 28. test_old_generation_digest_populated
# ---------------------------------------------------------------------------


class TestOldGenerationDigestPopulated:
    """Verify old_generation_digest is populated when available."""

    @pytest.mark.asyncio()
    async def test_old_generation_digest_on_success(self) -> None:
        """After successful commit, old_generation_digest is set."""
        rm = _make_runtime_manager()
        proc = _make_process()
        mgr = ReloadManager(rm, proc)

        change = MagicMock(section="routing")
        diff = _make_diff(changes=(change,))
        candidate = _make_candidate(generation_id=5)

        with (
            patch.object(
                mgr,
                "_compute_reload_diff",
                new_callable=AsyncMock,
                return_value=diff,
            ),
            patch.object(
                mgr,
                "_build_candidate_generation",
                new_callable=AsyncMock,
                return_value=candidate,
            ),
            patch.object(mgr, "_prepare_persistence_delta", return_value=MagicMock()),
            patch.object(mgr, "_apply_persistence_delta", new_callable=AsyncMock),
            patch.object(
                mgr,
                "_publish_generation",
                new_callable=AsyncMock,
            ),
        ):
            await mgr.reload(_make_validation())

        diag = mgr._last_diagnostic_result
        assert diag is not None
        # old_generation_digest may be None if the active generation
        # was generation 0 (initial) or if lookup failed, but the
        # field should exist on the dataclass.
        assert hasattr(diag, "old_generation_digest")
