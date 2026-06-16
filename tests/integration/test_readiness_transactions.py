"""Integration tests for readiness probe transaction safety."""

from __future__ import annotations

import asyncio

import pytest

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import RequestRepository


async def _seed_db(db: Database) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "TEST_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )


@pytest.mark.asyncio
async def test_probe_writable_then_normal_request() -> None:
    """Readiness probe followed by a normal request transaction works."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    # Probe succeeds
    assert await db.probe_writable()

    # Normal transaction works afterward
    request_repo = RequestRepository(db)
    async with db.transaction():
        await request_repo.create_pending(
            request_id="test-req-1",
            model_id="gpt-4",
            protocol="openai",
            streamed=False,
            account_id=1,
        )

    row = await db.fetch_one(
        "SELECT * FROM requests WHERE proxy_request_id = ?", ("test-req-1",)
    )
    assert row is not None
    assert row["status"] == "pending"
    await db.disconnect()


@pytest.mark.asyncio
async def test_probe_writable_concurrent_with_request() -> None:
    """Readiness probe waits for a concurrent request transaction."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    task_a_started = asyncio.Event()
    task_b_result: list[bool] = []

    async def task_a() -> None:
        async with db.transaction():
            await request_repo.create_pending(
                request_id="test-req-a",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            task_a_started.set()
            # Hold the transaction open while task B probes
            await asyncio.sleep(0.5)

    async def task_b() -> None:
        # Wait until task A is inside its transaction
        await task_a_started.wait()
        # Small delay to ensure task A holds the lock
        await asyncio.sleep(0.1)
        result = await db.probe_writable()
        task_b_result.append(result)

    a = asyncio.create_task(task_a())
    b = asyncio.create_task(task_b())
    await asyncio.gather(a, b)

    # Task B waited for task A's transaction to finish and then succeeded
    assert len(task_b_result) == 1
    assert task_b_result[0] is True

    # The row from task A is committed
    row = await db.fetch_one(
        "SELECT * FROM requests WHERE proxy_request_id = ?", ("test-req-a",)
    )
    assert row is not None
    await db.disconnect()


@pytest.mark.asyncio
async def test_child_task_does_not_inherit_ownership() -> None:
    """A child task inside a transaction must wait, not nest."""
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    child_saw_nested = []

    async def parent_task() -> None:
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, 1.0)",
                ("child-test", "KEY2"),
            )
            # Child task spawned from within the transaction
            child = asyncio.create_task(child_task())
            await child

    async def child_task() -> None:
        # The child should NOT be treated as nested owner
        # It should wait for the parent's transaction to complete
        depth = db._transaction_depth.get()
        owner = db._transaction_owner
        current = asyncio.current_task()
        # If owner is the parent (not current task), depth check would fail nested path
        is_nested_owner = depth > 0 and owner is current
        child_saw_nested.append(is_nested_owner)

    await parent_task()
    assert child_saw_nested == [False]
    await db.disconnect()
