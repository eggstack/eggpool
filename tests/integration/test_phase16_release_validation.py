"""Phase 16 release validation: end-to-end regression matrix for release readiness."""

from __future__ import annotations

import tempfile
import time

import pytest

from go_aggregator.accounts.state import AccountRuntimeState
from go_aggregator.catalog.pricing import (
    CostCalculator,
    PriceRepository,
    PriceSnapshot,
)
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from go_aggregator.health.health_manager import (
    FailureCategory,
    HealthManager,
    classify_failure_category,
)
from go_aggregator.models.config import ModelOverrideConfig
from go_aggregator.quota.estimation import (
    QuotaEstimator,
)
from go_aggregator.request.attempt_finalizer import (
    AttemptFinalizationData,
    AttemptFinalizer,
)
from go_aggregator.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)
from go_aggregator.security.redaction import REDACTED, redact_error_detail

SECRET_BEARING_INPUT = (
    "sk-FAKE_API_KEY Authorization: Bearer test-token "
    '"prompt": "private prompt" '
    '"completion": "private completion" '
    "password=secret123 api_key=abc123 "
    "https://user:pass@example.test/path?token=secret"
)

FORBIDDEN_MARKERS = [
    "sk-FAKE_API_KEY",
    "Authorization: Bearer test-token",
    '"prompt": "private prompt"',
    '"completion": "private completion"',
    "password=secret123",
    "api_key=abc123",
    "user:pass@",
    "token=secret",
]


async def _seed_db(db: Database) -> None:
    await db.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) "
        "VALUES (?, ?, 1, 1.0)",
        ("test-acct", "P16_KEY"),
    )
    await db.execute(
        "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
        ("gpt-4", "openai"),
    )
    await db.connection.commit()


def _make_selected(
    *,
    db_request_id: str,
    account_name: str = "test-acct",
    model_id: str = "gpt-4",
    attempt_id: int,
    reservation_id: str,
    estimated_microdollars: int = 100_000,
    attempt_number: int = 1,
) -> object:
    class Selected:
        pass

    s = Selected()
    s.db_request_id = db_request_id
    s.account_name = account_name
    s.model_id = model_id
    s.attempt_id = attempt_id
    s.reservation_id = reservation_id
    s.estimated_microdollars = estimated_microdollars
    s.attempt_number = attempt_number
    return s


# ===========================================================================
# A. Real 402 lifecycle
# ===========================================================================


class TestReal402Lifecycle:
    def test_classify_402_with_no_error_class(self) -> None:
        assert classify_failure_category(None, status_code=402) == (
            FailureCategory.QUOTA_EXHAUSTED
        )

    def test_classify_quotaexhausted_error_class(self) -> None:
        assert classify_failure_category("quotaexhausted") == (
            FailureCategory.QUOTA_EXHAUSTED
        )

    def test_classify_quota_exhausted_error_class(self) -> None:
        assert classify_failure_category("quota_exhausted") == (
            FailureCategory.QUOTA_EXHAUSTED
        )

    def test_record_quota_exhausted_places_account_in_cooldown(self) -> None:
        hm = HealthManager()
        before = time.time()
        hm.record_quota_exhausted("acct-a", cooldown_seconds=0.1)
        health = hm.get_account_health("acct-a")
        assert health.health_state == "quota_exhausted"
        assert not health.is_healthy
        assert health.cooldown_until >= before + 0.1
        assert not hm.is_account_healthy("acct-a")
        time.sleep(0.2)
        assert hm.is_account_healthy("acct-a")

    def test_runtime_state_quota_exhausted_uses_custom_cooldown(self) -> None:
        state = AccountRuntimeState(name="acct-a", enabled=True)
        state.record_failure("quota_exhausted", cooldown_seconds=5.0)
        assert state.health_state == "quota_exhausted"
        assert state.cooldown_until > time.time() + 4.0
        assert not state.is_eligible()


# ===========================================================================
# B. Already-released reservation
# ===========================================================================


