"""R001 runtime/reload/task oracle freeze tests."""

from __future__ import annotations

import json

from eggpool.runtime_task_inventory import RUNTIME_TASK_INVENTORY
from tests.migration_rs.runtime_lifecycle_fixtures import (
    FIXTURE_PATH,
    SCHEMA_VERSION,
    build_observation_bundle,
    observation_json,
)


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_r001_oracle_is_repeatable_and_matches_committed_fixture() -> None:
    first = build_observation_bundle()
    second = build_observation_bundle()
    assert observation_json() == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )
    assert first == json.loads(observation_json())
    assert first == _fixture()


def test_r001_schema_and_oracle_inventory_are_explicit() -> None:
    bundle = _fixture()
    assert bundle["schema_version"] == SCHEMA_VERSION
    assert bundle["oracle_modules"] == [
        "eggpool.runtime_manager",
        "eggpool.generation_factory",
        "eggpool.config_reload_policy",
        "eggpool.reload_transaction",
        "eggpool.runtime_task_inventory",
        "eggpool.runtime_tasks",
        "eggpool.reload_diagnostics",
        "eggpool.app",
        "eggpool.cli_rehash_helper",
    ]
    for key in (
        "ownership_inventory",
        "lifecycle",
        "reload",
        "config_policy",
        "tasks",
        "active_authority",
        "normalization",
    ):
        assert key in bundle


def test_r001_policy_is_complete_unique_and_fail_closed() -> None:
    policy = _fixture()["config_policy"]
    assert policy["disposition_values"] == ["live", "restart_required", "ignored"]
    assert policy["default_unknown_disposition"] == "restart_required"
    assert policy["unknown_path_probe"] == "restart_required"
    rows = policy["field_dispositions"]
    paths = [row["path"] for row in rows]
    assert len(paths) == len(set(paths))
    assert all(row["disposition"] in policy["disposition_values"] for row in rows)
    assert policy["ignored_paths"] == []
    assert {row["pattern"] for row in policy["dynamic_rules"]} == {
        "providers.<provider_id>",
        "accounts.<provider_id>/<account_name>",
        "model_overrides.<model_id>",
        "model_capabilities.<model_id>",
        "transcoder.<field>",
        "cache.<field>",
        "models.<field>",
    }


def test_r001_reload_cases_preserve_failure_and_acceptance_distinctions() -> None:
    reload_contract = _fixture()["reload"]
    names = {case["name"] for case in reload_contract["cases"]}
    assert names == {
        "identical_digest_noop",
        "valid_live_only_change",
        "restart_required_change",
        "mixed_live_and_restart_required_change",
        "invalid_config",
        "expected_digest_mismatch",
        "stale_caller_generation",
        "candidate_construction_failure",
        "preflight_process_transition_failure",
        "cancellation_before_acceptance",
        "cancellation_during_or_after_acceptance",
        "successful_publication_retirement_pending",
        "successful_publication_already_drained",
    }
    cases = {case["name"]: case for case in reload_contract["cases"]}
    assert cases["identical_digest_noop"]["generation_delta"] == 0
    assert cases["valid_live_only_change"]["publication_occurred"] is True
    assert (
        cases["mixed_live_and_restart_required_change"]["active_generation_unchanged"]
        is True
    )
    assert cases["cancellation_before_acceptance"]["accepted"] is False
    assert cases["cancellation_during_or_after_acceptance"]["accepted"] is True
    assert (
        cases["successful_publication_retirement_pending"]["retirement_pending"] is True
    )
    assert (
        cases["successful_publication_already_drained"]["retirement_pending"] is False
    )


def test_r001_lifecycle_and_task_inventory_cover_required_states() -> None:
    bundle = _fixture()
    lifecycle = bundle["lifecycle"]
    assert lifecycle["candidate"]["states"] == [
        "building",
        "prepared",
        "transferred",
        "aborted",
    ]
    assert lifecycle["generation_slot"]["states"] == [
        "active",
        "retiring",
        "closing",
        "closed",
        "failed_close",
    ]
    assert lifecycle["pending_swap"]["states"] == [
        "prepared",
        "staged",
        "committed",
        "rolled_back",
        "finalized",
    ]
    assert set(lifecycle["transaction"]["states"]) >= {
        "created",
        "validated",
        "diffed",
        "candidate_prepared",
        "commit_started",
        "runtime_staged",
        "runtime_swap_committed",
        "persistence_committed",
        "retirement_scheduled",
        "completed",
        "aborting",
        "aborted",
        "compensation_failed",
    }
    assert lifecycle["candidate"]["abort"]["closed_order"] == [
        "supervisor",
        "outbound_manager",
        "client_pool",
    ]
    tasks = bundle["tasks"]
    assert tasks["names_unique"] is True
    assert len(tasks["inventory"]) == len(RUNTIME_TASK_INVENTORY)
    assert {row["name"] for row in tasks["inventory"]} == {
        "catalog_refresh",
        "retention_cleanup",
        "checkpoint",
        "metrics_flush",
        "update_checker",
        "automatic_backup",
    }


def test_r001_authority_is_generation_safe_and_fixture_is_secret_free() -> None:
    bundle = _fixture()
    authority = bundle["active_authority"]
    assert "client_pool" in authority["generation_owned_app_state_mirrors"]
    assert "router" in authority["generation_owned_app_state_mirrors"]
    assert "server.max_request_body_bytes" in authority["live_request_authority"]
    assert authority["request_generation_rule"].startswith("one generation")
    assert authority["app_state_rule"].startswith("mirrors")

    rendered = observation_json().lower()
    forbidden = (
        "fixture-placeholder-not-retained",
        "authorization: bearer",
        "sk-proj-",
        "sk-ant-",
        "proxy://",
        "password=",
        "token=",
    )
    assert all(marker not in rendered for marker in forbidden)
    assert "<changed>" in rendered
    assert "<redacted>" in rendered
