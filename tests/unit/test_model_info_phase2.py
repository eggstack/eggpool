"""Tests for model-info Phase 2: lifecycle wiring, catalog diffing, scheduler."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from eggpool.catalog.service import CatalogRefreshResult
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.scheduler import ModelInfoRefreshScheduler
from eggpool.model_info.service import ModelInfoService
from eggpool.model_info.types import CanonicalModelInfo
from eggpool.models.config import ModelInfoConfig

if TYPE_CHECKING:
    from eggpool.catalog.cache import ModelCatalogCache


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(
    db: Database, model_id: str = "gpt-4o", display_name: str = "GPT-4o"
) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, display_name),
        )


def _make_cache_with_models(
    models: dict[str, dict], provider_id: str = "test-provider"
) -> ModelCatalogCache:  # noqa: F821
    """Create a ModelCatalogCache pre-populated with test models."""
    from eggpool.catalog.cache import ModelCatalogCache

    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    for model_id, info in models.items():
        entry = {
            "model_id": model_id,
            "display_name": info.get("display_name", model_id),
            "protocol": info.get("protocol", "openai"),
            "capabilities": info.get("capabilities", {}),
            "source_metadata": {},
            "first_seen_at": info.get("first_seen_at", now_ts),
            "last_seen_at": info.get("last_seen_at", now_ts),
            "discovered_limits": {},
            "effective_limits": info.get("effective_limits", {}),
        }
        cache._models[model_id] = entry
        cache._provider_models[(model_id, provider_id)] = dict(entry)
    return cache


# --- CatalogRefreshResult tests ---


class TestCatalogRefreshResult:
    def test_catalog_refresh_result_fields(self) -> None:
        """CatalogRefreshResult stores all diff fields."""
        result = CatalogRefreshResult(
            live_model_ids=frozenset({"a", "b"}),
            new_model_ids=frozenset({"b"}),
            withdrawn_model_ids=frozenset({"c"}),
            changed_provider_keys=frozenset({("b", "prov1")}),
            refreshed_at=1000.0,
            pruned_count=2,
        )
        assert result.live_model_ids == frozenset({"a", "b"})
        assert result.new_model_ids == frozenset({"b"})
        assert result.withdrawn_model_ids == frozenset({"c"})
        assert result.changed_provider_keys == frozenset({("b", "prov1")})
        assert result.refreshed_at == 1000.0
        assert result.pruned_count == 2

    def test_catalog_refresh_result_frozen(self) -> None:
        """CatalogRefreshResult is immutable."""
        result = CatalogRefreshResult(
            live_model_ids=frozenset(),
            new_model_ids=frozenset(),
            withdrawn_model_ids=frozenset(),
            changed_provider_keys=frozenset(),
            refreshed_at=0.0,
        )
        with pytest.raises(AttributeError):
            result.refreshed_at = 2000.0  # type: ignore[misc]


# --- Scheduler tests ---


class TestModelInfoRefreshScheduler:
    def setup_method(self) -> None:
        self.config = ModelInfoConfig()
        self.scheduler = ModelInfoRefreshScheduler(self.config)

    def test_sparse_new_initial_refresh_interval(self) -> None:
        """sparse_new model younger than initial TTL gets initial TTL."""
        now = datetime.now(UTC)
        first_seen = now - timedelta(minutes=10)  # very young
        next_refresh = self.scheduler.next_refresh_for(
            status="sparse_new",
            first_seen_at=first_seen,
            last_refreshed_at=None,
            now=now,
        )
        expected = now + timedelta(seconds=self.config.sparse_new_initial_ttl_s)
        assert next_refresh == expected

    def test_sparse_new_later_refresh_interval(self) -> None:
        """sparse_new model past initial TTL but within accelerated window."""
        now = datetime.now(UTC)
        first_seen = now - timedelta(hours=2)  # past initial TTL (1h)
        next_refresh = self.scheduler.next_refresh_for(
            status="sparse_new",
            first_seen_at=first_seen,
            last_refreshed_at=None,
            now=now,
        )
        expected = now + timedelta(seconds=self.config.sparse_new_later_ttl_s)
        assert next_refresh == expected

    def test_sparse_new_exits_accelerated_window(self) -> None:
        """sparse_new model beyond accelerated window uses partial TTL."""
        now = datetime.now(UTC)
        first_seen = now - timedelta(days=10)  # past accelerated window (7d)
        next_refresh = self.scheduler.next_refresh_for(
            status="sparse_new",
            first_seen_at=first_seen,
            last_refreshed_at=None,
            now=now,
        )
        expected = now + timedelta(seconds=self.config.partial_ttl_s)
        assert next_refresh == expected

    def test_conflicting_uses_conflict_ttl(self) -> None:
        """conflicting status uses conflict_ttl_s."""
        now = datetime.now(UTC)
        next_refresh = self.scheduler.next_refresh_for(
            status="conflicting",
            first_seen_at=now - timedelta(days=1),
            last_refreshed_at=now - timedelta(hours=1),
            now=now,
        )
        expected = now + timedelta(seconds=self.config.conflict_ttl_s)
        assert next_refresh == expected

    def test_fresh_uses_known_ttl(self) -> None:
        """fresh status uses known_ttl_s."""
        now = datetime.now(UTC)
        next_refresh = self.scheduler.next_refresh_for(
            status="fresh",
            first_seen_at=now - timedelta(days=30),
            last_refreshed_at=now - timedelta(hours=1),
            now=now,
        )
        expected = now + timedelta(seconds=self.config.known_ttl_s)
        assert next_refresh == expected

    def test_partial_uses_partial_ttl(self) -> None:
        """partial status uses partial_ttl_s."""
        now = datetime.now(UTC)
        next_refresh = self.scheduler.next_refresh_for(
            status="partial",
            first_seen_at=now - timedelta(days=5),
            last_refreshed_at=now - timedelta(hours=1),
            now=now,
        )
        expected = now + timedelta(seconds=self.config.partial_ttl_s)
        assert next_refresh == expected

    def test_source_unavailable_respects_cooldown(self) -> None:
        """source_unavailable respects source cooldown."""
        now = datetime.now(UTC)
        cooldown = now + timedelta(hours=2)
        next_refresh = self.scheduler.next_refresh_for(
            status="source_unavailable",
            first_seen_at=now - timedelta(days=1),
            last_refreshed_at=now - timedelta(hours=1),
            now=now,
            source_cooldown_until=cooldown,
        )
        assert next_refresh == cooldown

    def test_source_unavailable_falls_back_to_partial_ttl(self) -> None:
        """source_unavailable without cooldown uses partial TTL."""
        now = datetime.now(UTC)
        next_refresh = self.scheduler.next_refresh_for(
            status="source_unavailable",
            first_seen_at=now - timedelta(days=1),
            last_refreshed_at=now - timedelta(hours=1),
            now=now,
        )
        expected = now + timedelta(seconds=self.config.partial_ttl_s)
        assert next_refresh == expected

    def test_scheduler_never_returns_past_time(self) -> None:
        """Scheduler always returns a future time after now."""
        now = datetime.now(UTC)
        for status in (
            "sparse_new",
            "partial",
            "fresh",
            "conflicting",
            "stale",
            "unmatched",
            "source_unavailable",
            "manual_override",
            "withdrawn",
        ):
            next_refresh = self.scheduler.next_refresh_for(
                status=status,  # type: ignore[arg-type]
                first_seen_at=now - timedelta(days=30),
                last_refreshed_at=now - timedelta(hours=1),
                now=now,
            )
            assert next_refresh > now, f"Status {status} returned past time"

    def test_rank_due_work_sorts_by_priority(self) -> None:
        """rank_due_work sorts models by priority (lower = more urgent)."""
        now = datetime.now(UTC)
        candidates = [
            (
                "model-fresh",
                "fresh",
                now - timedelta(days=30),
                now - timedelta(hours=1),
            ),
            ("model-sparse", "sparse_new", now - timedelta(hours=1), None),
            (
                "model-partial",
                "partial",
                now - timedelta(days=5),
                now - timedelta(hours=1),
            ),
        ]
        decisions = self.scheduler.rank_due_work(candidates, now)
        assert len(decisions) == 3
        # next_refresh_for always returns now + ttl, so none are due;
        # verify priority ordering: sparse_new (1) < partial (2) < fresh (4)
        priorities = [d.priority for d in decisions]
        assert priorities == sorted(priorities)
        assert decisions[0].model_id == "model-sparse"
        assert decisions[1].model_id == "model-partial"
        assert decisions[2].model_id == "model-fresh"


# --- Service lifecycle tests ---


@pytest.mark.asyncio()
async def test_reconcile_catalog_refresh_creates_new_models() -> None:
    """reconcile_catalog_refresh creates rows for newly discovered models."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "new-model")

        cache = _make_cache_with_models({"new-model": {"display_name": "New"}})
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        result = CatalogRefreshResult(
            live_model_ids=frozenset({"new-model"}),
            new_model_ids=frozenset({"new-model"}),
            withdrawn_model_ids=frozenset(),
            changed_provider_keys=frozenset(),
            refreshed_at=datetime.now(UTC).timestamp(),
        )

        reconcile_result = await service.reconcile_catalog_refresh(result)
        assert reconcile_result["created"] == 1

        info = await service.get_summary("new-model")
        assert info is not None
        assert info.next_refresh_at is not None
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_reconcile_catalog_refresh_marks_withdrawn() -> None:
    """reconcile_catalog_refresh marks withdrawn models."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "old-model")
        await _seed_model(db, "live-model")

        cache = _make_cache_with_models({"live-model": {"display_name": "Live"}})
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # First, create canonical rows for both models
        now = datetime.now(UTC)
        for mid in ("old-model", "live-model"):
            info = CanonicalModelInfo(
                model_id=mid,
                status="partial",
                summary="test",
                sparse=False,
                detail={},
                provenance={},
                conflicts={},
                first_seen_at=now,
                last_seen_at=now,
                last_refreshed_at=now,
                next_refresh_at=now + timedelta(hours=1),
            )
            await service.repo.upsert_canonical(info)

        # Now simulate a refresh where old-model is withdrawn
        result = CatalogRefreshResult(
            live_model_ids=frozenset({"live-model"}),
            new_model_ids=frozenset(),
            withdrawn_model_ids=frozenset({"old-model"}),
            changed_provider_keys=frozenset(),
            refreshed_at=now.timestamp(),
        )

        reconcile_result = await service.reconcile_catalog_refresh(result)
        assert reconcile_result["updated"] == 1

        old_info = await service.get_summary("old-model")
        assert old_info is not None
        assert old_info.status == "withdrawn"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_refresh_due_models_respects_max_per_cycle() -> None:
    """refresh_due_models processes at most max_models_per_cycle models."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        cache = _make_cache_with_models(
            {
                "m1": {},
                "m2": {},
                "m3": {},
                "m4": {},
                "m5": {},
            }
        )
        config = ModelInfoConfig(max_models_per_cycle=3)
        service = ModelInfoService(config, db, cache)

        # Seed models and make them all due
        now = datetime.now(UTC)
        for i in range(1, 6):
            await _seed_model(db, f"m{i}", f"Model {i}")
            info = CanonicalModelInfo(
                model_id=f"m{i}",
                status="partial",
                summary=f"Model {i}",
                sparse=False,
                detail={},
                provenance={},
                conflicts={},
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now,
                last_refreshed_at=now,
                next_refresh_at=now - timedelta(hours=1),  # all due
            )
            await service.repo.upsert_canonical(info)

        result = await service.refresh_due_models()
        assert result["refreshed"] <= 3
        assert result["total"] == result["refreshed"]
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_model_info_failure_does_not_raise() -> None:
    """Service methods handle errors gracefully."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        cache = _make_cache_with_models({"model-a": {}})
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # reconcile with empty result should work
        result = CatalogRefreshResult(
            live_model_ids=frozenset(),
            new_model_ids=frozenset(),
            withdrawn_model_ids=frozenset(),
            changed_provider_keys=frozenset(),
            refreshed_at=datetime.now(UTC).timestamp(),
        )
        reconcile_result = await service.reconcile_catalog_refresh(result)
        assert reconcile_result["created"] == 0
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_source_health_helpers() -> None:
    """record_source_success and record_source_error work."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        cache = _make_cache_with_models({})
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        await service.record_source_success("test_source")
        health = await service.repo.source_health_snapshot()
        assert "test_source" in health
        assert health["test_source"]["last_success_at"] is not None

        await service.record_source_error("test_source", ValueError("test error"))
        health = await service.repo.source_health_snapshot()
        assert health["test_source"]["last_error_class"] == "ValueError"
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_startup_reconcile_creates_rows_for_catalog_models() -> None:
    """Startup reconciliation creates canonical rows for all catalog models."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "model-a", "Model A")
        await _seed_model(db, "model-b", "Model B")

        cache = _make_cache_with_models(
            {
                "model-a": {"display_name": "Model A", "protocol": "openai"},
                "model-b": {"display_name": "Model B", "protocol": "openai"},
            }
        )
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        result = await service.reconcile_catalog_snapshot(reason="startup")
        assert result["created"] == 2
        assert result["total"] == 2

        info_a = await service.get_summary("model-a")
        assert info_a is not None
        info_b = await service.get_summary("model-b")
        assert info_b is not None
    finally:
        await db.disconnect()


# --- App wiring tests ---


@pytest.mark.asyncio()
async def test_catalog_refresh_loop_invokes_model_info_reconcile() -> None:
    """_catalog_refresh_loop calls model_info.reconcile_catalog_refresh."""
    from eggpool.app import _catalog_refresh_loop

    mock_catalog = AsyncMock()
    mock_catalog.refresh.return_value = CatalogRefreshResult(
        live_model_ids=frozenset({"a"}),
        new_model_ids=frozenset({"a"}),
        withdrawn_model_ids=frozenset(),
        changed_provider_keys=frozenset(),
        refreshed_at=1000.0,
    )

    mock_model_info = AsyncMock()

    # Run one iteration then cancel
    import asyncio

    async def run_one_cycle() -> None:
        task = asyncio.create_task(
            _catalog_refresh_loop(mock_catalog, 0, mock_model_info)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    await run_one_cycle()
    mock_catalog.refresh.assert_called()
    mock_model_info.reconcile_catalog_refresh.assert_called()


@pytest.mark.asyncio()
async def test_catalog_refresh_loop_handles_model_info_failure() -> None:
    """_catalog_refresh_loop catches model_info errors."""
    from eggpool.app import _catalog_refresh_loop

    mock_catalog = AsyncMock()
    mock_catalog.refresh.return_value = CatalogRefreshResult(
        live_model_ids=frozenset(),
        new_model_ids=frozenset(),
        withdrawn_model_ids=frozenset(),
        changed_provider_keys=frozenset(),
        refreshed_at=1000.0,
    )

    mock_model_info = AsyncMock()
    mock_model_info.reconcile_catalog_refresh.side_effect = RuntimeError("db error")

    import asyncio

    async def run_one_cycle() -> None:
        task = asyncio.create_task(
            _catalog_refresh_loop(mock_catalog, 0, mock_model_info)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # Should not raise
    await run_one_cycle()
    mock_catalog.refresh.assert_called()
    mock_model_info.reconcile_catalog_refresh.assert_called()
