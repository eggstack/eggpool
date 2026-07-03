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
) -> tuple[SimpleNamespace, str]:
    async with db.transaction():
        request_id = await request_repo.create_pending(
            request_id="finalizer-1",
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
            reserved_microdollars=100_000,
            provider_id="opencode-go",
        )
        attempt_id = await attempt_repo.create(
            request_id=request_id,
            attempt_number=1,
            account_id=1,
        )
        reservation_id = await reservation_repo.create(
            request_id=request_id,
            account_id=1,
            model_id="gpt-4",
            estimated_tokens=1000,
            estimated_microdollars=100_000,
            ttl_seconds=300,
        )
    selected = SimpleNamespace(
        db_request_id=request_id,
        account_name="finalizer-acct",
        model_id="gpt-4",
        attempt_id=attempt_id,
        reservation_id=reservation_id,
        estimated_microdollars=100_000,
        attempt_number=1,
        provider_id="opencode-go",
        protocol="openai",
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
    async def test_positive_local_estimated_cost_falls_back_to_reservation(
        self,
    ) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
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
            assert int(row["cost_microdollars"]) == 100_000
            assert row["exactness"] == "estimated"
            assert int(row["local_cost_microdollars"]) == 250_000_000
            assert row["local_cost_exactness"] == "estimated"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_trusted_local_partial_cost_persists_as_canonical(self) -> None:
        db, request_repo, attempt_repo, reservation_repo = await _fresh_finalizer_db()
        try:
            selected, request_id = await _seed_request(
                db,
                request_repo,
                attempt_repo,
                reservation_repo,
            )
            calculator = AsyncMock()
            calculator.calculate_cost = AsyncMock(return_value=(18_900, "partial"))
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
            assert int(row["cost_microdollars"]) == 18_900
            assert row["exactness"] == "partial"
        finally:
            await db.disconnect()
