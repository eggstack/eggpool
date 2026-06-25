"""Unit tests for AccountBackoffRepository (Phase 4)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import AccountBackoffRepository

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-a", "ENV_A"),
        )
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("acct-b", "ENV_B"),
        )
        await database.execute_insert(
            "INSERT INTO models (model_id, display_name, protocol) VALUES (?, ?, ?)",
            ("gpt-4", "GPT-4", "openai"),
        )
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def repo(db: Database) -> AccountBackoffRepository:
    return AccountBackoffRepository(db)


@pytest.mark.asyncio
async def test_0024_migration_applied(repo: AccountBackoffRepository) -> None:
    """Phase 4 migration creates the account_backoffs table."""
    rows = await repo._db.fetch_all(  # noqa: SLF001
        "SELECT name FROM sqlite_master WHERE type='table' AND name='account_backoffs'"
    )
    assert len(rows) == 1

    migration_rows = await repo._db.fetch_all(  # noqa: SLF001
        "SELECT version FROM _migrations WHERE version = 24"
    )
    assert len(migration_rows) == 1


@pytest.mark.asyncio
async def test_upsert_failure_inserts_row(repo: AccountBackoffRepository) -> None:
    """upsert_failure creates the first row."""
    until = time.time() + 60
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="quota_exhausted",
        status_code=402,
        error_class="PaymentRequired",
        backoff_until=until,
        consecutive_failures=1,
    )
    rows = await repo.list_active()
    assert len(rows) == 1
    assert rows[0]["account_id"] == 1
    assert rows[0]["reason"] == "quota_exhausted"
    assert rows[0]["status_code"] == 402


@pytest.mark.asyncio
async def test_upsert_failure_refreshes_existing_row(
    repo: AccountBackoffRepository,
) -> None:
    """Repeated upserts increment consecutive_failures and update backoff."""
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class="TooManyRequests",
        backoff_until=time.time() + 30,
        consecutive_failures=1,
    )
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class="TooManyRequests",
        backoff_until=time.time() + 60,
        consecutive_failures=2,
    )
    rows = await repo.list_active()
    assert len(rows) == 1
    assert rows[0]["consecutive_failures"] == 2
    # Second backoff (60s) replaced the first (30s).
    assert rows[0]["backoff_until_epoch"] is not None
    assert rows[0]["backoff_until_epoch"] > time.time() + 30


@pytest.mark.asyncio
async def test_clear_success_removes_rows(
    repo: AccountBackoffRepository,
) -> None:
    """clear_success removes rows matching the account/model/reasons filter."""
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class=None,
        backoff_until=time.time() + 60,
        consecutive_failures=1,
    )
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="quota_exhausted",
        status_code=402,
        error_class=None,
        backoff_until=time.time() + 120,
        consecutive_failures=1,
    )
    removed = await repo.clear_success(
        account_id=1, model_id=None, reasons=["rate_limited"]
    )
    assert removed == 1
    rows = await repo.list_active()
    assert len(rows) == 1
    assert rows[0]["reason"] == "quota_exhausted"


@pytest.mark.asyncio
async def test_clear_success_all_reasons(
    repo: AccountBackoffRepository,
) -> None:
    """When ``reasons`` is None, every reason is cleared."""
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class=None,
        backoff_until=time.time() + 60,
        consecutive_failures=1,
    )
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="quota_exhausted",
        status_code=402,
        error_class=None,
        backoff_until=time.time() + 120,
        consecutive_failures=1,
    )
    removed = await repo.clear_success(account_id=1, model_id=None)
    assert removed == 2
    assert await repo.list_active() == []


@pytest.mark.asyncio
async def test_clear_success_scoped_to_model(
    repo: AccountBackoffRepository,
) -> None:
    """A non-None ``model_id`` only removes that pair (and account-wide)."""
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class=None,
        backoff_until=time.time() + 60,
        consecutive_failures=1,
    )
    await repo.upsert_failure(
        account_id=1,
        model_id="gpt-4",
        reason="model_unavailable",
        status_code=404,
        error_class=None,
        backoff_until=None,
        consecutive_failures=1,
    )
    removed = await repo.clear_success(
        account_id=1, model_id="gpt-4", reasons=["model_unavailable"]
    )
    assert removed == 1
    rows = await repo.list_active()
    assert len(rows) == 1
    assert rows[0]["reason"] == "rate_limited"


@pytest.mark.asyncio
async def test_list_active_excludes_expired(
    repo: AccountBackoffRepository,
) -> None:
    """Rows whose ``backoff_until`` has passed are not active."""
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class=None,
        backoff_until=time.time() - 1,
        consecutive_failures=1,
    )
    rows = await repo.list_active()
    assert rows == []


@pytest.mark.asyncio
async def test_list_active_includes_indefinite(
    repo: AccountBackoffRepository,
) -> None:
    """Rows with NULL ``backoff_until`` are always active."""
    await repo.upsert_failure(
        account_id=1,
        model_id="gpt-4",
        reason="model_unavailable",
        status_code=404,
        error_class=None,
        backoff_until=None,
        consecutive_failures=1,
    )
    rows = await repo.list_active()
    assert len(rows) == 1
    assert rows[0]["backoff_until_epoch"] is None


@pytest.mark.asyncio
async def test_expire_old_removes_expired(
    repo: AccountBackoffRepository,
) -> None:
    """expire_old deletes expired rows but keeps indefinite rows."""
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class=None,
        backoff_until=time.time() - 10,
        consecutive_failures=1,
    )
    await repo.upsert_failure(
        account_id=2,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class=None,
        backoff_until=time.time() + 100,
        consecutive_failures=1,
    )
    removed = await repo.expire_old()
    assert removed == 1
    rows = await repo.list_active()
    assert len(rows) == 1
    assert rows[0]["account_id"] == 2


@pytest.mark.asyncio
async def test_clear_account_removes_all_rows(
    repo: AccountBackoffRepository,
) -> None:
    """clear_account wipes every backoff for the account."""
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class=None,
        backoff_until=time.time() + 60,
        consecutive_failures=1,
    )
    await repo.upsert_failure(
        account_id=1,
        model_id="gpt-4",
        reason="model_unavailable",
        status_code=404,
        error_class=None,
        backoff_until=None,
        consecutive_failures=1,
    )
    removed = await repo.clear_account(account_id=1)
    assert removed == 2
    assert await repo.list_active() == []


@pytest.mark.asyncio
async def test_get_for_account_model_returns_both_scopes(
    repo: AccountBackoffRepository,
) -> None:
    """get_for_account_model returns account-wide AND matching model rows."""
    await repo.upsert_failure(
        account_id=1,
        model_id=None,
        reason="rate_limited",
        status_code=429,
        error_class=None,
        backoff_until=time.time() + 60,
        consecutive_failures=1,
    )
    await repo.upsert_failure(
        account_id=1,
        model_id="gpt-4",
        reason="model_unavailable",
        status_code=404,
        error_class=None,
        backoff_until=None,
        consecutive_failures=1,
    )
    await repo.upsert_failure(
        account_id=1,
        model_id="other-model",
        reason="model_unavailable",
        status_code=404,
        error_class=None,
        backoff_until=None,
        consecutive_failures=1,
    )
    rows = await repo.get_for_account_model(account_id=1, model_id="gpt-4")
    reasons = {row["reason"] for row in rows}
    assert reasons == {"rate_limited", "model_unavailable"}
