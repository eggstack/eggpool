"""Finalization supervisor startup/shutdown and diagnostics."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from eggpool.request.finalization_job import (
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


class TestStartupReconciliation:
    """Startup stale-state reconciliation."""

    def test_reconcile_startup_state_empty(self) -> None:
        db = MagicMock()

        async def _fetch_empty() -> list[object]:
            return []

        db.fetch_all = MagicMock(return_value=_fetch_empty())
        sup = RequestFinalizationSupervisor(db=db)

        async def _reconcile() -> int:
            return await sup.reconcile_startup_state()

        result = asyncio.run(_reconcile())
        assert result == 0

    def test_reconcile_startup_state_with_stale_requests(self) -> None:
        db = MagicMock()
        rows: list[object] = [
            {"proxy_request_id": "req-1", "status": "pending"},
            {"proxy_request_id": "req-2", "status": "pending"},
        ]

        async def _fetch_rows() -> list[object]:
            return rows

        db.fetch_all = MagicMock(return_value=_fetch_rows())
        sup = RequestFinalizationSupervisor(db=db)

        async def _reconcile() -> int:
            return await sup.reconcile_startup_state()

        result = asyncio.run(_reconcile())
        assert result == 2
        snap = sup.snapshot()
        assert snap["counters"]["startup_reconciled"] == 2

    def test_reconcile_startup_state_query_failure(self) -> None:
        db = MagicMock()
        db.fetch_all = MagicMock(side_effect=RuntimeError("db error"))
        sup = RequestFinalizationSupervisor(db=db)

        async def _reconcile() -> int:
            return await sup.reconcile_startup_state()

        result = asyncio.run(_reconcile())
        assert result == 0


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
