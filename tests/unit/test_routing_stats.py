"""Tests for routing-decision observability.

Covers Phase 2 of the metrics-core-api plan: routing_decisions table
queries, account-level selection breakdowns, and exclusion reason
parsing from ``exclude_reasons_json``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import RequestRepository, RoutingDecisionRepository
from eggpool.stats import queries
from eggpool.stats.service import StatsService, resolve_time_range

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "routing_stats_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def seeded_routing_db(db: Database) -> Database:
    """Seed an account, a model, and a request with routing decisions."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, provider_id) "
            "VALUES (?, ?, ?, ?)",
            ("acct_r", "ENV_R", 1, "opencode-go"),
        )
        await db.execute_write(
            "INSERT INTO models (model_id, protocol, provider_id) VALUES (?, ?, ?)",
            ("model_r", "openai", "opencode-go"),
        )
        await RequestRepository(db).create_pending(
            request_id="routing-trace-1",
            model_id="model_r",
            protocol="openai",
            streamed=False,
            account_id=1,
        )
    rows = await db.fetch_all("SELECT id FROM requests")
    request_id = int(rows[0]["id"])

    decisions = [
        {
            "attempt_number": 1,
            "selected_account_id": 1,
            "selected_account_name": "acct_r",
            "selected_tier": 10,
            "selected_score": 0.95,
            "eligible_count": 3,
            "scored_count": 3,
            "attempted_excluded_count": 0,
            "top_score": 0.95,
            "top_score_account_name": "acct_r",
            "exclude_reasons_json": json.dumps([]),
        },
        {
            "attempt_number": 2,
            "selected_account_id": 1,
            "selected_account_name": "acct_r",
            "selected_tier": 9,
            "selected_score": 0.40,
            "eligible_count": 2,
            "scored_count": 2,
            "attempted_excluded_count": 1,
            "top_score": 0.40,
            "top_score_account_name": "acct_r",
            "exclude_reasons_json": json.dumps(
                [
                    {"account": "acct_s", "reason": "circuit_breaker"},
                ]
            ),
        },
    ]
    async with db.transaction():
        repo = RoutingDecisionRepository(db)
        for d in decisions:
            await repo.create(
                request_id=request_id,
                attempt_number=d["attempt_number"],
                model_id="model_r",
                provider_id="opencode-go",
                protocol="openai",
                selected_account_id=d["selected_account_id"],
                selected_account_name=d["selected_account_name"],
                selected_tier=d["selected_tier"],
                selected_score=d["selected_score"],
                eligible_count=d["eligible_count"],
                scored_count=d["scored_count"],
                attempted_excluded_count=d["attempted_excluded_count"],
                top_score=d["top_score"],
                top_score_account_name=d["top_score_account_name"],
                exclude_reasons_json=d["exclude_reasons_json"],
            )
    return db


class TestFetchRoutingDistribution:
    """Tests for fetch_routing_distribution."""

    @pytest.mark.asyncio()
    async def test_empty_db_returns_no_rows(self, db: Database) -> None:
        rows = await queries.fetch_routing_distribution(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert rows == []

    @pytest.mark.asyncio()
    async def test_aggregates_by_model(self, seeded_routing_db: Database) -> None:
        rows = await queries.fetch_routing_distribution(
            seeded_routing_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["model_id"] == "model_r"
        assert row["provider_id"] == "opencode-go"
        assert row["decision_count"] == 2
        assert row["distinct_selected_accounts"] == 1
        # (3 + 2) / 2
        assert row["avg_eligible_count"] == pytest.approx(2.5)


class TestFetchRoutingSelectionBreakdown:
    """Tests for fetch_routing_selection_breakdown."""

    @pytest.mark.asyncio()
    async def test_aggregates_by_account(self, seeded_routing_db: Database) -> None:
        rows = await queries.fetch_routing_selection_breakdown(
            seeded_routing_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert len(rows) == 1
        assert rows[0]["account_name"] == "acct_r"
        assert rows[0]["selection_count"] == 2


class TestFetchRoutingExclusionBreakdown:
    """Tests for fetch_routing_exclusion_breakdown."""

    @pytest.mark.asyncio()
    async def test_parses_json_exclusion_array(
        self, seeded_routing_db: Database
    ) -> None:
        rows = await queries.fetch_routing_exclusion_breakdown(
            seeded_routing_db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert len(rows) == 1
        assert rows[0]["account_name"] == "acct_s"
        assert rows[0]["reason"] == "circuit_breaker"
        assert rows[0]["exclusion_count"] == 1

    @pytest.mark.asyncio()
    async def test_no_exclusions_returns_empty(self, db: Database) -> None:
        rows = await queries.fetch_routing_exclusion_breakdown(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert rows == []


class TestFetchRoutingDecisionsForRequest:
    """Tests for fetch_routing_decisions_for_request."""

    @pytest.mark.asyncio()
    async def test_returns_decisions_in_order(
        self, seeded_routing_db: Database
    ) -> None:
        rows = await seeded_routing_db.fetch_all("SELECT id FROM requests")
        request_id = int(rows[0]["id"])
        decisions = await queries.fetch_routing_decisions_for_request(
            seeded_routing_db, request_id
        )
        assert len(decisions) == 2
        assert decisions[0]["attempt_number"] == 1
        assert decisions[1]["attempt_number"] == 2
        # The second attempt excluded acct_s with reason circuit_breaker
        parsed = json.loads(decisions[1]["exclude_reasons_json"])
        assert parsed[0]["account"] == "acct_s"


class TestStatsServiceRoutingMethods:
    """Smoke tests for the high-level StatsService routing methods."""

    @pytest.mark.asyncio()
    async def test_get_routing_distribution(self, seeded_routing_db: Database) -> None:
        service = StatsService(seeded_routing_db)
        time_range = resolve_time_range("24h")
        rows = await service.get_routing_distribution(time_range)
        assert len(rows) == 1

    @pytest.mark.asyncio()
    async def test_get_routing_exclusion_breakdown(
        self, seeded_routing_db: Database
    ) -> None:
        service = StatsService(seeded_routing_db)
        time_range = resolve_time_range("24h")
        rows = await service.get_routing_exclusion_breakdown(time_range)
        assert rows[0]["reason"] == "circuit_breaker"


class TestRequestTraceIncludesRoutingDecisions:
    """Verify the trace endpoint surfaces routing decisions."""

    @pytest.mark.asyncio()
    async def test_trace_query_returns_request_and_attempts(
        self, seeded_routing_db: Database
    ) -> None:
        rows = await seeded_routing_db.fetch_all("SELECT id FROM requests")
        request_id = int(rows[0]["id"])
        trace = await queries.fetch_request_trace(seeded_routing_db, request_id)
        assert trace is not None
        assert trace["request"]["id"] == request_id
        assert trace["attempts"] == []  # no attempt rows seeded

    @pytest.mark.asyncio()
    async def test_service_returns_routing_decisions_for_request(
        self, seeded_routing_db: Database
    ) -> None:
        service = StatsService(seeded_routing_db)
        rows = await seeded_routing_db.fetch_all("SELECT id FROM requests")
        request_id = int(rows[0]["id"])
        decisions = await service.get_routing_decisions_for_request(request_id)
        assert len(decisions) == 2
        assert decisions[0]["attempt_number"] == 1
