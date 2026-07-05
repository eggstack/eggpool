"""Tests for Phase 7 dashboard / runtime compression aggregates.

Phase 7 surfaces the Phase 4 / Phase 5 / Phase 6 data via three new
query functions:

- ``fetch_compression_runtime`` — mode counts, applied / fallback
  counts, latency stats, per-transform aggregates, warnings rollup,
  cache-safety counters.
- ``fetch_compression_policy_stats`` — one entry per resolved policy
  (``<global>`` sentinel for the no-override path).
- ``fetch_cache_stability_summary`` — transcoded request count plus
  a static note that Phase 3 cache stability is per-request and
  in-memory on ``TranscodeContext.cache_boundary_tracker``.

These tests verify:

- Empty DB returns the stable zero shape (no exceptions).
- Phase 4 observe rows feed observe-mode runtime counts.
- Phase 5 applied rows feed applied counts and per-transform
  breakdown.
- Phase 5 fail-closed fallback rows surface in
  ``failed_fallback_count`` and ``stable_prefix_mismatch``.
- Per-transform savings are split proportionally across applied
  transforms.
- Warnings rollup aggregates across JSON entries.
- Phase 6 policy rollup groups by ``compression_policy_name``.
- Cache-stability summary reports the transcoded count.
- Latency stats (avg / p50 / p95 / max) are computed correctly.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.stats import queries

pytestmark = pytest.mark.dashboard

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest_asyncio.fixture()
async def db(tmp_path: pytest.TempPathFactory) -> AsyncGenerator[Database, None]:
    database = Database(path=str(tmp_path / "stats_phase7_test.sqlite3"))
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    yield database
    await database.disconnect()


# ---------------------------------------------------------------------------
# fetch_compression_runtime
# ---------------------------------------------------------------------------


class TestCompressionRuntimeEmpty:
    """Empty DB returns the stable zero shape."""

    @pytest.mark.asyncio()
    async def test_empty_db_has_all_keys(self, db: Database) -> None:
        """All top-level keys are present with safe defaults."""
        result = await queries.fetch_compression_runtime(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["window"]["request_count"] == 0
        assert result["window"]["seconds"] > 0
        assert result["mode_counts"] == {
            "disabled": 0,
            "observe": 0,
            "safe": 0,
        }
        assert result["applied_count"] == 0
        assert result["failed_fallback_count"] == 0
        assert result["candidate_count"] == 0
        assert result["estimated_savings_tokens"] == 0
        assert result["actual_savings_tokens"] == 0
        assert result["latency_ms"] == {
            "avg": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
        assert result["transforms"] == {}
        assert result["warnings"] == {}
        assert result["cache_safety"] == {
            "stable_prefix_preserved": 0,
            "stable_prefix_mismatch": 0,
        }


class TestCompressionRuntimeModeCounts:
    """Mode counts segregate disabled / observe / safe correctly."""

    @pytest.mark.asyncio()
    async def test_mode_counts_partition_by_status(self, db: Database) -> None:
        """Three rows: disabled, observe-mode, safe-mode."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_modes", "ENV_MODES", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_modes", "openai"),
            )
            # Disabled row
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_status, compression_mode
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    'disabled', 'observe'
                )""",
                ("acct_modes", "model_modes"),
            )
            # Observe-mode row
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_status, compression_mode
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-45 minutes'),
                    datetime('now', '-45 minutes'),
                    'completed',
                    'observed', 'observe'
                )""",
                ("acct_modes", "model_modes"),
            )
            # Safe-mode row
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_status, compression_mode
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'),
                    'completed',
                    'observed', 'safe'
                )""",
                ("acct_modes", "model_modes"),
            )

        result = await queries.fetch_compression_runtime(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["mode_counts"]["disabled"] == 1
        assert result["mode_counts"]["observe"] == 1
        assert result["mode_counts"]["safe"] == 1


class TestCompressionRuntimeAppliedAndFallback:
    """Phase 5 applied / fallback counters surface correctly."""

    @pytest.mark.asyncio()
    async def test_applied_count_and_savings(self, db: Database) -> None:
        """Applied rows contribute to applied_count and actual_savings_tokens."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_apply", "ENV_APPLY", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_apply", "openai"),
            )
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_applied, compression_savings_tokens
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    1, 1234
                )""",
                ("acct_apply", "model_apply"),
            )

        result = await queries.fetch_compression_runtime(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["applied_count"] == 1
        assert result["actual_savings_tokens"] == 1234

    @pytest.mark.asyncio()
    async def test_failed_fallback_and_prefix_mismatch(self, db: Database) -> None:
        """Fail-closed rows surface in both failed_fallback_count and mismatch."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_fb", "ENV_FB", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_fb", "openai"),
            )
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_failed_fallback,
                    compression_stable_prefix_preserved
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    1, 0
                )""",
                ("acct_fb", "model_fb"),
            )

        result = await queries.fetch_compression_runtime(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["failed_fallback_count"] == 1
        assert result["cache_safety"]["stable_prefix_preserved"] == 0
        assert result["cache_safety"]["stable_prefix_mismatch"] == 1


class TestCompressionRuntimeLatency:
    """Latency stats (avg / p50 / p95 / max) computed correctly."""

    @pytest.mark.asyncio()
    async def test_latency_distribution(self, db: Database) -> None:
        """5 rows with latencies 1..5 produce avg=3, p50=3, p95=5, max=5."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_lat", "ENV_LAT", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_lat", "openai"),
            )
            for latency in (1.0, 2.0, 3.0, 4.0, 5.0):
                await db.execute_write(
                    """INSERT INTO requests (
                        account_id, model_id, provider_id, upstream_protocol,
                        started_at, completed_at, status,
                        compression_latency_ms
                    ) VALUES (
                        (SELECT id FROM accounts WHERE name = ?),
                        (SELECT model_id FROM models WHERE model_id = ?),
                        'test_provider', 'openai',
                        datetime('now', '-1 hour'),
                        datetime('now', '-1 hour'),
                        'completed',
                        ?
                    )""",
                    ("acct_lat", "model_lat", latency),
                )

        result = await queries.fetch_compression_runtime(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        latency = result["latency_ms"]
        assert latency["avg"] == pytest.approx(3.0)
        assert latency["p50"] == pytest.approx(3.0)
        assert latency["p95"] == pytest.approx(5.0)
        assert latency["max"] == pytest.approx(5.0)


class TestCompressionRuntimeTransforms:
    """Per-transform aggregates surface from compression_transforms_by_reason_json."""

    @pytest.mark.asyncio()
    async def test_transform_counts_aggregated(self, db: Database) -> None:
        """Two applied rows contribute to transform counts."""
        reason_json = json.dumps({"repeated_line_run": 2, "log_compaction": 1})
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_tx", "ENV_TX", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_tx", "openai"),
            )
            for _ in range(2):
                await db.execute_write(
                    """INSERT INTO requests (
                        account_id, model_id, provider_id, upstream_protocol,
                        started_at, completed_at, status,
                        compression_applied,
                        compression_transforms_by_reason_json,
                        compression_savings_tokens
                    ) VALUES (
                        (SELECT id FROM accounts WHERE name = ?),
                        (SELECT model_id FROM models WHERE model_id = ?),
                        'test_provider', 'openai',
                        datetime('now', '-1 hour'),
                        datetime('now', '-1 hour'),
                        'completed',
                        1, ?, 300
                    )""",
                    ("acct_tx", "model_tx", reason_json),
                )

        result = await queries.fetch_compression_runtime(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["transforms"]["repeated_line_run"]["applied"] == 4
        assert result["transforms"]["log_compaction"]["applied"] == 2

    @pytest.mark.asyncio()
    async def test_transform_savings_proportional(self, db: Database) -> None:
        """Per-transform savings split proportionally across applied transforms."""
        reason_json = json.dumps({"repeated_line_run": 3, "log_compaction": 1})
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_sav", "ENV_SAV", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_sav", "openai"),
            )
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_applied,
                    compression_transforms_by_reason_json,
                    compression_savings_tokens
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    1, ?, 800
                )""",
                ("acct_sav", "model_sav", reason_json),
            )

        result = await queries.fetch_compression_runtime(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        # 800 tokens / 4 total = 200 per unit; 3 units repeated_line_run = 600
        assert result["transforms"]["repeated_line_run"]["tokens_saved"] == 600
        assert result["transforms"]["log_compaction"]["tokens_saved"] == 200


class TestCompressionRuntimeWarnings:
    """Warnings rollup aggregates compression_warnings_json entries."""

    @pytest.mark.asyncio()
    async def test_warnings_aggregated(self, db: Database) -> None:
        """Two rows with overlapping warnings roll up counts."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_warn", "ENV_WARN", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_warn", "openai"),
            )
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_warnings_json
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    ?
                )""",
                (
                    "acct_warn",
                    "model_warn",
                    json.dumps(["stable_prefix_hash_mismatch"]),
                ),
            )
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_warnings_json
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-45 minutes'),
                    datetime('now', '-45 minutes'),
                    'completed',
                    ?
                )""",
                (
                    "acct_warn",
                    "model_warn",
                    json.dumps(
                        [
                            "stable_prefix_hash_mismatch",
                            "transform_skipped_disabled",
                        ]
                    ),
                ),
            )

        result = await queries.fetch_compression_runtime(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["warnings"]["stable_prefix_hash_mismatch"] == 2
        assert result["warnings"]["transform_skipped_disabled"] == 1


# ---------------------------------------------------------------------------
# fetch_compression_policy_stats
# ---------------------------------------------------------------------------


class TestCompressionPolicyStatsEmpty:
    """Empty DB returns the stable zero shape."""

    @pytest.mark.asyncio()
    async def test_empty_db_has_all_keys(self, db: Database) -> None:
        """All top-level keys are present with safe defaults."""
        result = await queries.fetch_compression_policy_stats(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["policy_counts"] == []
        assert result["total_requests"] == 0
        assert result["total_policies"] == 0


class TestCompressionPolicyStatsGrouping:
    """Policy rollup groups rows by compression_policy_name."""

    @pytest.mark.asyncio()
    async def test_global_and_override_grouped(self, db: Database) -> None:
        """Global and override rows produce two policy entries."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_pol", "ENV_POL", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_pol", "openai"),
            )
            # Global row
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_mode, compression_status
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-1 hour'),
                    datetime('now', '-1 hour'),
                    'completed',
                    'observe', 'disabled'
                )""",
                ("acct_pol", "model_pol"),
            )
            # Override row
            await db.execute_write(
                """INSERT INTO requests (
                    account_id, model_id, provider_id, upstream_protocol,
                    started_at, completed_at, status,
                    compression_mode, compression_status,
                    compression_policy_name, compression_policy_source,
                    compression_applied, compression_failed_fallback,
                    compression_warning_count, compression_candidate_count
                ) VALUES (
                    (SELECT id FROM accounts WHERE name = ?),
                    (SELECT model_id FROM models WHERE model_id = ?),
                    'test_provider', 'openai',
                    datetime('now', '-30 minutes'),
                    datetime('now', '-30 minutes'),
                    'completed',
                    'safe', 'observed',
                    'opencode_safe', 'policy:opencode_safe',
                    1, 1, 3, 5
                )""",
                ("acct_pol", "model_pol"),
            )

        result = await queries.fetch_compression_policy_stats(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["total_requests"] == 2
        assert result["total_policies"] == 2
        by_name = {p["policy_name"]: p for p in result["policy_counts"]}
        # Global first in the order
        assert by_name["<global>"]["requests"] == 1
        assert by_name["<global>"]["policy_source"] == "global"
        assert by_name["<global>"]["mode_counts"]["disabled"] == 1
        assert by_name["opencode_safe"]["requests"] == 1
        assert by_name["opencode_safe"]["policy_source"] == "policy:opencode_safe"
        assert by_name["opencode_safe"]["applied"] == 1
        assert by_name["opencode_safe"]["failed_fallback"] == 1
        assert by_name["opencode_safe"]["warning_count"] == 3
        assert by_name["opencode_safe"]["candidate_count"] == 5
        assert by_name["opencode_safe"]["mode_counts"]["safe"] == 1


# ---------------------------------------------------------------------------
# fetch_cache_stability_summary
# ---------------------------------------------------------------------------


class TestCacheStabilitySummary:
    """Cache stability summary counts transcoded rows."""

    @pytest.mark.asyncio()
    async def test_empty_db_returns_zero(self, db: Database) -> None:
        """Empty DB returns transcoded_request_count=0 with notes."""
        result = await queries.fetch_cache_stability_summary(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["transcoded_request_count"] == 0
        assert "TranscodeContext" in result["notes"]

    @pytest.mark.asyncio()
    async def test_transcoded_count(self, db: Database) -> None:
        """Rows with transcoded=1 contribute to transcoded_request_count."""
        async with db.transaction():
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled) VALUES (?, ?, ?)",
                ("acct_cs", "ENV_CS", 1),
            )
            await db.execute_write(
                "INSERT INTO models (model_id, protocol) VALUES (?, ?)",
                ("model_cs", "openai"),
            )
            for i in range(3):
                await db.execute_write(
                    """INSERT INTO requests (
                        account_id, model_id, provider_id, upstream_protocol,
                        started_at, completed_at, status,
                        transcoded
                    ) VALUES (
                        (SELECT id FROM accounts WHERE name = ?),
                        (SELECT model_id FROM models WHERE model_id = ?),
                        'test_provider', 'openai',
                        datetime('now', ?),
                        datetime('now', ?),
                        'completed',
                        1
                    )""",
                    (
                        "acct_cs",
                        "model_cs",
                        f"-{i + 1} hour",
                        f"-{i + 1} hour",
                    ),
                )

        result = await queries.fetch_cache_stability_summary(
            db, "2000-01-01 00:00:00", "2099-12-31 23:59:59"
        )
        assert result["transcoded_request_count"] == 3
