"""Plan 030 — Configuration and migration validation (Workstream K).

Test:
- Old configuration with no new fields.
- New provider-control policy fields.
- Model capability overrides.
- Database recovery settings.
- Dispatch writer metric/batching settings.
- Instrumentation sampling settings.
- Invalid values and contradictory combinations.
- Rehash changes classified correctly as live/restart-required.
- Database migrations from representative prior schema versions.
- Legacy terminal model-unavailable data.

``eggpool check-config`` must validate all new configuration before
startup or rehash.

Run with::

    uv run pytest tests/unit/test_plan_030_config_validation.py -v
"""

from __future__ import annotations

import pytest

from eggpool.config_reload_policy import (
    ReloadDisposition,
)
from eggpool.config_reload_policy import (
    _disposition_for as classify_reload_field,
)
from eggpool.errors import ConfigError
from eggpool.models.config import (
    AppConfig,
    DatabaseRecoveryConfig,
    DispatchSpansConfig,
    DispatchWriterConfig,
)
from eggpool.transcoder.policy import ProviderControlPolicyConfig

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Old configuration with no new fields
# ---------------------------------------------------------------------------


class TestOldConfigCompatibility:
    """Old configuration with no new fields must remain valid."""

    def test_minimal_config_valid(self) -> None:
        """A minimal config with no new fields must validate."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "TEST_KEY",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": ":memory:"},
                "upstream": {"base_url": "https://test.example.com"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                "dashboard": {"enabled": False},
            }
        )
        assert config is not None

    def test_config_without_provider_control_policy(self) -> None:
        """Config without provider-control policy fields must validate."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "TEST_KEY",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": ":memory:"},
                "upstream": {"base_url": "https://test.example.com"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                "dashboard": {"enabled": False},
            }
        )
        # Provider control policy should have defaults
        assert config.transcoder.provider_control_policy is not None

    def test_config_without_recovery_settings(self) -> None:
        """Config without database recovery settings must validate with
        defaults."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "TEST_KEY",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": ":memory:"},
                "upstream": {"base_url": "https://test.example.com"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                "dashboard": {"enabled": False},
            }
        )
        # Recovery should have defaults
        assert config.database.recovery.enabled is True

    def test_config_without_dispatch_writer(self) -> None:
        """Config without dispatch writer settings must validate with
        defaults."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "TEST_KEY",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": ":memory:"},
                "upstream": {"base_url": "https://test.example.com"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                "dashboard": {"enabled": False},
            }
        )
        # Dispatch writer should default to disabled
        assert config.database.dispatch_writer.enabled is False

    def test_config_without_span_sampling(self) -> None:
        """Config without span sampling settings must validate with
        defaults."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "TEST_KEY",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": ":memory:"},
                "upstream": {"base_url": "https://test.example.com"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                "dashboard": {"enabled": False},
            }
        )
        # Span sampling should default to 5%
        assert config.metrics.dispatch_spans.sample_rate == 0.05


# ---------------------------------------------------------------------------
# New provider-control policy fields
# ---------------------------------------------------------------------------


class TestProviderControlPolicyFields:
    """New provider-control policy fields must validate."""

    def test_provider_control_policy_defaults(self) -> None:
        """Provider control policy has correct defaults."""
        policy = ProviderControlPolicyConfig()
        assert policy.unsupported_control == "reject"
        assert policy.unknown_contract == "allow_with_warning"
        assert policy.allow_compatibility_retry is False

    def test_provider_control_policy_custom(self) -> None:
        """Provider control policy can be customized."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "TEST_KEY",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": ":memory:"},
                "upstream": {"base_url": "https://test.example.com"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                "dashboard": {"enabled": False},
                "transcoder": {
                    "provider_control_policy": {
                        "unsupported_control": "reject",
                        "unknown_contract": "reject",
                        "allow_compatibility_retry": False,
                    }
                },
            }
        )
        policy = config.transcoder.provider_control_policy
        assert policy.unsupported_control == "reject"
        assert policy.unknown_contract == "reject"
        assert policy.allow_compatibility_retry is False


# ---------------------------------------------------------------------------
# Database recovery settings
# ---------------------------------------------------------------------------


