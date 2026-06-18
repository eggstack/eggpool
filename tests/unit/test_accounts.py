"""Tests for account registry and runtime state."""

from __future__ import annotations

import os

import pytest

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.accounts.state import AccountRuntimeState
from go_aggregator.errors import ConfigError
from go_aggregator.models.config import AppConfig


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
