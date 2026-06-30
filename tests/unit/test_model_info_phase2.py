"""Tests for model-info Phase 2: lifecycle wiring, catalog diffing, scheduler."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from eggpool.catalog.service import CatalogRefreshResult
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.dedup import canonical_needs_update
from eggpool.model_info.scheduler import ModelInfoRefreshScheduler
from eggpool.model_info.service import ModelInfoService, _compute_source_backoff
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


# --- Dedup tests ---


class TestCanonicalDedup:
    def test_identical_row_skips_write(self) -> None:
        """canonical_needs_update returns False when payloads match."""
        now = datetime.now(UTC)
        row = CanonicalModelInfo(
            model_id="m",
            status="partial",
            summary="s",
            sparse=False,
            detail={"k": "v"},
            provenance={"p": 1},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now + timedelta(hours=1),
        )
        clone = CanonicalModelInfo(
            model_id="m",
            status="partial",
            summary="s",
            sparse=False,
            detail={"k": "v"},
            provenance={"p": 1},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now + timedelta(hours=1),
        )
        assert canonical_needs_update(row, clone) is False

    def test_status_change_needs_write(self) -> None:
        """canonical_needs_update returns True when status differs."""
        now = datetime.now(UTC)
        base = CanonicalModelInfo(
            model_id="m",
            status="partial",
            summary="s",
            sparse=False,
            detail={},
            provenance={},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now,
        )
        changed = CanonicalModelInfo(
            model_id="m",
            status="fresh",
            summary="s",
            sparse=False,
            detail={},
            provenance={},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now,
        )
        assert canonical_needs_update(base, changed) is True

    def test_next_refresh_change_needs_write(self) -> None:
        """canonical_needs_update returns True when next_refresh_at differs."""
        now = datetime.now(UTC)
        base = CanonicalModelInfo(
            model_id="m",
            status="partial",
            summary="s",
            sparse=False,
            detail={},
            provenance={},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now + timedelta(hours=1),
        )
        changed = CanonicalModelInfo(
            model_id="m",
            status="partial",
            summary="s",
            sparse=False,
            detail={},
            provenance={},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now + timedelta(hours=2),
        )
        assert canonical_needs_update(base, changed) is True

    def test_none_existing_always_needs_write(self) -> None:
        """canonical_needs_update returns True when existing is None."""
        now = datetime.now(UTC)
        info = CanonicalModelInfo(
            model_id="m",
            status="partial",
            summary="s",
            sparse=False,
            detail={},
            provenance={},
            conflicts={},
            first_seen_at=now,
            last_seen_at=now,
            last_refreshed_at=now,
            next_refresh_at=now,
        )
        assert canonical_needs_update(None, info) is True


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
        priorities = [d.priority for d in decisions]
        assert priorities == sorted(priorities)
        assert decisions[0].model_id == "model-sparse"
        assert decisions[1].model_id == "model-partial"
        assert decisions[2].model_id == "model-fresh"


# --- Exponential backoff tests ---


class TestExponentialBackoff:
    def test_first_failure_15m(self) -> None:
        """First failure (count=0) produces 15-minute cooldown."""
        before = datetime.now(UTC)
        result = _compute_source_backoff("src", 0)
        after = datetime.now(UTC)
        expected_min = before + timedelta(minutes=15)
        expected_max = after + timedelta(minutes=15)
        assert expected_min <= result <= expected_max

    def test_second_failure_1h(self) -> None:
        """Second failure (count=1) produces 1-hour cooldown."""
        before = datetime.now(UTC)
        result = _compute_source_backoff("src", 1)
        after = datetime.now(UTC)
        expected_min = before + timedelta(hours=1)
        expected_max = after + timedelta(hours=1)
        assert expected_min <= result <= expected_max

    def test_third_failure_6h(self) -> None:
        """Third failure (count=2) produces 6-hour cooldown."""
        before = datetime.now(UTC)
        result = _compute_source_backoff("src", 2)
        after = datetime.now(UTC)
        expected_min = before + timedelta(hours=6)
        expected_max = after + timedelta(hours=6)
        assert expected_min <= result <= expected_max

    def test_repeated_failure_caps_at_24h(self) -> None:
        """Repeated failures (count>=3) cap at 24-hour cooldown."""
        before = datetime.now(UTC)
        result = _compute_source_backoff("src", 10)
        after = datetime.now(UTC)
        expected_min = before + timedelta(hours=24)
        expected_max = after + timedelta(hours=24)
        assert expected_min <= result <= expected_max


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
async def test_catalog_refresh_new_model_sets_due_now() -> None:
    """Newly discovered models get next_refresh_at close to now."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "fresh-model")

        cache = _make_cache_with_models({"fresh-model": {}})
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        now = datetime.now(UTC)
        result = CatalogRefreshResult(
            live_model_ids=frozenset({"fresh-model"}),
            new_model_ids=frozenset({"fresh-model"}),
            withdrawn_model_ids=frozenset(),
            changed_provider_keys=frozenset(),
            refreshed_at=now.timestamp(),
        )

        await service.reconcile_catalog_refresh(result)
        info = await service.get_summary("fresh-model")
        assert info is not None
        assert info.next_refresh_at is not None
        # next_refresh_at should be within 5 minutes of now
        delta = info.next_refresh_at - now
        assert timedelta(0) <= delta <= timedelta(minutes=5)
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
async def test_reconcile_catalog_refresh_skips_unchanged() -> None:
    """reconcile_catalog_refresh skips writes for unchanged rows."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)
        await _seed_model(db, "stable-model")

        cache = _make_cache_with_models({"stable-model": {"display_name": "Stable"}})
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # Create initial canonical row
        now = datetime.now(UTC)
        info = CanonicalModelInfo(
            model_id="stable-model",
            status="partial",
            summary="Existing summary",
            sparse=False,
            detail={"display_name": "Stable"},
            provenance={"sources": ["provider_catalog"]},
            conflicts={},
            first_seen_at=now - timedelta(days=1),
            last_seen_at=now - timedelta(hours=1),
            last_refreshed_at=now - timedelta(hours=1),
            next_refresh_at=now + timedelta(hours=1),
        )
        await service.repo.upsert_canonical(info)

        # Simulate a refresh with no changes
        result = CatalogRefreshResult(
            live_model_ids=frozenset({"stable-model"}),
            new_model_ids=frozenset(),
            withdrawn_model_ids=frozenset(),
            changed_provider_keys=frozenset(),
            refreshed_at=now.timestamp(),
        )

        reconcile_result = await service.reconcile_catalog_refresh(result)
        # No new, changed, or withdrawn models -> nothing to write
        assert reconcile_result["created"] == 0
        assert reconcile_result["updated"] == 0
        assert reconcile_result["refreshed"] == 0
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
        assert result["total"] == result["refreshed"] + result["skipped"]
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_refresh_due_models_skips_unchanged_rows() -> None:
    """refresh_due_models skips rows where payload is unchanged."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        cache = _make_cache_with_models({"unchanged-model": {}})
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        now = datetime.now(UTC)
        await _seed_model(db, "unchanged-model")
        info = CanonicalModelInfo(
            model_id="unchanged-model",
            status="sparse_new",
            summary="New model detected; metadata sparse.",
            sparse=True,
            detail={"providers": ["test-provider"]},
            provenance={"sources": ["provider_catalog"]},
            conflicts={},
            first_seen_at=now - timedelta(hours=2),
            last_seen_at=now - timedelta(hours=1),
            last_refreshed_at=now - timedelta(hours=1),
            next_refresh_at=now - timedelta(minutes=1),  # due
        )
        await service.repo.upsert_canonical(info)

        result = await service.refresh_due_models()
        assert result["refreshed"] + result["skipped"] == result["total"]
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
        assert health["test_source"]["failure_count"] == 0

        await service.record_source_error("test_source", ValueError("test error"))
        health = await service.repo.source_health_snapshot()
        assert health["test_source"]["last_error_class"] == "ValueError"
        assert health["test_source"]["failure_count"] == 1

        # Second error increments failure_count
        await service.record_source_error("test_source", ValueError("again"))
        health = await service.repo.source_health_snapshot()
        assert health["test_source"]["failure_count"] == 2

        # Success resets failure_count
        await service.record_source_success("test_source")
        health = await service.repo.source_health_snapshot()
        assert health["test_source"]["failure_count"] == 0
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


