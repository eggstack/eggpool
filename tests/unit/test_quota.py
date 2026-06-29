"""Tests for quota estimation and reservations."""

from __future__ import annotations

import time

import pytest

from eggpool.quota.estimation import (
    EWMA_HARD_CAP,
    GLOBAL_EWMA_HARD_CAP,
    MODEL_FAMILY_FALLBACKS,
    AccountQuota,
    ManualOffset,
    QuotaEstimator,
    QuotaWindow,
)
from eggpool.quota.reservation import Reservation, ReservationManager
from eggpool.quota.scorer import QuotaFairScorer, RoutingScore


class TestQuotaWindow:
    """Tests for QuotaWindow."""

    def test_basic_usage_tracking(self) -> None:
        """Test basic usage tracking in a window."""
        window = QuotaWindow(window_seconds=60.0)
        now = time.time()

        window.add_observation(now, 100, 500)
        window.add_observation(now + 10, 200, 1000)

        tokens, cost = window.get_usage(now + 10)
        assert tokens == 300
        assert cost == 1500

    def test_window_expiry(self) -> None:
        """Test that old observations are pruned."""
        window = QuotaWindow(window_seconds=60.0)
        now = time.time()

        window.add_observation(now, 100, 500)
        window.add_observation(now + 70, 200, 1000)  # Outside window

        tokens, cost = window.get_usage(now + 70)
        assert tokens == 200
        assert cost == 1000


class TestAccountQuota:
    """Tests for AccountQuota."""

    def test_record_usage(self) -> None:
        """Test recording usage."""
        quota = AccountQuota(account_name="test-account")
        quota.record_usage(100, 500)

        tokens, cost = quota.daily_window.get_usage()
        assert tokens == 100
        assert cost == 500

    def test_within_limits(self) -> None:
        """Test quota limit checking."""
        quota = AccountQuota(
            account_name="test-account",
            capacity_5h_microdollars=10000,
        )
        quota.record_usage(100, 500)
        assert quota.is_within_limits()

        quota.record_usage(100, 9500)
        assert not quota.is_within_limits()

    def test_remaining_capacity(self) -> None:
        """Test remaining capacity calculation."""
        quota = AccountQuota(
            account_name="test-account",
            capacity_5h_microdollars=10000,
        )
        quota.record_usage(100, 5000)
        capacity = quota.get_remaining_capacity()
        assert capacity == pytest.approx(0.5)

    def test_manual_offset_is_deprecated(self) -> None:
        """Deprecated ``manual_offset`` must not affect reported usage."""
        quota = AccountQuota(account_name="test-account")
        quota.record_usage(100, 500)

        quota.manual_offset = ManualOffset(tokens=50, cost_microdollars=250)
        tokens, cost = quota.get_effective_usage()
        assert tokens == 100
        assert cost == 500


class TestQuotaEstimator:
    """Tests for QuotaEstimator."""

    def test_record_usage(self) -> None:
        """Test recording usage for accounts."""
        estimator = QuotaEstimator()
        estimator.record_usage("account1", 100, 500)
        estimator.record_usage("account1", 200, 1000)

        quota = estimator.get_account_quota("account1")
        assert quota is not None
        tokens, cost = quota.daily_window.get_usage()
        assert tokens == 300
        assert cost == 1500

    def test_account_weights(self) -> None:
        """Test account weight management."""
        estimator = QuotaEstimator()
        estimator.set_account_weight("account1", 2.0)
        estimator.set_account_weight("account2", 0.5)

        assert estimator.get_account_weight("account1") == 2.0
        assert estimator.get_account_weight("account2") == 0.5

    def test_account_limits(self) -> None:
        """Test account limit management."""
        estimator = QuotaEstimator()
        estimator.set_account_limits("account1", capacity_7d_microdollars=10000)

        quota = estimator.get_account_quota("account1")
        assert quota is not None
        assert quota.capacity_7d_microdollars == 10000

    def test_eligible_accounts(self) -> None:
        """Test getting eligible accounts."""
        estimator = QuotaEstimator()
        estimator.set_account_limits("account1", capacity_5h_microdollars=10000)
        estimator.set_account_limits("account2", capacity_5h_microdollars=10000)

        # Account1 is at 50% usage
        estimator.record_usage("account1", 100, 5000)
        # Account2 is at 90% usage
        estimator.record_usage("account2", 100, 9000)

        eligible = estimator.get_eligible_accounts(["account1", "account2"])
        assert len(eligible) == 2
        assert eligible[0][0] == "account1"  # Higher capacity
        assert eligible[0][1] > eligible[1][1]  # Higher remaining capacity

    def test_configure_account_policy(self) -> None:
        """Test configuring full account policy."""
        estimator = QuotaEstimator()
        estimator.configure_account_policy(
            "account1",
            weight=2.0,
            capacity_5h_microdollars=24_000_000,
            capacity_7d_microdollars=60_000_000,
            capacity_30d_microdollars=120_000_000,
            offset_5h_microdollars=1_000_000,
            offset_7d_microdollars=2_000_000,
            offset_30d_microdollars=3_000_000,
        )

        quota = estimator.get_account_quota("account1")
        assert quota is not None
        assert quota.weight == 2.0
        assert quota.capacity_5h_microdollars == 24_000_000
        assert quota.capacity_7d_microdollars == 60_000_000
        assert quota.capacity_30d_microdollars == 120_000_000
        assert quota.five_hour_offset == 1_000_000
        assert quota.weekly_offset == 2_000_000
        assert quota.monthly_offset == 3_000_000

    def test_configure_account_policy_creates_if_missing(self) -> None:
        """Test that configure_account_policy creates account if missing."""
        estimator = QuotaEstimator()
        assert "new_account" not in estimator.accounts

        estimator.configure_account_policy(
            "new_account",
            weight=1.5,
            capacity_5h_microdollars=18_000_000,
            capacity_7d_microdollars=45_000_000,
            capacity_30d_microdollars=90_000_000,
            offset_5h_microdollars=0,
            offset_7d_microdollars=0,
            offset_30d_microdollars=0,
        )

        quota = estimator.get_account_quota("new_account")
        assert quota is not None
        assert quota.weight == 1.5