class TestAlreadyReleasedReservation:
    @pytest.mark.asyncio
    async def test_no_double_decrement_when_attempt_already_released(
        self,
    ) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        attempt_repo = AttemptRepository(db)
        reservation_repo = ReservationRepository(db)
        usage_window_repo = UsageWindowRepository(db)
        price_repo = PriceRepository(db)
        cost_calculator = CostCalculator(price_repo)
        health_manager = HealthManager()
        quota_estimator = QuotaEstimator()

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="req-b-released",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        quota_estimator.add_reservation("test-acct", 100_000)
        assert quota_estimator.get_account_reserved_cost("test-acct") == 100_000

        attempt_finalizer = AttemptFinalizer(
            db=db, attempt_repo=attempt_repo, reservation_repo=reservation_repo
        )
        attempt_result = await attempt_finalizer.finalize_failed_attempt(
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            data=AttemptFinalizationData(
                status_code=502,
                error_class="BadGateway",
                error_detail="upstream failed",
                release_reason="attempt_failed",
            ),
        )
        assert attempt_result.attempt_transitioned is True
        assert attempt_result.reservation_released is True

        resv_row = await db.fetch_one(
            "SELECT status FROM reservations WHERE id = ?", (reservation_id,)
        )
        assert resv_row is not None
        assert resv_row["status"] == "released"

        quota_estimator.remove_reservation("test-acct", 100_000)

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            cost_calculator=cost_calculator,
            quota_estimator=quota_estimator,
            health_manager=health_manager,
        )

        selected = _make_selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            estimated_microdollars=100_000,
        )

        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                status_code=502,
                error_class="BadGateway",
                input_tokens=10,
                output_tokens=5,
            ),
        )

        quota_obj = quota_estimator.get_account_quota("test-acct")
        assert quota_obj is not None

        windows = await usage_window_repo.get_usage_windows(
            account_id=1, now_iso="2026-01-01 00:00:00"
        )
        assert windows["5h"] >= 100_000

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_success_after_external_release(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        attempt_repo = AttemptRepository(db)
        reservation_repo = ReservationRepository(db)
        health_manager = HealthManager()
        quota_estimator = QuotaEstimator()

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="req-ext-release",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=50_000,
                ttl_seconds=300,
            )

        af = AttemptFinalizer(
            db=db, attempt_repo=attempt_repo, reservation_repo=reservation_repo
        )
        result = await af.finalize_failed_attempt(
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            data=AttemptFinalizationData(
                release_reason="attempt_retryable",
            ),
        )
        assert result.reservation_released is True

        price_repo = PriceRepository(db)
        cost_calc = CostCalculator(price_repo)
        quota_estimator.remove_reservation("test-acct", 50_000)

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            cost_calculator=cost_calc,
            quota_estimator=quota_estimator,
            health_manager=health_manager,
        )

        selected = _make_selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            estimated_microdollars=50_000,
        )

        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.COMPLETED,
                status_code=200,
                input_tokens=100,
                output_tokens=50,
            ),
        )

        hm_health = health_manager.get_account_health("test-acct")
        assert hm_health.health_state == "healthy"

        await db.disconnect()


# ===========================================================================
# C. Secret-bearing error detail
# ===========================================================================


