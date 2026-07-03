"""Tests for suspicious historical request-cost repair tooling."""

from __future__ import annotations

import pytest

from eggpool.cost_repair import repair_request_costs
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import RequestRepository


async def _fresh_db() -> tuple[Database, RequestRepository, int]:
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight, provider_id) "
            "VALUES (?, ?, 1, 1.0, ?)",
            ("repair-acct", "REPAIR_KEY", "minimax"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("minimax-m3", "openai"),
        )
    row = await db.fetch_one("SELECT id FROM accounts WHERE name = ?", ("repair-acct",))
    assert row is not None
    return db, RequestRepository(db), int(row["id"])


async def _create_completed_request(
    db: Database,
    request_repo: RequestRepository,
    *,
    account_id: int,
    cost_microdollars: int,
    exactness: str,
    request_id: str = "repair-1",
    model_id: str = "minimax-m3",
    provider_id: str = "minimax",
    protocol: str = "openai",
    reserved_microdollars: int = 33_000,
    input_tokens: int = 1_000,
    output_tokens: int = 2_000,
    local_cost_microdollars: int | None = None,
    local_cost_exactness: str | None = None,
    provider_cost_microdollars: int | None = None,
    provider_cost_source: str | None = None,
) -> str:
    async with db.transaction():
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            (model_id, protocol),
        )
        request_id = await request_repo.create_pending(
            request_id=request_id,
            model_id=model_id,
            protocol=protocol,
            streamed=False,
            account_id=account_id,
            reserved_microdollars=reserved_microdollars,
            provider_id=provider_id,
        )
        await request_repo.update_after_completion(
            request_id,
            status="completed",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microdollars=cost_microdollars,
            exactness=exactness,
            provider_cost_microdollars=provider_cost_microdollars,
            provider_cost_source=provider_cost_source,
            local_cost_microdollars=local_cost_microdollars,
            local_cost_exactness=local_cost_exactness,
        )
    return request_id


class TestRepairRequestCosts:
    @pytest.mark.asyncio
    async def test_dry_run_flags_reservation_fallback_rows_without_writing(
        self,
    ) -> None:
        db, request_repo, account_id = await _fresh_db()
        try:
            request_id = await _create_completed_request(
                db,
                request_repo,
                account_id=account_id,
                request_id="repair-minimax-1",
                model_id="MiniMax-M3",
                provider_id="minimax",
                reserved_microdollars=5_411_079,
                input_tokens=353,
                output_tokens=1_386,
                cost_microdollars=5_411_079,
                exactness="estimated",
                local_cost_microdollars=21_848,
                local_cost_exactness="estimated",
            )

            summary = await repair_request_costs(
                db,
                provider_filter="mini",
                dry_run=True,
            )

            assert summary.scanned == 1
            assert summary.suspicious == 1
            assert summary.repaired == 1
            assert summary.old_total_microdollars == 5_411_079
            assert summary.proposed_total_microdollars == 21_848
            assert summary.changed_rows[0]["reason"] == (
                "reservation_fallback_overrode_lower_local_estimate"
            )
            assert summary.changed_rows[0]["new_exactness"] == "estimated"

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 5_411_079
            assert row["exactness"] == "estimated"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_apply_updates_row_and_writes_audit_entry(self) -> None:
        db, request_repo, account_id = await _fresh_db()
        try:
            request_id = await _create_completed_request(
                db,
                request_repo,
                account_id=account_id,
                request_id="repair-minimax-apply-1",
                model_id="MiniMax-M3",
                provider_id="minimax",
                reserved_microdollars=5_411_079,
                input_tokens=353,
                output_tokens=1_386,
                cost_microdollars=5_411_079,
                exactness="estimated",
                local_cost_microdollars=21_848,
                local_cost_exactness="estimated",
            )

            summary = await repair_request_costs(db, dry_run=False)

            assert summary.repaired == 1
            assert summary.changed_rows[0]["reason"] == (
                "reservation_fallback_overrode_lower_local_estimate"
            )
            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 21_848
            assert row["exactness"] == "estimated"

            audit = await db.fetch_one(
                "SELECT old_cost_microdollars, new_cost_microdollars, "
                "old_exactness, new_exactness, reason "
                "FROM request_cost_repairs WHERE request_id = ?",
                (request_id,),
            )
            assert audit is not None
            assert int(audit["old_cost_microdollars"]) == 5_411_079
            assert int(audit["new_cost_microdollars"]) == 21_848
            assert audit["old_exactness"] == "estimated"
            assert audit["new_exactness"] == "estimated"
            assert (
                audit["reason"] == "reservation_fallback_overrode_lower_local_estimate"
            )

            second = await repair_request_costs(db, dry_run=False)
            assert second.repaired == 0
            count_row = await db.fetch_one(
                "SELECT COUNT(*) AS n FROM request_cost_repairs WHERE request_id = ?",
                (request_id,),
            )
            assert count_row is not None
            assert int(count_row["n"]) == 1
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_newly_finalized_minimax_row_is_not_flagged(self) -> None:
        db, request_repo, account_id = await _fresh_db()
        try:
            request_id = await _create_completed_request(
                db,
                request_repo,
                account_id=account_id,
                request_id="repair-minimax-clean-1",
                model_id="MiniMax-M3",
                provider_id="minimax",
                reserved_microdollars=5_411_079,
                input_tokens=353,
                output_tokens=1_386,
                cost_microdollars=21_848,
                exactness="estimated",
                local_cost_microdollars=21_848,
                local_cost_exactness="estimated",
            )

            summary = await repair_request_costs(db, dry_run=True)

            assert summary.scanned == 1
            assert summary.suspicious == 0
            assert summary.repaired == 0
            assert summary.changed_rows == []

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 21_848
            assert row["exactness"] == "estimated"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio
    async def test_provider_reported_rows_are_skipped(self) -> None:
        db, request_repo, account_id = await _fresh_db()
        try:
            request_id = await _create_completed_request(
                db,
                request_repo,
                account_id=account_id,
                cost_microdollars=250_000_000,
                exactness="provider_reported",
                provider_cost_microdollars=250_000_000,
                provider_cost_source="usage.cost_usd",
            )

            summary = await repair_request_costs(db, dry_run=False)

            assert summary.skipped_provider_reported == 1
            assert summary.suspicious == 0
            assert summary.repaired == 0
            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 250_000_000
            assert row["exactness"] == "provider_reported"
        finally:
            await db.disconnect()

    @pytest.mark.parametrize(
        "local_cost_microdollars",
        [None, 0, 50_000],
    )
    @pytest.mark.asyncio
    async def test_rows_with_missing_zero_or_higher_local_estimates_are_skipped(
        self, local_cost_microdollars: int | None
    ) -> None:
        db, request_repo, account_id = await _fresh_db()
        try:
            request_id = await _create_completed_request(
                db,
                request_repo,
                account_id=account_id,
                request_id=f"repair-skip-{local_cost_microdollars}",
                model_id="MiniMax-M3",
                provider_id="minimax",
                reserved_microdollars=33_000,
                cost_microdollars=33_000,
                exactness="estimated",
                local_cost_microdollars=local_cost_microdollars,
                local_cost_exactness=(
                    "estimated" if local_cost_microdollars is not None else None
                ),
            )

            summary = await repair_request_costs(db, dry_run=True)

            assert summary.scanned == 1
            assert summary.suspicious == 0
            assert summary.repaired == 0
            assert summary.changed_rows == []

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 33_000
            assert row["exactness"] == "estimated"
        finally:
            await db.disconnect()
