"""Integration test: catalog refresh → ping recording → stats query flow.

Verifies that pings recorded during catalog refresh are visible through
the stats service and dashboard rendering pipeline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.catalog.service import CatalogService
from go_aggregator.dashboard.render import render_overview, render_pings
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import PingRepository
from go_aggregator.stats.service import StatsService, TimeRange

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from datetime import UTC, datetime


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "ping_e2e.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def seeded_db(db: Database) -> Database:
    """Seed accounts for catalog refresh."""
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
            ("acct1", "ENV1", 1),
        )
    return db


def _make_config() -> MagicMock:
    config = MagicMock()
    config.providers = {}
    config.model_overrides = {}
    config.models = MagicMock()
    config.models.expose_mode = "union"
    return config


def _make_registry() -> MagicMock:
    registry = MagicMock()
    state = MagicMock()
    state.name = "acct1"
    state.enabled = True
    registry.get_enabled_states.return_value = [state]
    registry.get_api_key.return_value = "test-key"
    registry.get_provider_for_account.return_value = "opencode-go"
    return registry


class TestPingEndToEnd:
    """Full flow: catalog refresh → ping data → stats → dashboard."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_ping_recorded_after_refresh(self, seeded_db: Database) -> None:
        """Catalog refresh records ping data in the repository."""
        respx.get("https://api.opencode-go.com/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "gpt-4"}, {"id": "claude-3"}]},
            )
        )

        mock_client = httpx.AsyncClient(
            base_url="https://api.opencode-go.com",
            headers={"Authorization": "Bearer test-key"},
        )

        ping_repo = PingRepository(seeded_db)
        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=seeded_db,
            client_pool=mock_client,
            ping_repo=ping_repo,
        )

        await service.refresh()

        # Verify ping was recorded
        recent = await ping_repo.get_ping_recent(limit=10)
        assert len(recent) >= 1
        ping = recent[0]
        assert ping["provider_id"] == "opencode-go"
        assert ping["account_name"] == "acct1"
        assert ping["latency_ms"] >= 0
        assert ping["status_code"] == 200
        assert ping["error"] is None
        assert ping["model_count"] == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_ping_visible_in_stats_service(self, seeded_db: Database) -> None:
        """Pings recorded during refresh appear in StatsService queries."""
        respx.get("https://api.opencode-go.com/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "gpt-4"}]},
            )
        )

        mock_client = httpx.AsyncClient(
            base_url="https://api.opencode-go.com",
            headers={"Authorization": "Bearer test-key"},
        )

        ping_repo = PingRepository(seeded_db)
        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=seeded_db,
            client_pool=mock_client,
            ping_repo=ping_repo,
        )

        await service.refresh()

        # Query via StatsService
        stats = StatsService(seeded_db, ping_repo=ping_repo)
        time_range = TimeRange(
            start=datetime(2000, 1, 1, tzinfo=UTC),
            end=datetime(2100, 1, 1, tzinfo=UTC),
            label="custom",
        )

        ping_summary = await stats.get_ping_summary(time_range)
        assert len(ping_summary) >= 1
        assert ping_summary[0]["provider_id"] == "opencode-go"

        recent = await stats.get_ping_recent(limit=10)
        assert len(recent) >= 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_ping_appears_in_dashboard(self, seeded_db: Database) -> None:
        """Pings are rendered in the dashboard HTML."""
        respx.get("https://api.opencode-go.com/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "gpt-4"}]},
            )
        )

        mock_client = httpx.AsyncClient(
            base_url="https://api.opencode-go.com",
            headers={"Authorization": "Bearer test-key"},
        )

        ping_repo = PingRepository(seeded_db)
        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=seeded_db,
            client_pool=mock_client,
            ping_repo=ping_repo,
        )

        await service.refresh()

        # Get ping data through stats
        stats = StatsService(seeded_db, ping_repo=ping_repo)
        time_range = TimeRange(
            start=datetime(2000, 1, 1, tzinfo=UTC),
            end=datetime(2100, 1, 1, tzinfo=UTC),
            label="custom",
        )
        ping_summary = await stats.get_ping_summary(time_range)
        recent = await stats.get_ping_recent(limit=10)

        # Render ping page
        html = render_pings(ping_summary, recent, period="24h")
        assert "opencode-go" in html
        assert "Provider Pings" in html

    @respx.mock
    @pytest.mark.asyncio
    async def test_ping_appears_in_overview(self, seeded_db: Database) -> None:
        """Pings appear in the overview page provider health section."""
        respx.get("https://api.opencode-go.com/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "gpt-4"}]},
            )
        )

        mock_client = httpx.AsyncClient(
            base_url="https://api.opencode-go.com",
            headers={"Authorization": "Bearer test-key"},
        )

        ping_repo = PingRepository(seeded_db)
        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=seeded_db,
            client_pool=mock_client,
            ping_repo=ping_repo,
        )

        await service.refresh()

        stats = StatsService(seeded_db, ping_repo=ping_repo)
        time_range = TimeRange(
            start=datetime(2000, 1, 1, tzinfo=UTC),
            end=datetime(2100, 1, 1, tzinfo=UTC),
            label="custom",
        )
        ping_summary = await stats.get_ping_summary(time_range)

        html = render_overview(
            overview={
                "summary": {
                    "total_requests": 0,
                    "successful_requests": 0,
                    "error_requests": 0,
                    "error_rate": 0.0,
                    "total_input_tokens": 0,
                    "total_output_tokens": 0,
                    "total_cost_microdollars": 0,
                    "avg_latency_ms": 0.0,
                },
                "imbalance": {
                    "imbalance_ratio": 0.0,
                    "active_accounts": 0,
                    "most_used": None,
                    "least_used": None,
                },
                "period_label": "24h",
                "start": "2000-01-01 00:00:00",
                "end": "2100-01-01 00:00:00",
            },
            accounts=[],
            ping_summary=ping_summary,
        )
        assert "Provider health" in html
        assert "opencode-go" in html

    @respx.mock
    @pytest.mark.asyncio
    async def test_ping_recorded_on_failure(self, seeded_db: Database) -> None:
        """Ping is recorded even when upstream returns an error."""
        respx.get("https://api.opencode-go.com/models").mock(
            return_value=httpx.Response(403, text="Forbidden")
        )

        mock_client = httpx.AsyncClient(
            base_url="https://api.opencode-go.com",
            headers={"Authorization": "Bearer test-key"},
        )

        ping_repo = PingRepository(seeded_db)
        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=seeded_db,
            client_pool=mock_client,
            ping_repo=ping_repo,
        )

        await service.refresh()

        recent = await ping_repo.get_ping_recent(limit=10)
        assert len(recent) >= 1
        assert recent[0]["status_code"] == 403
        assert recent[0]["error"] == "HTTP 403"
        assert recent[0]["model_count"] == 0
