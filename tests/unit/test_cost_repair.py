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
    provider_cost_microdollars: int | None = None,
) -> str:
    async with db.transaction():
        request_id = await request_repo.create_pending(
            request_id="repair-1",
            model_id="minimax-m3",
            protocol="openai",
            streamed=False,
            account_id=account_id,
            reserved_microdollars=33_000,
            provider_id="minimax",
        )
        await request_repo.update_after_completion(
            request_id,
            status="completed",
            input_tokens=1_000,
            output_tokens=2_000,
            cost_microdollars=cost_microdollars,
            exactness=exactness,
            provider_cost_microdollars=provider_cost_microdollars,
        )
    return request_id


class TestRepairRequestCosts:
    @pytest.mark.asyncio
    async def test_dry_run_flags_suspicious_rows_without_writing(self) -> None:
        db, request_repo, account_id = await _fresh_db()
        try:
            request_id = await _create_completed_request(
                db,
                request_repo,
                account_id=account_id,
                cost_microdollars=250_000_000,
                exactness="estimated",
            )

            summary = await repair_request_costs(
                db,
                provider_filter="mini",
                dry_run=True,
            )

            assert summary.scanned == 1
            assert summary.suspicious == 1
            assert summary.repaired == 1
            assert summary.old_total_microdollars == 250_000_000
            assert summary.proposed_total_microdollars == 33_000
            assert summary.changed_rows[0]["new_exactness"] == "estimated"

            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 250_000_000
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
                cost_microdollars=250_000_000,
                exactness="estimated",
            )

            summary = await repair_request_costs(db, dry_run=False)

            assert summary.repaired == 1
            row = await db.fetch_one(
                "SELECT cost_microdollars, exactness FROM requests WHERE id = ?",
                (request_id,),
            )
            assert row is not None
            assert int(row["cost_microdollars"]) == 33_000
            assert row["exactness"] == "estimated"

            audit = await db.fetch_one(
                "SELECT old_cost_microdollars, new_cost_microdollars, "
                "old_exactness, new_exactness, reason "
                "FROM request_cost_repairs WHERE request_id = ?",
                (request_id,),
            )
            assert audit is not None
            assert int(audit["old_cost_microdollars"]) == 250_000_000
            assert int(audit["new_cost_microdollars"]) == 33_000
            assert audit["old_exactness"] == "estimated"
            assert audit["new_exactness"] == "estimated"

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
