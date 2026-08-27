"""Integration tests for 402 quota-exhausted cooldown (Phase 14)."""

from __future__ import annotations

import time

from eggpool.health.health_manager import HealthManager


def test_record_quota_exhausted_sets_cooldown() -> None:
    """402 places account into bounded cooldown."""
    hm = HealthManager()
    before = hm.clock()
    hm.record_quota_exhausted("acct-a", cooldown_seconds=300.0)
    after = hm.clock()

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


def test_quota_cooldown_is_not_shortened() -> None:
    """Repeated quota signals preserve the longest active cooldown."""
    now = [100.0]
    hm = HealthManager(clock=lambda: now[0])

    hm.record_quota_exhausted("acct-a", cooldown_seconds=300.0)
    now[0] = 110.0
    hm.record_quota_exhausted("acct-a", cooldown_seconds=1.0)

    assert hm.get_account_health("acct-a").cooldown_until == 400.0


def test_rate_limit_cooldown_is_not_shortened() -> None:
    """Repeated rate-limit signals preserve the longest active cooldown."""
    now = [100.0]
    hm = HealthManager(clock=lambda: now[0])

    hm.record_rate_limit("acct-a", retry_after_seconds=300.0)
    now[0] = 110.0
    hm.record_rate_limit("acct-a", retry_after_seconds=1.0)

    assert hm.get_account_health("acct-a").cooldown_until == 400.0