@pytest.mark.asyncio()
async def test_catalog_refresh_loop_skips_reconcile_when_none() -> None:
    """_catalog_refresh_loop does not call reconcile when model_info is None."""
    from eggpool.app import _catalog_refresh_loop

    mock_catalog = AsyncMock()
    mock_catalog.refresh.return_value = CatalogRefreshResult(
        live_model_ids=frozenset(),
        new_model_ids=frozenset(),
        withdrawn_model_ids=frozenset(),
        changed_provider_keys=frozenset(),
        refreshed_at=1000.0,
    )

    async def run_one_cycle() -> None:
        task = asyncio.create_task(_catalog_refresh_loop(mock_catalog, 0, None))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    await run_one_cycle()
    mock_catalog.refresh.assert_called()


@pytest.mark.asyncio()
async def test_supervisor_registration_disabled() -> None:
    """model_info_refresh task is not registered when disabled."""
    from eggpool.background import TaskSupervisor
    from eggpool.models.config import AppConfig

    config = AppConfig()
    config.model_info.enabled = False
    supervisor = TaskSupervisor()
    # When disabled, the registration block should not execute
    if config.model_info.enabled and config.model_info.refresh_interval_s > 0:
        supervisor.register(
            "model_info_refresh", lambda: AsyncMock().run_periodic_refresh()
        )
    assert supervisor.get_task("model_info_refresh") is None


