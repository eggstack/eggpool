"""C001 coordinator contract and deterministic failure-corpus tests."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.errors import DatabaseCommitError
from tests.migration_rs.coordinator_fixtures import (
    DURABLE_ROWS,
    MATRIX_PATH,
    OBSERVATION_PATH,
    build_observation_bundle,
    committed_observation,
    observation_json,
)
from tests.migration_rs.harness import StubHttpServer, StubResponse
from tests.support.database_faults import fail_commit

ROOT = Path(__file__).parents[2]


def _matrix() -> dict[str, object]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_c001_observation_generation_is_repeatable_and_matches_snapshot() -> None:
    first = build_observation_bundle()
    second = build_observation_bundle()
    assert first == second
    assert observation_json() == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )
    assert json.loads(OBSERVATION_PATH.read_text(encoding="utf-8")) == (
        committed_observation(second)
    )


def test_c001_matrix_covers_lifecycle_failure_concurrency_and_restart() -> None:
    matrix = _matrix()
    assert {
        "admission",
        "local_claim",
        "durable_publication",
        "wire_resolution",
        "provider_dispatch",
        "response_handoff",
        "stream_completion",
        "terminal_finalization",
        "restart_reconciliation",
    } <= set(matrix["phases"])
    assert {
        "two_requests_one_claim",
        "duplicate_finalization_callers",
        "leader_cancel_with_follower",
    } <= set(matrix["concurrency_cases"])
    assert {
        "pending_request_no_attempt",
        "terminal_attempt_active_reservation",
    } <= set(matrix["restart_snapshots"])


def test_c001_failure_projection_covers_statuses_and_retry_legality() -> None:
    bundle = build_observation_bundle()
    cases = {case["name"]: case for case in bundle["failure_cases"]}
    assert {
        cases[name]["status_code"] for name in cases if name.startswith("http_")
    } >= {
        400,
        401,
        403,
        404,
        408,
        409,
        429,
        500,
        503,
    }
    assert cases["http_401_ambiguous"]["effects"]["account_effect"] == "none"
    assert cases["http_401_explicit_credential"]["effects"]["account_effect"] == (
        "disable_auth"
    )
    assert cases["http_403_wire_auth_mismatch"]["effects"]["wire_effect"] == (
        "reject_candidate"
    )
    assert cases["post_handoff_500"]["effects"]["retry"] is False
    assert cases["connect_error"]["effects"]["retry_action"] == (
        "other_account_same_wire"
    )


def test_c001_retry_after_and_stream_terminal_facts_are_preserved() -> None:
    bundle = build_observation_bundle()
    assert bundle["retry_after"] == {
        "numeric_seconds": 12.0,
        "http_date": 126230412.0,
        "invalid": None,
        "missing": None,
    }
    streams = {case["name"]: case for case in bundle["streams"]}
    assert streams["openai_terminal"]["eof_classification"] == "complete"
    assert streams["anthropic_terminal"]["snapshot"]["terminal_kind"] == (
        "anthropic_message_stop"
    )
    assert streams["responses_terminal_failure"]["eof_classification"] == (
        "terminal_failure"
    )
    assert streams["responses_terminal_incomplete"]["eof_classification"] == (
        "terminal_incomplete"
    )
    assert streams["empty_eof"]["eof_classification"] == "empty_eof"
    assert streams["compatibility_eof"]["eof_classification"] == "compatibility_eof"
    assert streams["malformed_eof"]["snapshot"]["observer_error_count"] == 1
    assert bundle["synthetic_stream_decisions"] == {
        "gemini_incomplete": "terminal_incomplete",
        "midstream_exception": "upstream_midstream_error",
    }


def test_c001_wire_and_ownership_projection_is_bounded_and_monotonic() -> None:
    bundle = build_observation_bundle()
    wire = bundle["wire"]
    assert wire["leader_role"] == "leader"
    assert wire["follower_role"] == "follower"
    assert wire["leader_result"] == wire["follower_result"] == "accepted"
    assert wire["snapshot"]["inflight"] == 0
    assert wire["rejection_suppressed"] is True
    assert bundle["ownership"]["handoff"] == {
        "before": False,
        "after_repeated_mark": True,
    }
    assert set(bundle["ownership"]["identity"]) >= {
        "proxy_request_id",
        "db_request_id",
        "attempt_id",
        "reservation_id",
        "account_name",
        "provider_id",
        "model_id",
        "attempt_number",
    }


@pytest.mark.asyncio
async def test_c001_durable_row_projection_matches_latest_python_schema(
    tmp_path: Path,
) -> None:
    database = Database(path=str(tmp_path / "c001.sqlite3"))
    await database.connect()
    try:
        await MigrationRunner(database).run()
        for table, required_columns in DURABLE_ROWS.items():
            rows = await database.fetch_all(f'PRAGMA table_info("{table}")')
            columns = {str(row[1]) for row in rows}
            assert set(required_columns) <= columns
    finally:
        await database.disconnect()


@pytest.mark.asyncio
async def test_c001_injected_commit_fault_has_bounded_rollback_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(path=str(tmp_path / "c001-commit-fault.sqlite3"))
    await database.connect()
    try:
        await MigrationRunner(database).run()
        fail_commit(monkeypatch, database, RuntimeError("c001 commit fault"))
        with pytest.raises(DatabaseCommitError) as error:
            async with database.transaction():
                await database.execute_returning("SELECT 1")
        assert error.value.outcome == "rolled_back"
        assert error.value.rollback_succeeded is True
        assert database.writes_admitted is True
    finally:
        await database.disconnect()


def test_c001_local_provider_fixture_records_shape_without_body_retention() -> None:
    with StubHttpServer(
        {
            ("POST", "/v1/chat/completions"): StubResponse(
                status=200, body=b'{"ok":true}'
            )
        }
    ) as server:
        request = urllib.request.Request(
            f"{server.base_url}/v1/chat/completions",
            data=b"bounded-fixture-request",
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2.0) as response:
            assert response.status == 200
            assert response.read() == b'{"ok":true}'

    assert len(server.requests) == 1
    assert server.requests[0].path == "/v1/chat/completions"
    assert server.requests[0].body_length == len(b"bounded-fixture-request")
    assert "authorization" not in server.requests[0].header_names


def test_c001_observations_are_secret_safe() -> None:
    matrix = _matrix()
    rendered = observation_json()
    for marker in matrix["secret_markers_forbidden"]:
        assert marker not in rendered
    assert "bounded-fixture-request" not in rendered
    assert "provider raw error body" not in rendered
    assert str(ROOT / "tests") not in rendered
