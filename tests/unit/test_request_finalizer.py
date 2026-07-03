"""Focused cost-precedence tests for RequestFinalizer."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from eggpool.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)


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