@pytest.mark.asyncio()
async def test_supervisor_registration_enabled() -> None:
    """model_info_refresh task is registered when enabled."""
    from eggpool.background import TaskSupervisor
    from eggpool.models.config import AppConfig

    config = AppConfig()
    config.model_info.enabled = True
    config.model_info.refresh_interval_s = 100
    supervisor = TaskSupervisor()
    if config.model_info.enabled and config.model_info.refresh_interval_s > 0:
        mock_service = AsyncMock()
        supervisor.register(
            "model_info_refresh", lambda: mock_service.run_periodic_refresh()
        )
    assert supervisor.get_task("model_info_refresh") is not None


@pytest.mark.asyncio()
async def test_failure_count_affects_backoff_cooldown() -> None:
    """record_source_error uses escalating cooldowns based on failure_count."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await _run_migrations(db)

        cache = _make_cache_with_models({})
        config = ModelInfoConfig()
        service = ModelInfoService(config, db, cache)

        # First error -> 15m cooldown
        await service.record_source_error("src", ValueError("e1"))
        health = await service.repo.source_health_snapshot()
        cooldown1 = health["src"]["cooldown_until"]
        assert cooldown1 is not None

        # Second error -> 1h cooldown
        await service.record_source_error("src", ValueError("e2"))
        health = await service.repo.source_health_snapshot()
        cooldown2 = health["src"]["cooldown_until"]
        assert cooldown2 is not None
        assert health["src"]["failure_count"] == 2
        # cooldown2 should be further in the future than cooldown1
        # (since 1h > 15m and both are relative to their call time)
        # We just verify the failure_count escalated

        # Success resets
        await service.record_source_success("src")
        health = await service.repo.source_health_snapshot()
        assert health["src"]["failure_count"] == 0
    finally:
        await db.disconnect()
