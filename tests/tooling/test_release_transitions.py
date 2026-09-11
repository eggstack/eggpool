"""Contract tests for the release compatibility transition matrix runner."""

from __future__ import annotations

from scripts.qualify_release_transitions import (
    MANAGERS,
    PYTHON_VERSION,
    RUST_VERSION,
    _record,
)


def test_runner_declares_all_required_manager_classes() -> None:
    assert MANAGERS == ("uv-tool", "pipx", "pip")


def test_result_contains_bounded_transition_evidence() -> None:
    result = _record(
        "pip",
        (PYTHON_VERSION, "python"),
        (RUST_VERSION, "rust"),
        0.0,
        result="pass",
        rollback="pass",
        cli_version=RUST_VERSION,
        metadata_version=RUST_VERSION,
        state={
            "config_sha256": "config",
            "db_sha256": "database",
            "db_integrity": "ok",
            "migration_max": 54,
            "row_counts": {"requests": 1},
        },
    )
    assert {
        "manager_class",
        "source_version",
        "source_era",
        "target_version",
        "target_era",
        "result",
        "rollback_result",
        "cli_package_metadata_version",
        "cli_version",
        "config_sha256",
        "db_observation",
        "elapsed_ms",
        "error",
    } == set(result)
    assert result["db_observation"] == {
        "sha256": "database",
        "integrity": "ok",
        "migration_max": 54,
        "row_counts": {"requests": 1},
    }
