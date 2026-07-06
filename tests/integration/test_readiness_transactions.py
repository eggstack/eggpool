"""Integration tests for readiness probe transaction safety."""

from __future__ import annotations

import asyncio

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import RequestRepository


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
        owner = db._transaction_owner.get()
        current = asyncio.current_task()
        # If owner is the parent (not current task), depth check would fail nested path
        is_nested_owner = depth > 0 and owner is current
        child_saw_nested.append(is_nested_owner)

    await parent_task()
    assert child_saw_nested == [False]
    await db.disconnect()


@pytest.mark.asyncio
async def test_unrelated_task_waits_for_outer_transaction() -> None:
    """Task B must not piggyback on task A's transaction.

    Regression for the bug where ``db.transaction()`` used
    ``conn.in_transaction`` as a global nesting signal: any
    unrelated coroutine that entered ``db.transaction()`` while
    task A held the lock would silently execute inside A's
    transaction. The fix is to require per-task
    ``_in_transaction_context`` inheritance, so unrelated tasks
    acquire ``_connection_lock`` and wait for A to commit.
    """
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    task_a_started = asyncio.Event()
    a_executions: list[bool] = []
    b_executions: list[bool] = []
    b_order: list[int] = []
    counter = 0

    async def task_a() -> None:
        nonlocal counter
        async with db.transaction():
            await request_repo.create_pending(
                request_id="tx-isolation-a",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            task_a_started.set()
            # Hold the transaction open briefly so task B is forced
            # to serialize behind it.
            await asyncio.sleep(0.2)
            a_executions.append(True)

    async def task_b() -> None:
        nonlocal counter
        await task_a_started.wait()
        # Task B enters db.transaction() while task A still holds it.
        # It MUST wait for task A to commit; the writes inside B must
        # land in a separate transaction.
        async with db.transaction():
            counter += 1
            b_order.append(counter)
            await request_repo.create_pending(
                request_id="tx-isolation-b",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            b_executions.append(True)

    a = asyncio.create_task(task_a())
    b = asyncio.create_task(task_b())

    # Wait until task A is inside its transaction, then confirm
    # task B has not yet entered its own.
    await task_a_started.wait()
    await asyncio.sleep(0.05)
    assert not b.done()
    assert b_executions == []

    await asyncio.gather(a, b)

    assert b_executions == [True]
    assert a_executions == [True]

    # Both rows committed, in task A then task B order.
    rows = await db.fetch_all("SELECT proxy_request_id FROM requests ORDER BY id")
    assert [row["proxy_request_id"] for row in rows] == [
        "tx-isolation-a",
        "tx-isolation-b",
    ]
    await db.disconnect()


@pytest.mark.asyncio
async def test_probe_writable_does_not_leak_into_outer_transaction() -> None:
    """``probe_writable()`` must not leave a ``health_probe`` row behind.

    Regression for the bug where ``probe_writable()`` silently
    piggybacked on an unrelated request's transaction via the
    ``conn.in_transaction`` fast path. The probe's sentinel
    rollback was swallowed by the nested caller and the probe
    row ended up in the outer transaction's commit.
    """
    db = Database(path=":memory:")
    await db.connect()
    runner = MigrationRunner(db)
    await runner.run()
    await _seed_db(db)

    request_repo = RequestRepository(db)
    holder_started = asyncio.Event()

    async def holder() -> None:
        async with db.transaction():
            await request_repo.create_pending(
                request_id="probe-isolation",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            holder_started.set()
            # Hold the transaction open briefly while the probe
            # serializes behind us on the connection lock.
            await asyncio.sleep(0.2)

    async def prober() -> None:
        await holder_started.wait()
        # The probe runs only after the holder releases the lock
        # (it cannot piggyback on the holder's transaction).
        assert await db.probe_writable() is True

    holder_task = asyncio.create_task(holder())
    probe_task = asyncio.create_task(prober())
    await asyncio.gather(holder_task, probe_task)

    # No probe row should have committed (the probe is rolled back).
    rows = await db.fetch_all("SELECT * FROM health_probe")
    assert rows == []

    # The request row from the holder is committed.
    row = await db.fetch_one(
        "SELECT * FROM requests WHERE proxy_request_id = ?",
        ("probe-isolation",),
    )
    assert row is not None
    await db.disconnect()
