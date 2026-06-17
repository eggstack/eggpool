"""Integration tests for 402 quota-exhausted cooldown (Phase 14)."""

from __future__ import annotations

import time

from go_aggregator.health.health_manager import HealthManager


def test_record_quota_exhausted_sets_cooldown() -> None:
    """402 places account into bounded cooldown."""
    hm = HealthManager()
    before = time.time()
    hm.record_quota_exhausted("acct-a", cooldown_seconds=300.0)
    after = time.time()

    health = hm.get_account_health("acct-a")
    assert health.health_state == "quota_exhausted"
    assert not health.is_healthy
    assert health.cooldown_until >= before + 300.0
    assert health.cooldown_until <= after + 300.0


def test_quota_exhausted_account_not_healthy() -> None:
    """Account in quota-exhausted cooldown is not healthy."""
    hm = HealthManager()
    hm.record_quota_exhausted("acct-a", cooldown_seconds=300.0)
    assert not hm.is_account_healthy("acct-a")


def test_quota_exhausted_account_becomes_eligible_after_cooldown() -> None:
    """Account becomes eligible after cooldown expires."""
    hm = HealthManager()
    hm.record_quota_exhausted("acct-a", cooldown_seconds=0.1)

    # Still in cooldown
    assert not hm.is_account_healthy("acct-a")

    # Wait for cooldown to expire
    time.sleep(0.5)
    assert hm.is_account_healthy("acct-a")