class TestDatabaseRecoverySettings:
    """Database recovery settings must validate."""

    def test_recovery_defaults(self) -> None:
        """Recovery config has correct defaults."""
        recovery = DatabaseRecoveryConfig()
        assert recovery.enabled is True
        assert recovery.max_attempts >= 1
        assert recovery.initial_backoff_ms > 0
        assert recovery.max_backoff_ms > recovery.initial_backoff_ms

    def test_recovery_custom(self) -> None:
        """Recovery config can be customized."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "TEST_KEY",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {
                    "path": ":memory:",
                    "recovery": {
                        "enabled": True,
                        "max_attempts": 5,
                        "initial_backoff_ms": 200,
                        "max_backoff_ms": 10000,
                    },
                },
                "upstream": {"base_url": "https://test.example.com"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                "dashboard": {"enabled": False},
            }
        )
        recovery = config.database.recovery
        assert recovery.max_attempts == 5
        assert recovery.initial_backoff_ms == 200
        assert recovery.max_backoff_ms == 10000

    def test_recovery_invalid_max_attempts(self) -> None:
        """Invalid max_attempts raises validation error."""
        with pytest.raises(ConfigError):
            AppConfig.from_dict(
                {
                    "server": {
                        "api_key_env": "TEST_KEY",
                        "host": "127.0.0.1",
                        "port": 0,
                    },
                    "database": {
                        "path": ":memory:",
                        "recovery": {
                            "enabled": True,
                            "max_attempts": 0,
                        },
                    },
                    "upstream": {"base_url": "https://test.example.com"},
                    "models": {"startup_refresh": False, "refresh_interval_s": 0},
                    "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                    "dashboard": {"enabled": False},
                }
            )


# ---------------------------------------------------------------------------
# Dispatch writer settings
# ---------------------------------------------------------------------------


class TestDispatchWriterSettings:
    """Dispatch writer settings must validate."""

    def test_dispatch_writer_defaults(self) -> None:
        """Dispatch writer config has correct defaults."""
        writer = DispatchWriterConfig()
        assert writer.enabled is False
        assert writer.max_queue_depth > 0
        assert writer.max_batch_size > 0
        assert writer.max_batch_wait_ms > 0

    def test_dispatch_writer_enabled(self) -> None:
        """Dispatch writer can be enabled."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "TEST_KEY",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {
                    "path": ":memory:",
                    "dispatch_writer": {
                        "enabled": True,
                        "max_queue_depth": 1000,
                        "max_batch_size": 50,
                    },
                },
                "upstream": {"base_url": "https://test.example.com"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                "dashboard": {"enabled": False},
            }
        )
        writer = config.database.dispatch_writer
        assert writer.enabled is True
        assert writer.max_queue_depth == 1000
        assert writer.max_batch_size == 50


# ---------------------------------------------------------------------------
# Instrumentation sampling settings
# ---------------------------------------------------------------------------


class TestInstrumentationSamplingSettings:
    """Instrumentation sampling settings must validate."""

    def test_span_sampling_defaults(self) -> None:
        """Span sampling config has correct defaults."""
        spans = DispatchSpansConfig()
        assert spans.sample_rate == 0.05
        assert spans.window_size > 0

    def test_span_sampling_custom(self) -> None:
        """Span sampling can be customized."""
        config = AppConfig.from_dict(
            {
                "server": {
                    "api_key_env": "TEST_KEY",
                    "host": "127.0.0.1",
                    "port": 0,
                },
                "database": {"path": ":memory:"},
                "upstream": {"base_url": "https://test.example.com"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                "dashboard": {"enabled": False},
                "metrics": {
                    "dispatch_spans": {
                        "sample_rate": 0.10,
                        "window_size": 500,
                    }
                },
            }
        )
        spans = config.metrics.dispatch_spans
        assert spans.sample_rate == 0.10
        assert spans.window_size == 500

    def test_span_sampling_invalid_rate(self) -> None:
        """Invalid sample rate raises validation error."""
        with pytest.raises(ConfigError):
            AppConfig.from_dict(
                {
                    "server": {
                        "api_key_env": "TEST_KEY",
                        "host": "127.0.0.1",
                        "port": 0,
                    },
                    "database": {"path": ":memory:"},
                    "upstream": {"base_url": "https://test.example.com"},
                    "models": {"startup_refresh": False, "refresh_interval_s": 0},
                    "accounts": [{"name": "test", "api_key_env": "TEST_KEY"}],
                    "dashboard": {"enabled": False},
                    "metrics": {
                        "dispatch_spans": {
                            "sample_rate": 1.5,
                        }
                    },
                }
            )


# ---------------------------------------------------------------------------
# Rehash classification
# ---------------------------------------------------------------------------


class TestRehashClassification:
    """Rehash changes classified correctly as live/restart-required."""

    def test_provider_control_policy_live(self) -> None:
        """Provider control policy changes are live."""
        assert classify_reload_field("transcoder.provider_control_policy") == (
            ReloadDisposition.LIVE
        )

    def test_recovery_enabled_live(self) -> None:
        """Database recovery settings are live."""
        assert classify_reload_field("database.recovery.enabled") == (
            ReloadDisposition.LIVE
        )

    def test_dispatch_writer_enabled_restart_required(self) -> None:
        """Dispatch writer enabled flag requires restart."""
        assert classify_reload_field("database.dispatch_writer.enabled") == (
            ReloadDisposition.RESTART_REQUIRED
        )

    def test_span_sampling_live(self) -> None:
        """Span sampling settings are live."""
        assert classify_reload_field("metrics.dispatch_spans.sample_rate") == (
            ReloadDisposition.LIVE
        )

    def test_server_port_restart_required(self) -> None:
        """Server port changes require restart."""
        assert classify_reload_field("server.port") == (
            ReloadDisposition.RESTART_REQUIRED
        )

    def test_database_path_restart_required(self) -> None:
        """Database path changes require restart."""
        assert classify_reload_field("database.path") == (
            ReloadDisposition.RESTART_REQUIRED
        )