class TestReservation:
    """Tests for Reservation."""

    def test_creation(self) -> None:
        """Test reservation creation."""
        reservation = Reservation(
            reservation_id="test-id",
            account_name="test-account",
            estimated_tokens=100,
            estimated_cost_microdollars=500,
            request_id="req-123",
            created_at=time.time(),
            expires_at=time.time() + 300,
        )
        assert not reservation.released
        assert not reservation.is_expired()

    def test_release(self) -> None:
        """Test reservation release."""
        reservation = Reservation(
            reservation_id="test-id",
            account_name="test-account",
            estimated_tokens=100,
            estimated_cost_microdollars=500,
            request_id="req-123",
            created_at=time.time(),
            expires_at=time.time() + 300,
        )
        reservation.release("completed")
        assert reservation.released
        assert reservation.release_reason == "completed"

    def test_expiry_boundary_and_epoch_release_timestamp(self) -> None:
        reservation = Reservation(
            reservation_id="test-id",
            account_name="test-account",
            estimated_tokens=100,
            estimated_cost_microdollars=500,
            request_id="req-123",
            created_at=0.0,
            expires_at=10.0,
        )
        assert reservation.is_expired(10.0)
        reservation.release("completed", timestamp=0.0)
        assert reservation.released_at == 0.0


class TestReservationManager:
    """Tests for ReservationManager."""

    def test_create_and_release(self) -> None:
        """Test reservation creation and release."""
        manager = ReservationManager()
        reservation = manager.create_reservation(
            account_name="test-account",
            estimated_tokens=100,
            estimated_cost_microdollars=500,
            request_id="req-123",
        )

        assert not reservation.released
        manager.release_reservation(reservation.reservation_id, "completed")
        assert reservation.released

    def test_account_reservations(self) -> None:
        """Test getting account reservations."""
        manager = ReservationManager()
        manager.create_reservation("account1", 100, 500, "req-1")
        manager.create_reservation("account1", 200, 1000, "req-2")
        manager.create_reservation("account2", 150, 750, "req-3")

        reservations = manager.get_account_reservations("account1")
        assert len(reservations) == 2

    def test_reserved_usage(self) -> None:
        """Test getting reserved usage."""
        manager = ReservationManager()
        manager.create_reservation("account1", 100, 500, "req-1")
        manager.create_reservation("account1", 200, 1000, "req-2")

        tokens, cost = manager.get_account_reserved_usage("account1")
        assert tokens == 300
        assert cost == 1500

    def test_reconcile_expired(self) -> None:
        """Test reconciliation of expired reservations."""
        manager = ReservationManager()
        manager.reservation_ttl_seconds = 0.0  # Immediate expiry

        manager.create_reservation("account1", 100, 500, "req-1")
        time.sleep(0.01)  # Let it expire

        cleaned = manager.reconcile_reservations()
        assert cleaned == 1

    def test_explicit_zero_ttl_is_not_replaced_by_default(self) -> None:
        manager = ReservationManager(reservation_ttl_seconds=300.0)
        reservation = manager.create_reservation(
            "account1", 100, 500, "req-1", ttl_seconds=0.0
        )
        assert reservation.expires_at == reservation.created_at


