"""Tests for configuration loading and validation."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from go_aggregator.errors import ConfigError
from go_aggregator.models.config import AppConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def valid_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[server]
host = "127.0.0.1"
port = 9000
api_key_env = "TEST_API_KEY"
log_level = "DEBUG"
access_log = false

[upstream]
base_url = "https://api.example.com"
connect_timeout_s = 3

[database]
path = "test.sqlite3"
wal = true
synchronous = "NORMAL"

[models]
refresh_interval_s = 600
expose_mode = "union"

[routing]
strategy = "quota_fair"

[limits]
five_hour_microdollars = 50000000

[[accounts]]
name = "test_account"
api_key_env = "TEST_KEY_1"
weight = 1.0

[[accounts]]
name = "test_account_2"
api_key_env = "TEST_KEY_2"
weight = 2.0
"""
    )
    return config_file


def test_load_valid_config(valid_config: Path) -> None:
    os.environ["TEST_KEY_1"] = "key1"
    os.environ["TEST_KEY_2"] = "key2"
    config = AppConfig.from_toml(str(valid_config))
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 9000
    assert len(config.accounts) == 2
    assert config.accounts[0].name == "test_account"
    del os.environ["TEST_KEY_1"]
    del os.environ["TEST_KEY_2"]


def test_missing_required_fields(tmp_path: Path) -> None:
    config_file = tmp_path / "bad.toml"
    config_file.write_text('[server]\nport = "not_a_number"\n')
    with pytest.raises(ConfigError, match="Config validation failed"):
        AppConfig.from_toml(str(config_file))


def test_duplicate_account_names(tmp_path: Path) -> None:
    os.environ["DUP_KEY"] = "key"
    config_file = tmp_path / "dup.toml"
    config_file.write_text(
        """
[[accounts]]
name = "same_name"
api_key_env = "DUP_KEY"

[[accounts]]
name = "same_name"
api_key_env = "DUP_KEY"
"""
    )
    with pytest.raises(ConfigError, match="Duplicate account name"):
        AppConfig.from_toml(str(config_file))
    del os.environ["DUP_KEY"]


def test_missing_env_var_for_enabled_account(tmp_path: Path) -> None:
    config_file = tmp_path / "missing_env.toml"
    config_file.write_text(
        """
[[accounts]]
name = "missing_env"
api_key_env = "NONEXISTENT_ENV_VAR_XYZ"
"""
    )
    with pytest.raises(ConfigError, match="is not set"):
        AppConfig.from_toml(str(config_file))


def test_zero_weight_rejected(tmp_path: Path) -> None:
    os.environ["ZERO_KEY"] = "key"
    config_file = tmp_path / "zero.toml"
    config_file.write_text(
        """
[[accounts]]
name = "zero_weight"
api_key_env = "ZERO_KEY"
weight = 0
"""
    )
    with pytest.raises(ConfigError, match="non-positive weight"):
        AppConfig.from_toml(str(config_file))
    del os.environ["ZERO_KEY"]


def test_negative_weight_rejected(tmp_path: Path) -> None:
    os.environ["NEG_KEY"] = "key"
    config_file = tmp_path / "neg.toml"
    config_file.write_text(
        """
[[accounts]]
name = "neg_weight"
api_key_env = "NEG_KEY"
weight = -1.5
"""
    )
    with pytest.raises(ConfigError, match="non-positive weight"):
        AppConfig.from_toml(str(config_file))
    del os.environ["NEG_KEY"]


def test_file_not_found() -> None:
    with pytest.raises(ConfigError, match="not found"):
        AppConfig.from_toml("/nonexistent/path/config.toml")


def test_invalid_toml(tmp_path: Path) -> None:
    config_file = tmp_path / "bad_syntax.toml"
    config_file.write_text("[unclosed\n")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        AppConfig.from_toml(str(config_file))


def test_extra_fields_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "extra.toml"
    config_file.write_text("[server]\nunknown_field = true\n")
    with pytest.raises(ConfigError, match="Config validation failed"):
        AppConfig.from_toml(str(config_file))


def test_disabled_account_skips_env_check(tmp_path: Path) -> None:
    config_file = tmp_path / "disabled.toml"
    config_file.write_text(
        """
[[accounts]]
name = "disabled_account"
api_key_env = "TOTALLY_UNSET_ENV_VAR"
enabled = false
"""
    )
    config = AppConfig.from_toml(str(config_file))
    assert config.accounts[0].name == "disabled_account"
    assert config.accounts[0].enabled is False
