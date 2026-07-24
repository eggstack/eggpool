"""End-to-end diagnostics matrix using the full ReloadHarness.

Exercises the ``ReloadResultCategory`` enum through the full reload()
path, asserting result category, terminal stage, snapshot presence,
counters, and retirement status.  Moved from the unit test file where
the ``reload_harness`` fixture was not available.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.reload_diagnostics import (
    ReloadResultCategory,
    ReloadRetirementStatus,
)

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


class TestHarnessFullReloadDiagnostics:
    """Integration tests using the full ReloadHarness for end-to-end diagnostics."""

    @pytest.mark.asyncio()
    async def test_harness_success_committed(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Full harness: LIVE changes produce SUCCESS_COMMITTED."""
        result = await reload_harness.reload(reload_harness.candidate_config)
        assert result.ok is True
        assert result.generation is not None
        assert len(result.changed_sections) > 0

        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.SUCCESS_COMMITTED

    @pytest.mark.asyncio()
    async def test_harness_success_noop(self, reload_harness: ReloadHarness) -> None:
        """Full harness: identical config produces SUCCESS_NOOP."""
        result = await reload_harness.reload(reload_harness.initial_config)
        assert result.ok is True
        assert "No configuration changes" in result.message

        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.SUCCESS_NOOP

    @pytest.mark.asyncio()
    async def test_harness_rejected_validation(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Full harness: digest mismatch produces REJECTED_VALIDATION."""
        validation = reload_harness.make_validation(reload_harness.candidate_config)
        wrong_digest = "0" * 64
        result = await reload_harness.reload_manager.reload(
            validation, expected_digest=wrong_digest
        )
        assert result.ok is False

        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.REJECTED_VALIDATION

    @pytest.mark.asyncio()
    async def test_harness_rejected_restart_required(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Full harness: host change produces REJECTED_RESTART_REQUIRED."""
        from eggpool.models.config import AppConfig, ServerConfig

        restart_config = AppConfig(
            server=ServerConfig(host="127.0.0.1", port=9999),
            providers=reload_harness.initial_config.providers,
        )
        result = await reload_harness.reload(restart_config)
        assert result.ok is False
        assert len(result.restart_required) > 0

        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.REJECTED_RESTART_REQUIRED

    @pytest.mark.asyncio()
    async def test_harness_failed_candidate_prepare(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Full harness: build failure produces FAILED_CANDIDATE_PREPARE."""
        reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = RuntimeError(
            "simulated build failure"
        )
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = None

        assert result.ok is False

        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert diag.category == ReloadResultCategory.FAILED_CANDIDATE_PREPARE

    @pytest.mark.asyncio()
    async def test_harness_failed_commit(self, reload_harness: ReloadHarness) -> None:
        """Full harness: publish failure produces FAILED_COMMIT."""
        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
            "simulated publish failure"
        )
        try:
            result = await reload_harness.reload()
        finally:
            reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

        assert result.ok is False

        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert diag.category in (
            ReloadResultCategory.FAILED_COMMIT,
            ReloadResultCategory.FAILED_PUBLICATION,
            ReloadResultCategory.COMPENSATION_FAILED,
            ReloadResultCategory.FAILED_PROCESS_TRANSITION_APPLY,
        )

    @pytest.mark.asyncio()
    async def test_harness_snapshot_includes_diagnostic(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Full harness: snapshot includes last_diagnostic_result after reload."""
        await reload_harness.reload()

        snapshot = reload_harness.reload_manager.snapshot()
        diag = snapshot["last_diagnostic_result"]
        assert diag is not None
        assert isinstance(diag, dict)
        assert "category" in diag
        assert "terminal_stage" in diag

    @pytest.mark.asyncio()
    async def test_harness_counters_in_snapshot(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Full harness: snapshot counters include all expected fields."""
        await reload_harness.reload()

        snapshot = reload_harness.reload_manager.snapshot()
        counters = snapshot["counters"]
        expected_keys = {
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
            # Plan 019 Workstream G3: finalization counters.
            "accepted_reloads",
            "fully_finalized_reloads",
            "accepted_finalization_failures",
            "accepted_finalization_retries",
            "retirement_retry_count",
            # Plan 020 Workstream C2: completion reconciliation counters.
            "accepted_finalization_failures_recovered",
            "delayed_completion_count",
        }
        assert expected_keys == set(counters.keys())

    @pytest.mark.asyncio()
    async def test_harness_retirement_status_on_success(
        self, reload_harness: ReloadHarness
    ) -> None:
        """Full harness: retirement status is a ReloadRetirementStatus on success."""
        await reload_harness.reload(reload_harness.candidate_config)

        diag = reload_harness.reload_manager._last_diagnostic_result
        assert diag is not None
        assert isinstance(diag.retirement, ReloadRetirementStatus)
        assert isinstance(diag.retirement.retirement_pending, bool)
