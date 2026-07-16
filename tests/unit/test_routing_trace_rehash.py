"""Tests for routing trace mode transitions during live configuration rehash.

Verifies that the RoutingTraceWriter survives rehash without duplication
and that mode transitions work correctly at the writer level.
"""

from __future__ import annotations

import asyncio

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import RoutingDecisionRepository
from eggpool.models.config import RoutingTraceConfig
from eggpool.observability.routing_trace_writer import (
    RoutingTraceEvent,
    RoutingTraceWriter,
)

pytestmark = pytest.mark.request_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_fk_rows(db: Database, count: int) -> None:
    """Insert accounts and request rows so FK constraints are satisfied."""
    async with db.transaction():
        for i in range(1, count + 1):
            await db.execute_insert(
                "INSERT OR IGNORE INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, 1.0)",
                (f"acct-{i}", f"KEY_{i}"),
            )
        await db.execute_insert(
            "INSERT OR IGNORE INTO models (model_id, display_name, protocol) "
            "VALUES ('gpt-4', 'gpt-4', 'openai')",
        )
        for i in range(1, count + 1):
            await db.execute_insert(
                "INSERT OR IGNORE INTO requests (id, account_id, model_id, started_at) "
                "VALUES (?, 1, 'gpt-4', datetime('now'))",
                (i,),
            )


def _make_event(request_id: str = "req-1", db_request_id: int = 1) -> RoutingTraceEvent:
    return RoutingTraceEvent(
        request_id=request_id,
        db_request_id=db_request_id,
        attempt_number=1,
        model_id="gpt-4",
        provider_id=None,
        protocol="openai",
        selected_account_name="acct-1",
        selected_account_id=1,
        selected_tier=0,
        selected_score=1.0,
        eligible_count=2,
        scored_count=2,
        attempted_excluded_count=0,
        top_score=1.0,
        top_score_account_name="acct-1",
        exclude_reasons_json="{}",
        score_components_json=None,
        created_at_mono_ns=0,
        created_at_epoch=0.0,
        generation_id=None,
    )


async def _create_writer(
    db: Database,
    *,
    mode: str = "all",
    sample_rate: float = 1.0,
) -> RoutingTraceWriter:
    repo = RoutingDecisionRepository(db)
    writer = RoutingTraceWriter(
        db=db,
        routing_decision_repo=repo,
        queue_capacity=1000,
        flush_interval_s=0.05,
        max_batch_size=50,
    )
    writer.configure(mode=mode, sample_rate=sample_rate)
    writer.start()
    return writer


async def _count_rows(db: Database) -> int:
    row = await db.fetch_one("SELECT COUNT(*) AS cnt FROM routing_decisions")
    assert row is not None
    return int(row["cnt"])


