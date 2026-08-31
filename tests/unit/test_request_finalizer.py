"""Focused cost-precedence tests for RequestFinalizer."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from eggpool.request.finalizer import (
    AttemptRuntimeLease,
    DurableFinalizationResult,
    DurableTerminalConflictError,
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)


@pytest.mark.asyncio
async def test_already_terminal_durable_state_converges_owned_runtime_outcomes() -> (
    None
):
    calls = {"usage": 0, "health_release": 0, "health": 0, "account": 0}

    class Quota:
        async def remove_reservation(self, *args: object, **kwargs: object) -> None:
            pass

        async def record_usage_and_snapshot(
            self, *args: object, **kwargs: object
        ) -> None:
            calls["usage"] += 1

    class Router:
        async def decrement_active_request_count(self, account_name: str) -> None:
            pass

    class Health:
        async def release_request(self, account_name: str) -> None:
            calls["health_release"] += 1

        def record_failure(self, *args: object, **kwargs: object) -> None:
            calls["health"] += 1

    class AccountState:
        def record_failure(self, reason: str) -> None:
            calls["account"] += 1

    class Registry:
        def get_state(self, account_name: str) -> AccountState:
            return AccountState()

    finalizer = object.__new__(RequestFinalizer)
    finalizer._router = Router()
    finalizer._quota_estimator = Quota()
    finalizer._health_manager = Health()
    finalizer._registry = Registry()
    finalizer._effects_applier = None
    finalizer._quarantine = None
    selected = SimpleNamespace(
        account_name="acct",
        model_id="model",
        estimated_tokens=10,
        estimated_microdollars=20,
        provider_id="provider",
        protocol="openai",
    )
    lease = AttemptRuntimeLease(
        account_name="acct",
        estimated_tokens=10,
        estimated_microdollars=20,
        active_count_acquired=True,
        quota_reservation_acquired=True,
        health_probe_acquired=True,
        usage_outcome_required=True,
        health_outcome_required=True,
        account_runtime_outcome_required=True,
    )
    durable = DurableFinalizationResult(
        request_terminal=True,
        request_transitioned=False,
        attempt_transitioned=False,
        attempt_terminal=True,
        reservation_terminal=True,
        reservation_transitioned=False,
        cost_microdollars=20,
    )

    await finalizer.apply_runtime_convergence(
        selected=selected,
        data=FinalizationData(
            outcome=FinalizationOutcome.UPSTREAM_ERROR,
            input_tokens=3,
            output_tokens=2,
        ),
        durable=durable,
        runtime_lease=lease,
    )
    await finalizer.apply_runtime_convergence(
        selected=selected,
        data=FinalizationData(outcome=FinalizationOutcome.UPSTREAM_ERROR),
        durable=durable,
        runtime_lease=lease,
    )

    assert lease.released
    assert calls == {"usage": 1, "health_release": 1, "health": 1, "account": 1}


async def _fresh_finalizer_db() -> tuple[
    Database,
    RequestRepository,
    AttemptRepository,
    ReservationRepository,
]:
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) "
            "VALUES (?, ?, 1, 1.0, ?)",
            ("finalizer-acct", "FINALIZER_KEY", "opencode-go"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
    return (
        db,
        RequestRepository(db),
        AttemptRepository(db),
        ReservationRepository(db),
    )


async def _seed_request(
    db: Database,
    request_repo: RequestRepository,
    attempt_repo: AttemptRepository,
    reservation_repo: ReservationRepository,
    *,
    proxy_request_id: str = "finalizer-1",
    model_id: str = "gpt-4",
    protocol: str = "openai",
    provider_id: str = "opencode-go",
    reservation_microdollars: int = 100_000,
    selected_estimated_microdollars: int | None = None,
    estimated_tokens: int = 1000,
) -> tuple[SimpleNamespace, str]:
    if selected_estimated_microdollars is None:
        selected_estimated_microdollars = reservation_microdollars
    async with db.transaction():
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            (model_id, protocol),
        )
        request_id = await request_repo.create_pending(
            request_id=proxy_request_id,
            model_id=model_id,
            protocol=protocol,
            streamed=False,
            account_id=1,
            reserved_microdollars=reservation_microdollars,
            provider_id=provider_id,
        )
        attempt_id = await attempt_repo.create(
            request_id=request_id,
            attempt_number=1,
            account_id=1,
        )
        reservation_id = await reservation_repo.create(
            request_id=request_id,
            account_id=1,
            model_id=model_id,
            estimated_tokens=estimated_tokens,
            estimated_microdollars=reservation_microdollars,
            ttl_seconds=300,
        )
    selected = SimpleNamespace(
        db_request_id=request_id,
        account_name="finalizer-acct",
        model_id=model_id,
        attempt_id=attempt_id,
        reservation_id=reservation_id,
        estimated_microdollars=selected_estimated_microdollars,
        attempt_number=1,
        provider_id=provider_id,
        protocol=protocol,
    )
    return selected, request_id


class TestRequestFinalizerCostPrecedence:
    @pytest.mark.asyncio
    async def test_provider_reported_cost_wins_over_local_cost(self) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
            )
            calculator = AsyncMock()
            calculator.calculate_cost = AsyncMock(return_value=(15_000, "derived"))
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
                cost_calculator=calculator,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                    input_tokens=1_000,
                    output_tokens=1_000,
                    provider_cost_microdollars=22_000,
                    provider_cost_source="usage.cost_usd",
                ),
            )

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness, provider_cost_microdollars, "
                "local_cost_microdollars, local_cost_exactness "
                "FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 22_000
            assert row["exactness"] == "provider_reported"
            assert int(row["provider_cost_microdollars"]) == 22_000
            assert int(row["local_cost_microdollars"]) == 15_000
            assert row["local_cost_exactness"] == "derived"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_reasoning_only_usage_reaches_cost_calculator(self) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
                proxy_request_id="finalizer-reasoning-only-1",
                reservation_microdollars=0,
            )
            calculator = AsyncMock()
            calculator.calculate_cost = AsyncMock(return_value=(1_500, "derived"))
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
                cost_calculator=calculator,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    reasoning_tokens=100,
                ),
            )

            call = calculator.calculate_cost.await_args
            assert call is not None
            assert call.kwargs["reasoning_tokens"] == 100
            row = await db.fetch_one(
                "SELECT cost_microdollars FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert row["cost_microdollars"] == 1_500
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_cache_pricing_uses_reported_usage_dialect(self) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, _request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
                proxy_request_id="finalizer-cache-dialect-1",
            )
            calculator = AsyncMock()
            calculator.calculate_cost = AsyncMock(return_value=(1_500, "derived"))
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
                cost_calculator=calculator,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    input_tokens=100,
                    cache_read_tokens=80,
                    upstream_protocol="openai",
                    normalized_usage=SimpleNamespace(input_tokens_include_cache=False),
                ),
            )

            call = calculator.calculate_cost.await_args
            assert call is not None
            assert call.kwargs["input_tokens_include_cache"] is False
        finally:
            await db.disconnect()


@pytest.mark.asyncio
async def test_first_finalization_avoids_convergence_selects() -> None:
    """A first terminalization needs no read-after-write convergence queries."""
    db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
    try:
        selected, _request_id = await _seed_request(
            db,
            request_repo,
            attempt_repo,
            reservation_repo,
        )
        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
        )

        with patch.object(db, "fetch_one", wraps=db.fetch_one) as fetch_one:
            result = await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                    input_tokens=10,
                    output_tokens=20,
                ),
            )

        assert result.durable_converged
        assert result.request_transitioned
        assert result.attempt_transitioned
        assert result.reservation_transitioned
        fetch_one.assert_not_awaited()
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_duplicate_finalization_falls_back_to_component_reads() -> None:
    """A replay still reads each unchanged component to prove convergence."""
    db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
    try:
        selected, _request_id = await _seed_request(
            db,
            request_repo,
            attempt_repo,
            reservation_repo,
        )
        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
        )
        data = FinalizationData(
            outcome=FinalizationOutcome.COMPLETED,
            status_code=200,
            input_tokens=10,
            output_tokens=20,
        )
        first = await finalizer.finalize(selected, data)
        assert first.durable_converged

        with patch.object(db, "fetch_one", wraps=db.fetch_one) as fetch_one:
            second = await finalizer.finalize(selected, data)

        assert second.durable_converged
        assert not second.request_transitioned
        assert not second.attempt_transitioned
        assert not second.reservation_transitioned
        assert fetch_one.await_count == 3
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_partial_convergence_reads_terminal_request_and_expired_reservation() -> (
    None
):
    """No-transition components retain focused reads for partial repair."""
    db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
    try:
        selected, request_id = await _seed_request(
            db,
            request_repo,
            attempt_repo,
            reservation_repo,
        )
        async with db.transaction():
            await db.execute_write(
                "UPDATE requests SET status = 'completed', "
                "completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (request_id,),
            )
            await db.execute_write(
                "UPDATE reservations SET status = 'expired', "
                "released_at = CURRENT_TIMESTAMP WHERE id = ?",
                (selected.reservation_id,),
            )

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
        )
        with patch.object(db, "fetch_one", wraps=db.fetch_one) as fetch_one:
            result = await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                ),
            )

        assert result.durable_converged
        assert not result.request_transitioned
        assert result.attempt_transitioned
        assert not result.reservation_transitioned
        assert fetch_one.await_count == 2
    finally:
        await db.disconnect()


@pytest.mark.asyncio
async def test_conflicting_terminal_identity_remains_rejected() -> None:
    db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
    try:
        selected, _request_id = await _seed_request(
            db,
            request_repo,
            attempt_repo,
            reservation_repo,
        )
        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
        )
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.COMPLETED,
                status_code=200,
            ),
        )

        with pytest.raises(DurableTerminalConflictError):
            await finalizer.validate_terminal_identity(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.CLIENT_CANCELLED,
                ),
            )
    finally:
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_estimated_local_cost_beats_higher_reservation_floor_regression(
        self,
    ) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
                proxy_request_id="finalizer-minimax-1",
                model_id="MiniMax-M3",
                provider_id="minimax",
                reservation_microdollars=5_411_079,
                selected_estimated_microdollars=5_411_079,
                estimated_tokens=1_739,
            )
            calculator = AsyncMock()
            calculator.calculate_cost = AsyncMock(return_value=(21_848, "estimated"))
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
                cost_calculator=calculator,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                    input_tokens=353,
                    output_tokens=1_386,
                ),
            )

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness, local_cost_microdollars, "
                "local_cost_exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 21_848
            assert row["exactness"] == "estimated"
            assert int(row["local_cost_microdollars"]) == 21_848
            assert row["local_cost_exactness"] == "estimated"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_higher_reservation_does_not_floor_after_lower_local_selection(
        self,
    ) -> None:
        """When local estimate and reservation are both plausible but local is lower,
        the bounded selector picks the local and reservation must not raise it."""
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
                proxy_request_id="finalizer-higher-res-1",
                reservation_microdollars=80_000,
                selected_estimated_microdollars=80_000,
            )
            calculator = AsyncMock()
            calculator.calculate_cost = AsyncMock(return_value=(60_000, "estimated"))
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
                cost_calculator=calculator,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                    input_tokens=1_000,
                    output_tokens=2_000,
                ),
            )

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness, local_cost_microdollars, "
                "local_cost_exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 60_000
            assert row["exactness"] == "estimated"
            assert int(row["local_cost_microdollars"]) == 60_000
            assert row["local_cost_exactness"] == "estimated"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_reservation_lower_than_local_can_win_when_plausible(self) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
                proxy_request_id="finalizer-plausible-reservation-1",
                reservation_microdollars=40_000,
                selected_estimated_microdollars=40_000,
            )
            calculator = AsyncMock()
            calculator.calculate_cost = AsyncMock(return_value=(80_000, "estimated"))
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
                cost_calculator=calculator,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                    input_tokens=1_000,
                    output_tokens=2_000,
                ),
            )

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness, local_cost_microdollars, "
                "local_cost_exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 40_000
            assert row["exactness"] == "estimated"
            assert int(row["local_cost_microdollars"]) == 80_000
            assert row["local_cost_exactness"] == "estimated"
        finally:
            await db.disconnect()

    @pytest.mark.parametrize(
        ("local_exactness", "local_cost_microdollars"),
        [
            ("exact", 12_345),
            ("derived", 18_900),
            ("partial", 23_456),
        ],
    )
    @pytest.mark.asyncio
    async def test_trusted_local_exactness_still_ignores_reservation(
        self,
        local_exactness: str,
        local_cost_microdollars: int,
    ) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
                proxy_request_id=f"finalizer-{local_exactness}-1",
                reservation_microdollars=1_000_000,
            )
            calculator = AsyncMock()
            calculator.calculate_cost = AsyncMock(
                return_value=(local_cost_microdollars, local_exactness)
            )
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
                cost_calculator=calculator,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                    input_tokens=1_000,
                    output_tokens=1_000,
                    cache_read_tokens=500,
                    cache_write_tokens=200,
                ),
            )

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == local_cost_microdollars
            assert row["exactness"] == local_exactness
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_generic_estimate_is_not_floored_to_implausible_reservation(
        self,
    ) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
                proxy_request_id="finalizer-generic-1",
                reservation_microdollars=1_000_000,
                selected_estimated_microdollars=1_000_000,
                estimated_tokens=1,
            )
            calculator = AsyncMock()
            calculator.calculate_cost = AsyncMock(
                return_value=(250_000_000, "estimated")
            )
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
                cost_calculator=calculator,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                    input_tokens=1,
                    output_tokens=0,
                ),
            )

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness, local_cost_microdollars, "
                "local_cost_exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 5
            assert row["exactness"] == "estimated"
            assert int(row["local_cost_microdollars"]) == 250_000_000
            assert row["local_cost_exactness"] == "estimated"
        finally:
            await db.disconnect()


class TestRequestFinalizerSegmentationStatus:
    @pytest.mark.asyncio
    async def test_not_collected_flag_persists_status(self) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
                proxy_request_id="finalizer-segmentation-not-collected-1",
            )
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                    segmentation_not_collected=True,
                ),
            )

            row = await db.fetch_one(
                "SELECT segmentation_status FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert row["segmentation_status"] == "not_collected"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_missing_segmentation_defaults_to_empty_request(self) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
                proxy_request_id="finalizer-segmentation-empty-1",
            )
            finalizer = RequestFinalizer(
                db=db,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
            )

            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.COMPLETED,
                    status_code=200,
                ),
            )

            row = await db.fetch_one(
                "SELECT segmentation_status FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert row["segmentation_status"] == "empty_request"
        finally:
            await db.disconnect()