class TestQuotaFairScorer:
    """Tests for QuotaFairScorer."""

    @pytest.mark.asyncio()
    async def test_score_accounts(self) -> None:
        """Test scoring accounts. Lower score = less utilized = preferred."""
        estimator = QuotaEstimator()
        estimator.set_account_limits("account1", capacity_5h_microdollars=10000)
        estimator.set_account_limits("account2", capacity_5h_microdollars=10000)
        estimator.record_usage("account1", 100, 5000)
        estimator.record_usage("account2", 100, 9000)

        scorer = QuotaFairScorer(quota_estimator=estimator)
        scores = await scorer.score_accounts(["account1", "account2"])

        assert len(scores) == 2
        # account1 used 50%, account2 used 90% — account1 should score lower
        assert scores[0].quota_score < scores[1].quota_score

    def test_select_account(self) -> None:
        """Test account selection. Lower score = less utilized = preferred."""
        scorer = QuotaFairScorer()
        scores = [
            RoutingScore("account1", 1.0, 1.0, True),
            RoutingScore("account2", 0.5, 1.0, True),
        ]

        selected = scorer.select_account(scores)
        assert selected is not None
        assert selected.account_name == "account2"

    def test_select_no_eligible(self) -> None:
        """Test selection when no accounts eligible."""
        scorer = QuotaFairScorer()
        scores = [
            RoutingScore("account1", 1.0, 1.0, False),
            RoutingScore("account2", 0.5, 1.0, False),
        ]

        selected = scorer.select_account(scores)
        assert selected is None

    def test_rank_accounts(self) -> None:
        """Test account ranking. Lower score = less utilized = ranked first."""
        scorer = QuotaFairScorer()
        scores = [
            RoutingScore("account1", 0.5, 1.0, True),
            RoutingScore("account2", 1.0, 1.0, True),
            RoutingScore("account3", 0.75, 1.0, True),
        ]

        ranked = scorer.rank_accounts(scores)
        assert ranked[0].account_name == "account1"
        assert ranked[1].account_name == "account3"
        assert ranked[2].account_name == "account2"

    def test_routing_score_tier_default_zero(self) -> None:
        """``RoutingScore.tier`` defaults to 0 (the standard base tier)."""
        score = RoutingScore("a", 0.5, 1.0, True)
        assert score.tier == 0

    def test_routing_score_tier_set_explicitly(self) -> None:
        """``RoutingScore.tier`` is settable as a constructor kwarg."""
        score = RoutingScore("a", 0.5, 1.0, True, tier=5)
        assert score.tier == 5

    @pytest.mark.asyncio()
    async def test_above_quota_account_remains_scoreable(self) -> None:
        """Above-capacity accounts remain scoreable but worse-ranked.

        In ``score_only`` mode (the default), the scorer never returns
        ``is_eligible=False`` simply because local cost exceeds the
        configured capacity. It still reflects the higher utilization
        through the quota score so high-usage accounts are ranked below
        lower-usage peers.
        """
        from eggpool.quota.estimation import AccountQuota, PersistedWindowSnapshot

        estimator = QuotaEstimator()
        estimator.accounts["acct1"] = AccountQuota(
            account_name="acct1",
            capacity_5h_microdollars=10_000_000,
            persisted_snapshot=PersistedWindowSnapshot(
                account_id=1, cost_5h=50_000_000, cost_7d=0, cost_30d=0
            ),
        )
        estimator.accounts["acct2"] = AccountQuota(
            account_name="acct2",
            capacity_5h_microdollars=10_000_000,
            persisted_snapshot=PersistedWindowSnapshot(
                account_id=2, cost_5h=0, cost_7d=0, cost_30d=0
            ),
        )

        scorer = QuotaFairScorer(quota_estimator=estimator)
        scores = await scorer.score_accounts(["acct1", "acct2"])

        assert all(score.is_eligible for score in scores)
        by_name = {score.account_name: score for score in scores}
        assert by_name["acct1"].quota_score > by_name["acct2"].quota_score