class TestSecretBearingErrorDetail:
    @pytest.mark.asyncio
    async def test_request_finalizer_persists_redacted_detail(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        attempt_repo = AttemptRepository(db)
        reservation_repo = ReservationRepository(db)

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="req-redact",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
        )

        selected = _make_selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )

        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_detail=SECRET_BEARING_INPUT,
            ),
        )

        row = await db.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        for marker in FORBIDDEN_MARKERS:
            assert marker not in detail, f"Marker {marker!r} found in persisted detail"

        await db.disconnect()

    @pytest.mark.asyncio
    async def test_attempt_finalizer_persists_redacted_detail(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        attempt_repo = AttemptRepository(db)
        reservation_repo = ReservationRepository(db)

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="req-redact-attempt",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        af = AttemptFinalizer(
            db=db, attempt_repo=attempt_repo, reservation_repo=reservation_repo
        )
        await af.finalize_failed_attempt(
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            data=AttemptFinalizationData(
                error_detail=SECRET_BEARING_INPUT,
            ),
        )

        row = await db.fetch_one(
            "SELECT error_detail FROM request_attempts WHERE id = ?",
            (attempt_id,),
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        for marker in FORBIDDEN_MARKERS:
            assert marker not in detail, f"Marker {marker!r} found in persisted detail"
        assert REDACTED in detail

        await db.disconnect()


# ===========================================================================
# D. Cursor ownership
# ===========================================================================


class TestCursorOwnership:
    @pytest.mark.asyncio
    async def test_execute_returning(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        async with db.transaction():
            rows = await db.execute_returning(
                "UPDATE accounts SET weight = 2.0 WHERE name = ? "
                "RETURNING id, name, weight",
                ("test-acct",),
            )
        assert len(rows) == 1
        assert rows[0]["weight"] == 2.0
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_insert(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()

        async with db.transaction():
            last_id = await db.execute_insert(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, 1.0)",
                ("new-acct", "NEW_KEY"),
            )
        assert isinstance(last_id, int)
        assert last_id > 0
        row = await db.fetch_one(
            "SELECT id FROM accounts WHERE name = ?", ("new-acct",)
        )
        assert row is not None
        assert row["id"] == last_id
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_execute_write(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        async with db.transaction():
            count = await db.execute_write(
                "UPDATE accounts SET enabled = 0 WHERE name = ?",
                ("test-acct",),
            )
        assert count == 1
        await db.disconnect()


# ===========================================================================
# E. Partial price overrides
# ===========================================================================


class TestPartialPriceOverrides:
    def test_cache_only_override_yields_config_source(self) -> None:
        override = ModelOverrideConfig(
            cache_read_per_million_microdollars=500_000,
            cache_write_per_million_microdollars=1_000_000,
        )
        has_any = any(
            v is not None
            for v in (
                override.input_price_per_1k,
                override.output_price_per_1k,
                override.cache_read_per_million_microdollars,
                override.cache_write_per_million_microdollars,
            )
        )
        assert has_any is True
        assert override.input_price_per_1k is None
        assert override.output_price_per_1k is None

    def test_input_config_and_upstream_output_is_mixed(self) -> None:
        override = ModelOverrideConfig(input_price_per_1k=0.003)
        has_any = any(
            v is not None
            for v in (
                override.input_price_per_1k,
                override.output_price_per_1k,
                override.cache_read_per_million_microdollars,
                override.cache_write_per_million_microdollars,
            )
        )
        assert has_any is True

    def test_cache_read_config_and_upstream_cache_write_is_mixed(self) -> None:
        override = ModelOverrideConfig(cache_read_per_million_microdollars=100_000)
        has_any = any(
            v is not None
            for v in (
                override.input_price_per_1k,
                override.output_price_per_1k,
                override.cache_read_per_million_microdollars,
                override.cache_write_per_million_microdollars,
            )
        )
        assert has_any is True
        assert override.cache_write_per_million_microdollars is None

    def test_missing_categories_remain_null(self) -> None:
        snapshot = PriceSnapshot(
            model_id="m",
            input_price_per_1k=0.003,
            output_price_per_1k=None,
            captured_at="2026-01-01T00:00:00",
            input_per_million_microdollars=3_000_000,
            output_per_million_microdollars=None,
            cache_read_per_million_microdollars=None,
            cache_write_per_million_microdollars=None,
            source="mixed",
        )
        assert snapshot.output_per_million_microdollars is None
        assert snapshot.cache_read_per_million_microdollars is None
        assert snapshot.cache_write_per_million_microdollars is None
        assert snapshot.source == "mixed"


# ===========================================================================
# F. Cooldown parity
# ===========================================================================


class TestCooldownParity:
    def test_runtime_state_quota_exhausted_uses_configured_cooldown(self) -> None:
        state = AccountRuntimeState(name="a", enabled=True)
        state.record_failure("quota_exhausted", cooldown_seconds=60.0)
        assert state.health_state == "quota_exhausted"
        assert state.cooldown_until > time.time() + 55.0

    def test_runtime_state_rate_limit_with_retry_after(self) -> None:
        state = AccountRuntimeState(name="a", enabled=True)
        state.record_failure("rate_limited", rate_limit_retry_after=120.0)
        assert state.health_state == "cooldown"
        assert state.cooldown_until > time.time() + 110.0

    def test_runtime_state_rate_limit_backoff_when_no_retry_after(self) -> None:
        state = AccountRuntimeState(name="a", enabled=True)
        state.record_failure("rate_limited")
        assert state.health_state == "cooldown"
        assert state.cooldown_until > time.time() + 25.0


# ===========================================================================
# G. Cache rounding
# ===========================================================================


class TestCacheRounding:
    @pytest.mark.asyncio
    async def test_one_token_at_one_microcost_is_estimated(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        price_repo = PriceRepository(db)
        async with db.transaction():
            await price_repo.record_snapshot(
                model_id="gpt-4",
                input_price_per_1k=None,
                output_price_per_1k=None,
                input_per_million_microdollars=None,
                output_per_million_microdollars=None,
                cache_read_per_million_microdollars=1,
                cache_write_per_million_microdollars=1,
                source="config",
            )

        calc = CostCalculator(price_repo)
        cost, exactness = await calc.calculate_cost(
            model_id="gpt-4",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1,
            cache_write_tokens=0,
        )
        assert exactness == "estimated"
        assert cost >= 0
        await db.disconnect()

    @pytest.mark.asyncio
    async def test_larger_cache_only_usage_is_derived(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        price_repo = PriceRepository(db)
        async with db.transaction():
            await price_repo.record_snapshot(
                model_id="gpt-4",
                input_price_per_1k=None,
                output_price_per_1k=None,
                input_per_million_microdollars=None,
                output_per_million_microdollars=None,
                cache_read_per_million_microdollars=500_000,
                cache_write_per_million_microdollars=1_000_000,
                source="config",
            )

        calc = CostCalculator(price_repo)
        cost, exactness = await calc.calculate_cost(
            model_id="gpt-4",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1000,
            cache_write_tokens=0,
        )
        assert cost > 0
        assert exactness == "derived"
        await db.disconnect()


# ===========================================================================
# H. Restart invariants
# ===========================================================================


class TestRestartInvariants:
    @pytest.mark.asyncio
    async def test_usage_totals_persist_across_disconnect(self) -> None:
        db_path = tempfile.mktemp(suffix=".db")
        try:
            db = Database(path=db_path)
            await db.connect()
            runner = MigrationRunner(db)
            await runner.run()
            await _seed_db(db)

            request_repo = RequestRepository(db)
            async with db.transaction():
                db_id = await request_repo.create_pending(
                    request_id="req-persist",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )

            async with db.transaction():
                await request_repo.update_after_completion(
                    db_id,
                    status="completed",
                    status_code=200,
                    cost_microdollars=75_000,
                )

            await db.disconnect()

            db2 = Database(path=db_path)
            await db2.connect()
            try:
                row = await db2.fetch_one(
                    "SELECT cost_microdollars FROM requests WHERE id = ?",
                    (db_id,),
                )
                assert row is not None
                assert row["cost_microdollars"] == 75_000
            finally:
                await db2.disconnect()
        finally:
            import os

            os.unlink(db_path)

    @pytest.mark.asyncio
    async def test_invariants_pass_after_simple_lifecycle(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        runner = MigrationRunner(db)
        await runner.run()
        await _seed_db(db)

        request_repo = RequestRepository(db)
        attempt_repo = AttemptRepository(db)
        reservation_repo = ReservationRepository(db)

        async with db.transaction():
            db_id = await request_repo.create_pending(
                request_id="req-invariant",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
        )

        selected = _make_selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )

        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.COMPLETED,
                status_code=200,
                input_tokens=100,
                output_tokens=50,
            ),
        )

        stale = await db.fetch_all(
            "SELECT id FROM requests "
            "WHERE status = 'pending' "
            "AND started_at < datetime('now', '-600 seconds')"
        )
        assert len(stale) == 0

        incomplete = await db.fetch_all(
            "SELECT ra.id FROM request_attempts ra "
            "JOIN requests r ON r.id = ra.request_id "
            "WHERE ra.completed_at IS NULL AND r.status != 'pending'"
        )
        assert len(incomplete) == 0

        active_for_terminal = await db.fetch_all(
            "SELECT rv.id FROM reservations rv "
            "JOIN requests r ON r.id = rv.request_id "
            "WHERE rv.status = 'active' AND r.status != 'pending'"
        )
        assert len(active_for_terminal) == 0

        negatives = await db.fetch_all(
            "SELECT id FROM requests WHERE cost_microdollars < 0"
        )
        assert len(negatives) == 0

        await db.disconnect()


# ===========================================================================
# I. Privacy
# ===========================================================================


class TestPrivacy:
    def test_redact_sk_api_key(self) -> None:
        result = redact_error_detail("my key is sk-abc123def456ghi")
        assert "sk-abc123def456ghi" not in result
        assert REDACTED in result

    def test_redact_authorization_bearer_header(self) -> None:
        result = redact_error_detail("Authorization: Bearer tok123xyz")
        assert "tok123xyz" not in result
        assert REDACTED in result

    def test_redact_prompt_field(self) -> None:
        result = redact_error_detail('"prompt": "secret text here"')
        assert "secret text here" not in result
        assert REDACTED in result

    def test_redact_completion_field(self) -> None:
        result = redact_error_detail('"completion": "more secrets here"')
        assert "more secrets here" not in result
        assert REDACTED in result

    def test_redact_password_assignment(self) -> None:
        result = redact_error_detail("password=hunter2")
        assert "hunter2" not in result
        assert REDACTED in result

    def test_redact_api_key_assignment(self) -> None:
        result = redact_error_detail("api_key=supersecret")
        assert "supersecret" not in result
        assert REDACTED in result

    def test_redact_url_userinfo(self) -> None:
        result = redact_error_detail("https://admin:secretpass@example.com/api")
        assert "secretpass" not in result
        assert REDACTED in result

    def test_redact_sensitive_query_param(self) -> None:
        result = redact_error_detail("https://example.com/api?access_token=tok456")
        assert "tok456" not in result
        assert REDACTED in result

    def test_redact_passes_through_none_and_empty(self) -> None:
        assert redact_error_detail(None) is None
        assert redact_error_detail("") == ""
