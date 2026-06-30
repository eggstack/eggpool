"""Tests for the /api/backoffs endpoint."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eggpool.api.backoff import register_backoff_routes
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
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def app_with_repo(db: Database) -> tuple[FastAPI, AccountBackoffRepository]:
    app = FastAPI()
    repo = AccountBackoffRepository(db)
    app.state.account_backoff_repo = repo
    app.state.db = db
    register_backoff_routes(app)
    return app, repo


def _populate(repo: AccountBackoffRepository) -> None:
    """Insert three backoff rows covering the documented shape."""
    now = time.time()
    import asyncio

    async def _seed() -> None:
        await repo.upsert_failure(
            account_id=1,
            model_id=None,
            reason="rate_limited",
            status_code=429,
            error_class="RateLimitError",
            backoff_until=now + 120,
            consecutive_failures=2,
        )
        await repo.upsert_failure(
            account_id=1,
            model_id="gpt-4",
            reason="model_unavailable",
            status_code=404,
            error_class="ModelNotFoundError",
            backoff_until=None,
            consecutive_failures=1,
        )
        await repo.upsert_failure(
            account_id=2,
            model_id=None,
            reason="authentication_failed",
            status_code=401,
            error_class="AuthenticationError",
            backoff_until=now + 3600,
            consecutive_failures=1,
        )

    asyncio.run(_seed())


def test_endpoint_returns_empty_list_when_no_backoffs(
    app_with_repo: tuple[FastAPI, AccountBackoffRepository],
) -> None:
    app, _repo = app_with_repo
    with TestClient(app) as client:
        response = client.get("/api/backoffs")
    assert response.status_code == 200
    payload = response.json()
    assert payload["backoffs"] == []
    assert payload["now"] is not None


def test_endpoint_returns_active_backoffs(
    app_with_repo: tuple[FastAPI, AccountBackoffRepository],
) -> None:
    app, repo = app_with_repo
    _populate(repo)
    with TestClient(app) as client:
        response = client.get("/api/backoffs")
    assert response.status_code == 200
    payload = response.json()
    rows = cast("list[dict[str, Any]]", payload["backoffs"])
    assert len(rows) == 3
    by_account = {r["account_name"]: r for r in rows}
    assert by_account["acct-a"]["reason"] in {"rate_limited", "model_unavailable"}
    assert by_account["acct-b"]["reason"] == "authentication_failed"
    assert by_account["acct-a"]["consecutive_failures"] in {1, 2}
    assert by_account["acct-b"]["consecutive_failures"] == 1


def test_endpoint_omits_expired_backoffs(
    app_with_repo: tuple[FastAPI, AccountBackoffRepository],
) -> None:
    app, repo = app_with_repo
    import asyncio

    async def _seed() -> None:
        await repo.upsert_failure(
            account_id=1,
            model_id=None,
            reason="rate_limited",
            status_code=429,
            error_class="RateLimitError",
            backoff_until=time.time() - 60,
            consecutive_failures=1,
        )

    asyncio.run(_seed())
    with TestClient(app) as client:
        response = client.get("/api/backoffs")
    payload = response.json()
    assert payload["backoffs"] == []


def test_endpoint_honors_now_query_parameter(
    app_with_repo: tuple[FastAPI, AccountBackoffRepository],
) -> None:
    app, repo = app_with_repo
    import asyncio

    cutoff = 1_800_000_000.0

    async def _seed() -> None:
        await repo.upsert_failure(
            account_id=1,
            model_id=None,
            reason="rate_limited",
            status_code=429,
            error_class="RateLimitError",
            backoff_until=cutoff + 60,
            consecutive_failures=1,
        )

    asyncio.run(_seed())

    with TestClient(app) as client:
        active_response = client.get(f"/api/backoffs?now={cutoff}")
        expired_response = client.get(f"/api/backoffs?now={cutoff + 120}")

    active_payload = active_response.json()
    assert active_response.status_code == 200
    assert active_payload["now"] == "2027-01-15T08:00:00+00:00"
    assert len(active_payload["backoffs"]) == 1

    assert expired_response.status_code == 200
    assert expired_response.json()["backoffs"] == []


def test_endpoint_rejects_invalid_now_query_parameter(
    app_with_repo: tuple[FastAPI, AccountBackoffRepository],
) -> None:
    app, _repo = app_with_repo
    with TestClient(app) as client:
        response = client.get("/api/backoffs?now=not-a-timestamp")
    assert response.status_code == 400
    assert response.json() == {"error": "now must be a POSIX epoch timestamp"}


def test_endpoint_rejects_non_finite_now_query_parameter(
    app_with_repo: tuple[FastAPI, AccountBackoffRepository],
) -> None:
    app, _repo = app_with_repo
    with TestClient(app) as client:
        response = client.get("/api/backoffs?now=inf")
    assert response.status_code == 400
    assert response.json() == {"error": "now must be a finite POSIX epoch timestamp"}


def test_endpoint_includes_iso_backoff_until(
    app_with_repo: tuple[FastAPI, AccountBackoffRepository],
) -> None:
    app, repo = app_with_repo
    _populate(repo)
    with TestClient(app) as client:
        response = client.get("/api/backoffs")
    rows = response.json()["backoffs"]
    rate_limited = next(
        r
        for r in rows
        if r["reason"] == "rate_limited" and r["account_name"] == "acct-a"
    )
    assert isinstance(rate_limited["backoff_until"], str)
    assert rate_limited["backoff_until"].endswith("+00:00")
    model_unavail = next(r for r in rows if r["reason"] == "model_unavailable")
    assert model_unavail["backoff_until"] is None


def test_endpoint_reports_account_name_lookup_failure(
    app_with_repo: tuple[FastAPI, AccountBackoffRepository],
) -> None:
    """Backoff rows without account-name enrichment should not look healthy."""
    app, repo = app_with_repo
    _populate(repo)

    class _FailingDb:
        async def fetch_all(self, _sql: str, _params: tuple[int, ...]) -> list[Any]:
            raise RuntimeError("database unavailable")

    app.state.db = _FailingDb()
    with TestClient(app) as client:
        response = client.get("/api/backoffs")

    assert response.status_code == 500
    assert response.json() == {"error": "failed to read backoff account names"}


def test_endpoint_returns_503_when_repo_missing() -> None:
    app = FastAPI()
    app.state.db = None
    register_backoff_routes(app)
    with TestClient(app) as client:
        response = client.get("/api/backoffs")
    assert response.status_code == 503
