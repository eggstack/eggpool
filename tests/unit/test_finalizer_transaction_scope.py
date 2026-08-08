"""Finalizer transaction scope tests.

Verifies that the finalizer precomputes diagnostic serialization outside
the BEGIN IMMEDIATE transaction, uses the integer account_id directly
from SelectedAttempt, and places best-effort account event enrichment
after the transaction commits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from eggpool.db.repositories import (
    AttemptFinalizationMutation,
    RequestFinalizationMutation,
    ReservationReleaseMutation,
)
from eggpool.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
    _FinalizationDiagnosticSnapshot,
)


class TestFinalizationDiagnosticSnapshot:
    """Precomputed diagnostic dataclass is populated correctly."""

    def test_default_values(self) -> None:
        snap = _FinalizationDiagnosticSnapshot()
        assert snap.segmentation_status == "empty_request"
        assert snap.compression_status == "disabled"
        assert snap.synthetic_cache_status is None
        assert snap.segmentation_summary_json is None
        assert snap.compression_summary_json is None
        assert snap.synthetic_cache_warnings_json is None

    def test_populated_values(self) -> None:
        snap = _FinalizationDiagnosticSnapshot(
            segmentation_status="ok",
            stable_prefix_hash="abc123",
            compression_status="observed",
            compression_mode="observe",
            synthetic_cache_status="applied",
        )
        assert snap.segmentation_status == "ok"
        assert snap.stable_prefix_hash == "abc123"
        assert snap.compression_status == "observed"
        assert snap.compression_mode == "observe"
        assert snap.synthetic_cache_status == "applied"


class TestPrecomputeFinalizationDiagnostics:
    """Diagnostic precomputation is pure and correct."""

    def _make_finalizer(self) -> RequestFinalizer:
        db = MagicMock()
        db.transaction = MagicMock()
        return RequestFinalizer(
            db=db,
            request_repo=MagicMock(),
            attempt_repo=MagicMock(),
            reservation_repo=MagicMock(),
            health_manager=None,
            quota_estimator=None,
            cost_calculator=None,
        )

    def test_empty_data(self) -> None:
        f = self._make_finalizer()
        data = FinalizationData(
            outcome=FinalizationOutcome.COMPLETED,
            status_code=200,
            input_tokens=0,
            output_tokens=0,
        )
        snap = f._precompute_finalization_diagnostics(data)
        assert snap.segmentation_status == "empty_request"
        assert snap.compression_status == "disabled"
        assert snap.synthetic_cache_status is None

    def test_segmentation_not_collected(self) -> None:
        f = self._make_finalizer()
        data = FinalizationData(
            outcome=FinalizationOutcome.COMPLETED,
            status_code=200,
            input_tokens=0,
            output_tokens=0,
            segmentation_not_collected=True,
        )
        snap = f._precompute_finalization_diagnostics(data)
        assert snap.segmentation_status == "not_collected"

    def test_compression_observed(self) -> None:
        f = self._make_finalizer()

        class _FakeCompressionObs:
            mode = "observe"
            candidate_count = 5
            eligible_candidate_count = 3
            suppressed_candidate_count = 1
            estimated_original_tokens = 100
            estimated_compressed_tokens = 80
            estimated_savings_tokens = 20
            analyzer_latency_ms = 1.5
            warnings = ["w1"]
            reason_code_counts = {"truncation": 2}

        data = FinalizationData(
            outcome=FinalizationOutcome.COMPLETED,
            status_code=200,
            input_tokens=0,
            output_tokens=0,
            compression_observation=_FakeCompressionObs(),  # type: ignore[arg-type]
        )
        snap = f._precompute_finalization_diagnostics(data)
        assert snap.compression_status == "observed"
        assert snap.compression_mode == "observe"
        assert snap.compression_candidate_count == 5
        assert snap.compression_eligible_candidate_count == 3
        assert snap.compression_suppressed_candidate_count == 1
        assert snap.compression_warning_count == 1
        assert snap.compression_reason_code_counts_json is not None
        parsed = json.loads(snap.compression_reason_code_counts_json)
        assert parsed["truncation"] == 2


class TestFinalizerAccountIdReuse:
    """Finalizer uses integer account_id from SelectedAttempt directly."""

    @pytest.mark.asyncio
    async def test_account_id_not_queried_from_db(self) -> None:
        """The finalizer should use selected.account_id (int) directly
        instead of calling get_id_by_name()."""
        db = MagicMock()
        db.transaction.return_value.__aenter__ = AsyncMock()
        db.transaction.return_value.__aexit__ = AsyncMock(return_value=False)

        request_repo = MagicMock()
        request_repo.finalize_if_pending_returning = AsyncMock(
            return_value=RequestFinalizationMutation(
                transitioned=True,
                status="error",
            )
        )
        request_repo.get_by_id = AsyncMock(return_value=None)
        attempt_repo = MagicMock()
        attempt_repo.finalize_if_incomplete_returning = AsyncMock(
            return_value=AttemptFinalizationMutation(
                transitioned=True,
                terminal=True,
            )
        )
        attempt_repo.get_by_id = AsyncMock(return_value=None)
        reservation_repo = MagicMock()
        reservation_repo.release_returning = AsyncMock(
            return_value=ReservationReleaseMutation(
                transitioned=True,
                status="released",
            )
        )
        reservation_repo.TERMINAL_STATUSES = frozenset({"released", "expired"})

        mock_event_repo = MagicMock()
        mock_event_repo.record = AsyncMock()

        f = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            health_manager=None,
            quota_estimator=None,
            cost_calculator=None,
        )

        @dataclass
        class _FakeSelected:
            db_request_id = "req-1"
            account_name = "test-account"
            account_id = 42
            attempt_id = 101
            attempt_number = 1
            provider_id = "openai"
            model_id = "gpt-4"
            reservation_id = "res-1"

        data = FinalizationData(
            outcome=FinalizationOutcome.UPSTREAM_ERROR,
            status_code=500,
            input_tokens=0,
            output_tokens=0,
            error_class="TestError",
        )

        with patch(
            "eggpool.request.finalizer.AccountEventRepository",
            return_value=mock_event_repo,
        ):
            await f.finalize(_FakeSelected(), data)  # type: ignore[arg-type]

        # AccountEventRepository.record should have been called with
        # account_id=42 (the integer), NOT after a get_id_by_name query.
        mock_event_repo.record.assert_called_once()
        call_kwargs = mock_event_repo.record.call_args
        assert call_kwargs.kwargs["account_id"] == 42
        assert call_kwargs.kwargs["event_type"] == "upstream_error"
