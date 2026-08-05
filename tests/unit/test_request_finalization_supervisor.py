"""Finalization supervisor startup/shutdown and diagnostics."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from eggpool.request.finalization_job import (
    FinalizationCapacityError,
    FinalizationIdentity,
    RequestFinalizationSupervisor,
)


def _make_identity(**overrides: object) -> FinalizationIdentity:
    defaults = dict(
        proxy_request_id="req-1",
        db_request_id="db-req-1",
        attempt_id=1,
        reservation_id="res-1",
        account_id=10,
        account_name="acct",
        provider_id="openai",
        model_id="gpt-4",
        client_protocol="openai",
        upstream_protocol="openai",
        attempt_number=1,
    )
    defaults.update(overrides)
    return FinalizationIdentity(**defaults)  # type: ignore[arg-type]


class TestShutdownDrain:
    """Shutdown drain and adoption semantics."""

    def test_shutdown_with_no_jobs(self) -> None:
        db = MagicMock()
        sup = RequestFinalizationSupervisor(db=db)

        async def _shutdown() -> int:
            return await sup.shutdown(timeout_s=1.0)

        remaining = asyncio.run(_shutdown())
        assert remaining == 0

    def test_shutdown_drains_completed_jobs(self) -> None:
        db = MagicMock()
        sup = RequestFinalizationSupervisor(db=db)
        job = sup.register_or_get(_make_identity(), "client_cancelled")
        asyncio.run(job.run())

        async def _shutdown() -> int:
            return await sup.shutdown(timeout_s=5.0)

        remaining = asyncio.run(_shutdown())
        assert remaining == 0
        assert sup.active_count == 0

    def test_adopt_for_shutdown(self) -> None:
        db = MagicMock()
        sup = RequestFinalizationSupervisor(db=db)
        sup.register_or_get(
            _make_identity(proxy_request_id="req-1"), "client_cancelled"
        )
        sup.register_or_get(
            _make_identity(proxy_request_id="req-2"), "client_cancelled"
        )
        adopted = sup.adopt_for_shutdown()
        assert adopted == 2
        assert sup.active_count == 0
        snap = sup.snapshot()
        assert snap["shutdown_adopted_count"] == 2

    def test_adopt_for_shutdown_is_idempotent(self) -> None:
        db = MagicMock()
        sup = RequestFinalizationSupervisor(db=db)
        sup.register_or_get(_make_identity(), "client_cancelled")
        adopted1 = sup.adopt_for_shutdown()
        adopted2 = sup.adopt_for_shutdown()
        assert adopted1 == 1
        assert adopted2 == 0


class TestDiagnostics:
    """Diagnostics and snapshot contract."""

    def test_snapshot_shape(self) -> None:
        db = MagicMock()
        sup = RequestFinalizationSupervisor(db=db)
        sup.register_or_get(_make_identity(), "client_cancelled")
        snap = sup.snapshot()
        assert "active_count" in snap
        assert "history_count" in snap
        assert "shutdown_adopted_count" in snap
        assert "oldest_active_age_s" in snap
        assert "active_by_progress" in snap
        assert "counters" in snap
        assert "config" in snap
        counters = snap["counters"]
        assert "registered" in counters
        assert "completed" in counters
        assert "failures_recovered" in counters
        assert "saturation_rejections" in counters
        assert "shutdown_adopted" in counters
        assert "startup_reconciled" in counters

    def test_snapshot_active_by_progress(self) -> None:
        db = MagicMock()
        sup = RequestFinalizationSupervisor(db=db)
        sup.register_or_get(
            _make_identity(proxy_request_id="req-1"), "client_cancelled"
        )
        sup.register_or_get(
            _make_identity(proxy_request_id="req-2"), "client_cancelled"
        )
        snap = sup.snapshot()
        assert snap["active_count"] == 2
        # Both are in CREATED state
        assert snap["active_by_progress"].get("created", 0) == 2

    def test_snapshot_oldest_age(self) -> None:
        db = MagicMock()
        sup = RequestFinalizationSupervisor(db=db)
        sup.register_or_get(_make_identity(), "client_cancelled")
        snap = sup.snapshot()
        assert snap["oldest_active_age_s"] is not None
        assert snap["oldest_active_age_s"] >= 0

    def test_snapshot_empty(self) -> None:
        db = MagicMock()
        sup = RequestFinalizationSupervisor(db=db)
        snap = sup.snapshot()
        assert snap["active_count"] == 0
        assert snap["oldest_active_age_s"] is None


class TestGenerationOwnership:
    """Accepted jobs retain and release exactly one generation reference."""

    def test_duplicate_registration_retains_once_and_releases_once(self) -> None:
        db = MagicMock()
        retained = 0
        released = 0

        def retain() -> None:
            nonlocal retained
            retained += 1

        def release() -> None:
            nonlocal released
            released += 1

        sup = RequestFinalizationSupervisor(
            db=db,
            retain_generation=retain,
            release_generation=release,
        )
        job = sup.register_or_get(_make_identity(), "client_cancelled")
        assert sup.register_or_get(_make_identity(), "client_cancelled") is job
        assert retained == 1
        assert released == 0

        sup._reconcile_completed_jobs()
        assert released == 0

        asyncio.run(job.run())
        sup._reconcile_completed_jobs()
        assert released == 1
        sup._reconcile_completed_jobs()
        assert released == 1

    def test_capacity_rejection_does_not_retain(self) -> None:
        retained = 0

        def retain() -> None:
            nonlocal retained
            retained += 1

        sup = RequestFinalizationSupervisor(
            db=MagicMock(),
            max_active_jobs=0,
            retain_generation=retain,
            release_generation=lambda: None,
        )
        with pytest.raises(FinalizationCapacityError):
            sup.register_or_get(_make_identity(), "client_cancelled")
        assert retained == 0