class TestEstimateCostTierPriority:
    """Tests for the 5-tier cost estimation hierarchy in QuotaEstimator."""

    def test_tier3_override_beats_tier4_family_fallback(self) -> None:
        """Configured per-model override takes precedence over family fallback."""
        estimator = QuotaEstimator()
        # Set an override for gpt-4o that differs from the built-in family rate
        estimator.set_model_override("gpt-4o", input_price=1.0, output_price=2.0)

        cost_override = estimator.estimate_cost("acct", "gpt-4o", 1000)

        # Compute expected cost using the override's average rate
        avg_rate = (1.0 + 2.0) / 2.0  # 1.5 dollars/1M tokens
        expected = int(1000 * avg_rate * estimator.default_safety_factor)
        assert cost_override == max(expected, 1)

        # Verify the family fallback would produce a different (higher) cost
        family_rate = MODEL_FAMILY_FALLBACKS["gpt-4o"]
        family_avg = (family_rate[0] + family_rate[1]) / 2.0
        family_cost = int(1000 * family_avg * estimator.default_safety_factor)
        assert cost_override != family_cost

    def test_account_override_beats_global_model_override(self) -> None:
        estimator = QuotaEstimator()
        estimator.set_model_override("gpt-4o", input_price=1.0, output_price=1.0)
        estimator.set_account_model_override(
            "provider-b-account",
            "gpt-4o",
            input_price=9.0,
            output_price=9.0,
        )

        provider_cost = estimator.estimate_cost("provider-b-account", "gpt-4o", 1000)
        global_cost = estimator.estimate_cost("provider-a-account", "gpt-4o", 1000)

        assert provider_cost == int(1000 * 9.0 * estimator.default_safety_factor)
        assert global_cost == int(1000 * 1.0 * estimator.default_safety_factor)

    def test_tier4_family_fallback_used_when_no_override(self) -> None:
        """Family fallback is used when no override is configured."""
        estimator = QuotaEstimator()
        cost = estimator.estimate_cost("acct", "gpt-4o", 1000)

        family_rate = MODEL_FAMILY_FALLBACKS["gpt-4o"]
        avg_rate = (family_rate[0] + family_rate[1]) / 2.0
        expected = int(1000 * avg_rate * estimator.default_safety_factor)
        assert cost == max(expected, 1)

    def test_tier5_global_fallback_when_no_match(self) -> None:
        """Global fallback is used when no override or family match exists."""
        estimator = QuotaEstimator()
        cost = estimator.estimate_cost("acct", "unknown-model-xyz", 1000)

        from eggpool.quota.estimation import (
            GLOBAL_FALLBACK,
            GLOBAL_FALLBACK_FLOOR_MICRODOLLARS_PER_TOKEN,
        )

        cost_per_token = max(
            GLOBAL_FALLBACK[0],
            GLOBAL_FALLBACK_FLOOR_MICRODOLLARS_PER_TOKEN,
        )
        expected = int(1000 * cost_per_token * estimator.default_safety_factor)
        assert cost == max(expected, 1)

    def test_tier1_account_ewma_used_when_sufficient_samples(self) -> None:
        """Account/model EWMA (Tier 1) is used when >= 5 samples exist."""
        estimator = QuotaEstimator()
        # Record 5 observations to build EWMA
        for _ in range(5):
            estimator.record_usage(
                "acct", tokens=100, cost_microdollars=500, model_id="m"
            )

        cost = estimator.estimate_cost("acct", "m", 1000)

        # The EWMA estimate should produce a nonzero cost
        assert cost > 0
        # Verify it differs from what Tier 2-5 would produce
        # (EWMA is based on actual observed cost_per_token)
        ewma = estimator.account_model_ewma["acct"]["m"]
        assert ewma.sample_count == 5
        expected_ewma = int(
            1000 * ewma.estimate_cost_per_token * estimator.default_safety_factor
        )
        assert cost == max(expected_ewma, 1)

    def test_tier2_global_ewma_used_when_no_account_ewma(self) -> None:
        """Global model EWMA (Tier 2) is used when account EWMA is absent."""
        estimator = QuotaEstimator()
        # Record usage for a different account to build global EWMA only
        for _ in range(5):
            estimator.record_usage(
                "other-acct", tokens=100, cost_microdollars=500, model_id="m"
            )

        cost = estimator.estimate_cost("acct", "m", 1000)

        ewma = estimator.global_model_ewma["m"]
        assert ewma.sample_count == 5
        expected = int(
            1000 * ewma.estimate_cost_per_token * estimator.default_safety_factor
        )
        assert cost == max(expected, 1)

    def test_family_matching_prefers_longer_names(self) -> None:
        """Longer/more specific family names match first."""
        estimator = QuotaEstimator()
        # "gpt-4o-mini" should match the mini family, not the generic gpt-4 family
        cost_mini = estimator.estimate_cost("acct", "gpt-4o-mini", 1000)

        mini_rate = MODEL_FAMILY_FALLBACKS["gpt-4o-mini"]
        generic_rate = MODEL_FAMILY_FALLBACKS["gpt-4"]
        mini_avg = (mini_rate[0] + mini_rate[1]) / 2.0
        generic_avg = (generic_rate[0] + generic_rate[1]) / 2.0

        mini_expected = int(1000 * mini_avg * estimator.default_safety_factor)
        generic_expected = int(1000 * generic_avg * estimator.default_safety_factor)

        assert cost_mini == max(mini_expected, 1)
        assert cost_mini != max(generic_expected, 1)

    def test_minimum_cost_is_one(self) -> None:
        """Estimated cost is always at least 1 microdollar."""
        estimator = QuotaEstimator()
        cost = estimator.estimate_cost("acct", "unknown", 0)
        assert cost >= 1


