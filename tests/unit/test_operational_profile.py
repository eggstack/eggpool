"""Tests for the Milestone A6 operational profile logging.

Pins the schema of the structured profile line so future changes
cannot silently drop fields that downstream tooling consumes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from eggpool.app import _log_operational_profile
from eggpool.background import TaskSupervisor
from eggpool.runtime_manager import ProcessRuntime

if TYPE_CHECKING:
    import pytest


def _make_config() -> object:
    from eggpool.models.config import AppConfig

    return AppConfig.from_dict(
        {
            "server": {"api_key": "ep_test_operational_profile_0000000"},
            "providers": {
                "opencode-go": {
                    "id": "opencode-go",
                    "base_url": "https://opencode.ai/zen/go/v1",
                    "protocols": ["openai"],
                    "models_endpoint": {"method": "GET", "path": "/models"},
                    "accounts": [
                        {
                            "name": "default",
                            "api_key": "sk-test-operational-profile-000",
                            "enabled": True,
                            "weight": 1.0,
                        }
                    ],
                }
            },
        }
    )


def _make_process() -> ProcessRuntime:
    return ProcessRuntime(
        db=object(),
        stats_db=None,
        config_path=None,
        metrics_coalescer=None,
    )


class TestOperationalProfile:
    """The profile log must contain every documented field."""

    def test_profile_includes_all_documented_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = _make_config()
        process = _make_process()
        supervisor = TaskSupervisor()
        process_supervisor = TaskSupervisor()

        with caplog.at_level(logging.INFO, logger="eggpool.app"):
            _log_operational_profile(
                config=config,
                db=object(),
                stats_db=None,
                process=process,
                supervisor=supervisor,
                process_supervisor=process_supervisor,
                model_info_enabled=True,
            )

        profile_records = [
            record for record in caplog.records if "Operational profile" in record.msg
        ]
        assert profile_records, "Operational profile log line was not emitted"
        # The helper passes the dict via ``extra={"profile": ...}`` so
        # structured-log consumers can parse it directly.
        record = profile_records[0]
        profile = getattr(record, "profile", None)
        assert isinstance(profile, dict), (
            f"profile not exposed via record.profile: {profile!r}"
        )
        required_keys = {
            "workers",
            "runtime_threads",
            "database_worker_threads",
            "stats_db_separate",
            "wal",
            "synchronous",
            "busy_timeout_ms",
            "routing_trace_mode",
            "routing_trace_sample_rate",
            "metrics_write_mode",
            "metrics_flush_interval_s",
            "transcoder_enabled",
            "compression_enabled",
            "compression_mode",
            "model_info_enabled",
            "task_total",
            "task_process_owned",
            "task_generation_leased",
            "process_task_spec_version",
        }
        missing = required_keys - profile.keys()
        assert not missing, f"Missing profile keys: {missing}"

    def test_profile_excludes_secrets_and_request_content(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The profile must never include the API key, raw URLs with
        credentials, or any per-request content."""
        config = _make_config()
        process = _make_process()
        supervisor = TaskSupervisor()
        process_supervisor = TaskSupervisor()

        with caplog.at_level(logging.INFO, logger="eggpool.app"):
            _log_operational_profile(
                config=config,
                db=object(),
                stats_db=None,
                process=process,
                supervisor=supervisor,
                process_supervisor=process_supervisor,
                model_info_enabled=True,
            )
        for record in caplog.records:
            if "Operational profile" not in record.msg:
                continue
            rendered = record.getMessage()
            # No raw API keys.
            assert "ep_test_operational_profile_0000000" not in rendered
            assert "sk-test-operational-profile-000" not in rendered
            # No request bodies or upstream URLs.
            assert "messages" not in rendered
            assert "Authorization" not in rendered
            assert "Bearer" not in rendered

    def test_profile_task_counts_split_by_ownership(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Process-owned tasks must be counted separately from
        generation-leased tasks so the operator can verify process /
        generation ownership is correct after a reload."""

        async def noop() -> None:
            return None

        config = _make_config()
        process = _make_process()
        supervisor = TaskSupervisor()
        process_supervisor = TaskSupervisor()

        # Generation-leased task on the gen supervisor.
        supervisor.register_periodic("catalog_refresh", noop, interval_s=60.0)
        # Process-owned tasks on the process supervisor.
        process_supervisor.register_periodic(
            "checkpoint", noop, interval_s=14400.0, run_immediately=True
        )
        process_supervisor.register_periodic("metrics_flush", noop, interval_s=30.0)

        with caplog.at_level(logging.INFO, logger="eggpool.app"):
            _log_operational_profile(
                config=config,
                db=object(),
                stats_db=None,
                process=process,
                supervisor=supervisor,
                process_supervisor=process_supervisor,
                model_info_enabled=False,
            )

        record = next(r for r in caplog.records if "Operational profile" in r.msg)
        profile = getattr(record, "profile", None)
        assert isinstance(profile, dict)
        assert profile["task_total"] == 3
        assert profile["task_process_owned"] == 2
        assert profile["task_generation_leased"] == 1
        assert profile["model_info_enabled"] is False
