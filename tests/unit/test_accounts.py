"""Tests for account registry and runtime state."""

from __future__ import annotations

import os
import time

import pytest

from eggpool.accounts.registry import AccountRegistry
from eggpool.accounts.state import (
    DEFAULT_BACKOFF_MAX_SECONDS,
    AccountRuntimeState,
)
from eggpool.errors import ConfigError
from eggpool.models.config import AppConfig


def test_account_runtime_state_eligible() -> None:
    state = AccountRuntimeState(name="test", enabled=True)
    assert state.is_eligible() is True


def test_account_runtime_state_disabled() -> None:
    state = AccountRuntimeState(name="test", enabled=False)
    assert state.is_eligible() is False


def test_account_runtime_state_auth_failed() -> None:
    state = AccountRuntimeState(name="test", health_state="authentication_failed")
    assert state.is_eligible() is False


def test_account_runtime_state_quota_exhausted() -> None:
    state = AccountRuntimeState(name="test", health_state="quota_exhausted")
    assert state.is_eligible() is False


def test_account_runtime_state_record_success() -> None:
    state = AccountRuntimeState(name="test", health_state="cooldown")
    state.record_success()
    assert state.health_state == "healthy"
    assert state.consecutive_failures == 0


def test_account_runtime_state_record_failure() -> None:
    state = AccountRuntimeState(name="test")
    state.record_failure("rate_limited")
    assert state.consecutive_failures == 1
    assert state.health_state == "cooldown"


def test_account_runtime_state_auth_failure() -> None:
    state = AccountRuntimeState(name="test")
    state.record_failure("authentication")
    assert state.health_state == "authentication_failed"


def test_account_runtime_state_reset_health() -> None:
    state = AccountRuntimeState(
        name="test",
        health_state="cooldown",
        consecutive_failures=5,
    )
    state.reset_health()
    assert state.health_state == "healthy"
    assert state.consecutive_failures == 0


def test_account_registry_loads_accounts() -> None:
    os.environ["TEST_REG_KEY_1"] = "key1"
    os.environ["TEST_REG_KEY_2"] = "key2"
    try:
        config = AppConfig.from_dict(
            {
                "accounts": [
                    {"name": "acct1", "api_key_env": "TEST_REG_KEY_1"},
                    {"name": "acct2", "api_key_env": "TEST_REG_KEY_2"},
                ]
            }
        )
        registry = AccountRegistry(config)
        assert len(registry.get_all_states()) == 2
        assert registry.get_api_key("acct1") == "key1"
    finally:
        del os.environ["TEST_REG_KEY_1"]
        del os.environ["TEST_REG_KEY_2"]


def test_account_registry_rejects_missing_key() -> None:
    config = AppConfig.from_dict(
        {
            "accounts": [
                {"name": "acct1", "api_key_env": "MISSING_KEY_XYZ"},
            ]
        }
    )
    with pytest.raises(ConfigError, match="is not set"):
        config.validate_account_credentials()


def test_account_registry_disabled_skips_key_check() -> None:
    config = AppConfig.from_dict(
        {
            "accounts": [
                {
                    "name": "disabled",
                    "api_key_env": "MISSING_KEY_XYZ",
                    "enabled": False,
                },
            ]
        }
    )
    registry = AccountRegistry(config)
    assert len(registry.get_all_states()) == 1
    assert registry.get_all_states()[0].enabled is False


def test_account_registry_eligible_states() -> None:
    os.environ["TEST_ELIGIBLE_KEY"] = "key"
    try:
        config = AppConfig.from_dict(
            {
                "accounts": [
                    {"name": "enabled", "api_key_env": "TEST_ELIGIBLE_KEY"},
                    {
                        "name": "disabled",
                        "api_key_env": "TEST_ELIGIBLE_KEY",
                        "enabled": False,
                    },
                ]
            }
        )
        registry = AccountRegistry(config)
        eligible = registry.get_eligible_states()
        assert len(eligible) == 1
        assert eligible[0].name == "enabled"
    finally:
        del os.environ["TEST_ELIGIBLE_KEY"]


def test_backoff_max_is_one_hour() -> None:
    """Backoff cap should be 3600 seconds (1 hour)."""
    assert DEFAULT_BACKOFF_MAX_SECONDS == 3600.0


def test_backoff_exponential_with_cap() -> None:
    """Backoff should double each failure, capped at 1 hour."""
    state = AccountRuntimeState(name="test")
    before = time.time()

    # 1st failure: 30s
    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 25.0
    assert state.cooldown_until <= before + 35.0

    # 2nd failure: 60s
    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 55.0
    assert state.cooldown_until <= before + 65.0

    # 3rd failure: 120s
    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 115.0
    assert state.cooldown_until <= before + 125.0

    # 4th failure: 240s
    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 235.0
    assert state.cooldown_until <= before + 245.0

    # 5th failure: 480s
    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 475.0
    assert state.cooldown_until <= before + 485.0

    # 6th failure: 960s
    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 955.0
    assert state.cooldown_until <= before + 965.0

    # 7th failure: 1920s
    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 1915.0
    assert state.cooldown_until <= before + 1925.0

    # 8th failure: 3600s (capped)
    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 3595.0
    assert state.cooldown_until <= before + 3605.0

    # 9th failure: still 3600s (capped)
    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 3595.0
    assert state.cooldown_until <= before + 3605.0


def test_backoff_resets_on_different_error_class() -> None:
    """Backoff counter should reset when error class changes."""
    state = AccountRuntimeState(name="test")
    before = time.time()

    state.record_failure("rate_limited")
    assert state.cooldown_until > before + 25.0
    assert state.cooldown_until <= before + 35.0

    # Different error class resets counter
    state.record_failure("connect_timeout")
    assert state.cooldown_until > before + 25.0
    assert state.cooldown_until <= before + 35.0


def test_retry_after_overrides_backoff() -> None:
    """Retry-After header should take precedence over exponential backoff."""
    state = AccountRuntimeState(name="test")
    before = time.time()

    state.record_failure("rate_limited", rate_limit_retry_after=120.0)
    assert state.cooldown_until > before + 115.0
    assert state.cooldown_until <= before + 125.0