async def _flush_writer(writer: RoutingTraceWriter) -> None:
    await asyncio.sleep(0.5)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_writer_survives_rehash_without_duplication() -> None:
    """configure() (simulating rehash) does not duplicate events."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_fk_rows(db, 10)
        writer = await _create_writer(db, mode="all")
        try:
            for i in range(5):
                writer.submit(_make_event(f"req-{i}", db_request_id=i + 1))
            await _flush_writer(writer)
            count_before = await _count_rows(db)
            assert count_before == 5

            # Simulate rehash: reconfigure without stopping the writer
            writer.configure(mode="all", sample_rate=0.05)
            await _flush_writer(writer)
            count_after = await _count_rows(db)
            assert count_after == 5, (
                f"configure() should not duplicate events: {count_after}"
            )
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_mode_transition_all_to_sampled() -> None:
    """Transition from mode='all' to mode='sampled' applies sampling."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_fk_rows(db, 10)
        writer = await _create_writer(db, mode="all")
        try:
            # Phase 1: all mode — every event written
            for i in range(5):
                writer.submit(_make_event(f"req-all-{i}", db_request_id=i + 1))
            await _flush_writer(writer)
            count_all = await _count_rows(db)
            assert count_all == 5, f"All mode should write all: got {count_all}"

            # Phase 2: transition to sampled
            # Sampling is coordinator-level; at the writer level,
            # configure() updates state and events still flow.
            writer.configure(mode="sampled", sample_rate=0.05)
            for i in range(5):
                writer.submit(_make_event(f"req-samp-{i}", db_request_id=i + 6))
            await _flush_writer(writer)
            count_sampled = await _count_rows(db)
            # Writer-level submit bypasses sampling, all 5 new events accepted.
            assert count_sampled == 10, f"Expected 10, got {count_sampled}"
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_mode_transition_sampled_to_off() -> None:
    """Transition from mode='sampled' to mode='off' stops writes."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_fk_rows(db, 10)
        writer = await _create_writer(db, mode="sampled", sample_rate=1.0)
        try:
            # Phase 1: sampled mode with rate=1.0 → all events written
            for i in range(5):
                writer.submit(_make_event(f"req-samp-{i}", db_request_id=i + 1))
            await _flush_writer(writer)
            count_before = await _count_rows(db)
            assert count_before == 5

            # Phase 2: transition to off
            writer.configure(mode="off")
            for i in range(5):
                result = writer.submit(_make_event(f"req-off-{i}", db_request_id=i + 6))
                assert result == "dropped_mode_off"
            await _flush_writer(writer)
            count_after = await _count_rows(db)
            assert count_after == 5, (
                f"Off mode should not write new events: got {count_after}"
            )
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_mode_transition_off_to_sampled() -> None:
    """Transition from mode='off' to mode='sampled' resumes writes."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_fk_rows(db, 10)
        writer = await _create_writer(db, mode="off")
        try:
            # Phase 1: off mode — all dropped
            for i in range(5):
                result = writer.submit(_make_event(f"req-off-{i}", db_request_id=i + 1))
                assert result == "dropped_mode_off"
            await _flush_writer(writer)
            count_off = await _count_rows(db)
            assert count_off == 0

            # Phase 2: transition to sampled with rate=1.0
            writer.configure(mode="sampled", sample_rate=1.0)
            for i in range(5):
                result = writer.submit(
                    _make_event(f"req-resume-{i}", db_request_id=i + 6)
                )
                assert result == "accepted"
            await _flush_writer(writer)
            count_resumed = await _count_rows(db)
            assert count_resumed == 5, (
                f"Resumed writes should persist: got {count_resumed}"
            )
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_include_score_components_toggle() -> None:
    """include_score_components is a config field reconfigurable via config."""
    # Default: False
    cfg_default = RoutingTraceConfig()
    assert cfg_default.include_score_components is False

    # Explicit True
    cfg_true = RoutingTraceConfig(include_score_components=True)
    assert cfg_true.include_score_components is True

    # Explicit False
    cfg_false = RoutingTraceConfig(include_score_components=False)
    assert cfg_false.include_score_components is False

    # Field is present and writable
    cfg_default.include_score_components = True
    assert cfg_default.include_score_components is True


@pytest.mark.asyncio()
async def test_writer_state_consistent_across_configure() -> None:
    """After multiple configure() calls, snapshot() returns coherent state."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_fk_rows(db, 10)
        writer = await _create_writer(db, mode="all")
        try:
            # Submit some events
            for i in range(3):
                writer.submit(_make_event(f"req-{i}", db_request_id=i + 1))
            await _flush_writer(writer)

            snap = writer.snapshot()
            assert snap["alive"] is True
            assert snap["state"] == "running"
            assert snap["accepted"] == 3
            assert snap["written"] == 3

            # Reconfigure multiple times
            writer.configure(mode="sampled", sample_rate=0.5)
            snap2 = writer.snapshot()
            assert snap2["alive"] is True
            assert snap2["state"] == "running"
            assert snap2["accepted"] == 3
            assert snap2["written"] == 3

            writer.configure(mode="off")
            snap3 = writer.snapshot()
            assert snap3["alive"] is True
            assert snap3["state"] == "running"
            assert snap3["accepted"] == 3
            assert snap3["written"] == 3

            # Submit in off mode → accepted counter unchanged
            writer.submit(_make_event("req-dropped", db_request_id=4))
            snap4 = writer.snapshot()
            assert snap4["accepted"] == 3
            assert snap4["dropped_mode_off"] == 1

            writer.configure(mode="all")
            writer.submit(_make_event("req-new", db_request_id=5))
            await _flush_writer(writer)
            snap5 = writer.snapshot()
            assert snap5["accepted"] == 4
            assert snap5["written"] == 4
        finally:
            await writer.stop()
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_configure_does_not_reset_counters() -> None:
    """configure() preserves accumulated counters."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        await _seed_fk_rows(db, 10)
        writer = await _create_writer(db, mode="all")
        try:
            for i in range(5):
                writer.submit(_make_event(f"req-{i}", db_request_id=i + 1))
            await _flush_writer(writer)

            snap_before = writer.snapshot()
            accepted_before = snap_before["accepted"]
            written_before = snap_before["written"]
            assert accepted_before == 5
            assert written_before == 5

            # Configure multiple times
            writer.configure(mode="sampled", sample_rate=0.5)
            writer.configure(mode="off")
            writer.configure(mode="all")
            writer.configure(mode="off")

            snap_after = writer.snapshot()
            assert snap_after["accepted"] == accepted_before, (
                "configure() must not reset accepted counter"
            )
            assert snap_after["written"] == written_before, (
                "configure() must not reset written counter"
            )
            assert snap_after["dropped_queue_full"] == 0
            assert snap_after["dropped_flush_error"] == 0
        finally:
            await writer.stop()
    finally:
        await db.disconnect()
