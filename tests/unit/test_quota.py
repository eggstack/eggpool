"""Tests for quota estimation and reservations."""

from __future__ import annotations

import time

import pytest

from go_aggregator.quota.estimation import (
    AccountQuota,
    ManualOffset,
    QuotaEstimator,
    QuotaWindow,
)
from go_aggregator.quota.reservation import Reservation, ReservationManager
from go_aggregator.quota.scorer import QuotaFairScorer, RoutingScore


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
            max_daily_cost_microdollars=10000,
        )
        quota.record_usage(100, 500)
        assert quota.is_within_limits()

        quota.record_usage(100, 9500)
        assert not quota.is_within_limits()

    def test_remaining_capacity(self) -> None:
        """Test remaining capacity calculation."""
        quota = AccountQuota(
            account_name="test-account",
            max_daily_cost_microdollars=10000,
        )
        quota.record_usage(100, 5000)
        capacity = quota.get_remaining_capacity()
        assert capacity == pytest.approx(0.5)

    def test_manual_offset(self) -> None:
        """Test manual offset application."""
        quota = AccountQuota(account_name="test-account")
        quota.record_usage(100, 500)

        quota.manual_offset = ManualOffset(tokens=50, cost_microdollars=250)
        tokens, cost = quota.get_effective_usage()
        assert tokens == 150
        assert cost == 750


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
        estimator.set_account_limits("account1", max_daily_cost_microdollars=10000)

        quota = estimator.get_account_quota("account1")
        assert quota is not None
        assert quota.max_daily_cost_microdollars == 10000

    def test_eligible_accounts(self) -> None:
        """Test getting eligible accounts."""
        estimator = QuotaEstimator()
        estimator.set_account_limits("account1", max_daily_cost_microdollars=10000)
        estimator.set_account_limits("account2", max_daily_cost_microdollars=10000)

        # Account1 is at 50% usage
        estimator.record_usage("account1", 100, 5000)
        # Account2 is at 90% usage
        estimator.record_usage("account2", 100, 9000)

        eligible = estimator.get_eligible_accounts(["account1", "account2"])
        assert len(eligible) == 2
        assert eligible[0][0] == "account1"  # Higher capacity
        assert eligible[0][1] > eligible[1][1]  # Higher remaining capacity


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


class TestQuotaFairScorer:
    """Tests for QuotaFairScorer."""

    def test_score_accounts(self) -> None:
        """Test scoring accounts."""
        estimator = QuotaEstimator()
        estimator.set_account_limits("account1", max_daily_cost_microdollars=10000)
        estimator.set_account_limits("account2", max_daily_cost_microdollars=10000)
        estimator.record_usage("account1", 100, 5000)
        estimator.record_usage("account2", 100, 9000)

        scorer = QuotaFairScorer(quota_estimator=estimator)
        scores = scorer.score_accounts(["account1", "account2"])

        assert len(scores) == 2
        assert scores[0].quota_score > scores[1].quota_score

    def test_select_account(self) -> None:
        """Test account selection."""
        scorer = QuotaFairScorer()
        scores = [
            RoutingScore("account1", 1.0, 1.0, True),
            RoutingScore("account2", 0.5, 1.0, True),
        ]

        selected = scorer.select_account(scores)
        assert selected is not None
        assert selected.account_name == "account1"

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
        """Test account ranking."""
        scorer = QuotaFairScorer()
        scores = [
            RoutingScore("account1", 0.5, 1.0, True),
            RoutingScore("account2", 1.0, 1.0, True),
            RoutingScore("account3", 0.75, 1.0, True),
        ]

        ranked = scorer.rank_accounts(scores)
        assert ranked[0].account_name == "account2"
        assert ranked[1].account_name == "account3"
        assert ranked[2].account_name == "account1"