class TestEWMAHardCaps:
    """EWMA tables are bounded by ``ewma_hard_cap`` / ``global_ewma_hard_cap``.

    Phase 2 of the memory footprint plan: the per-account bucket evicts
    its least-recently-touched (account, model) entry on insert
    overflow, and the outer account dict evicts its least-recently-
    touched bucket on new-account overflow. The global model EWMA
    behaves the same way for its single-level dict.
    """

    def test_account_bucket_evicts_lru_on_overflow(self) -> None:
        """Insert beyond ``ewma_hard_cap`` drops the oldest model keys."""
        cap = 4
        estimator = QuotaEstimator(ewma_hard_cap=cap)
        for i in range(10):
            estimator.record_usage(
                account_name="acct-a",
                tokens=1000,
                cost_microdollars=2000,
                model_id=f"model-{i}",
            )
        bucket = estimator.account_model_ewma["acct-a"]
        assert len(bucket) == cap
        # With LRU eviction on a cap of 4, the last 4 models survive.
        assert "model-9" in bucket
        assert "model-8" in bucket
        assert "model-7" in bucket
        assert "model-6" in bucket
        assert "model-0" not in bucket

    def test_account_dict_evicts_lru_account_on_overflow(self) -> None:
        """Insert beyond ``ewma_hard_cap`` distinct accounts drops the oldest bucket."""
        cap = 4
        estimator = QuotaEstimator(ewma_hard_cap=cap)
        for i in range(10):
            estimator.record_usage(
                account_name=f"acct-{i}",
                tokens=1000,
                cost_microdollars=2000,
                model_id="gpt-4",
            )
        assert len(estimator.account_model_ewma) == cap
        assert "acct-9" in estimator.account_model_ewma
        assert "acct-0" not in estimator.account_model_ewma

    def test_global_model_ewma_evicts_lru_on_overflow(self) -> None:
        """Insert beyond ``global_ewma_hard_cap`` drops the oldest model keys."""
        cap = 4
        estimator = QuotaEstimator(global_ewma_hard_cap=cap)
        for i in range(10):
            estimator.record_usage(
                account_name="acct-a",
                tokens=1000,
                cost_microdollars=2000,
                model_id=f"model-{i}",
            )
        assert len(estimator.global_model_ewma) == cap
        assert "model-9" in estimator.global_model_ewma
        assert "model-0" not in estimator.global_model_ewma

    def test_existing_keys_are_moved_to_mru_position(self) -> None:
        """Updating an existing key pops it to the MRU end so it survives eviction."""
        estimator = QuotaEstimator(global_ewma_hard_cap=4)
        # Seed with 4 distinct models so the dict is at the cap.
        for name in ("model-a", "model-b", "model-c", "model-d"):
            estimator.record_usage(
                account_name="acct-a",
                tokens=1000,
                cost_microdollars=2000,
                model_id=name,
            )
        # Touch the OLDEST key twice: it should be promoted to MRU.
        for _ in range(2):
            estimator.record_usage(
                account_name="acct-a",
                tokens=1000,
                cost_microdollars=2000,
                model_id="model-a",
            )
        # Insert a NEW key. The LRU is now model-b (oldest after promotion).
        estimator.record_usage(
            account_name="acct-a",
            tokens=1000,
            cost_microdollars=2000,
            model_id="model-e",
        )
        assert "model-a" in estimator.global_model_ewma
        assert "model-b" not in estimator.global_model_ewma
        assert "model-e" in estimator.global_model_ewma

    def test_default_caps_match_module_constants(self) -> None:
        """Zero-arg ``QuotaEstimator()`` uses the module-level hard caps."""
        estimator = QuotaEstimator()
        assert estimator.ewma_hard_cap == EWMA_HARD_CAP
        assert estimator.global_ewma_hard_cap == GLOBAL_EWMA_HARD_CAP
        assert EWMA_HARD_CAP >= 1
        assert GLOBAL_EWMA_HARD_CAP >= 1
