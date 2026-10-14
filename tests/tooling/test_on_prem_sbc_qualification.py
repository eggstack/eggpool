"""M012 contracts: the physical-SBC runner binds to an attested target,
records actual concurrency/completion only, and never fabricates the
1/5/10/25/49 named-user matrix or a small-team capacity claim.

The runner covers a single-admin loopback corpus plus a fixed
contention observation; the named-user matrix in
`plans/implementation/on-prem-user-access/012-physical-sbc-small-team-qualification.md`
remains unimplemented until provisioned hardware exists. These tests
pin that boundary so a blocked off-target run can never be mistaken
for measured evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.qualification_sbc import (
    BENCHMARK_FIXTURE,
    CONCURRENCY_BATCH_SIZE,
    CONCURRENCY_WORKERS,
    DEFAULT_FIXTURE,
    MANIFEST_VERSION,
    SCHEMA_V1,
    SCHEMA_VERSION,
    board_metadata,
    main,
    run_qualification,
)


def test_m012_target_gate_requires_attested_physical_sbc() -> None:
    board, reason = board_metadata()
    if board is None:
        assert reason, "gate must name the missing evidence"
        lowered = reason.lower()
        assert any(
            token in lowered
            for token in ("linux", "aarch64", "device-tree", "physical")
        )
    else:
        assert board["board_model"]
        assert board["attestation"] == "Linux aarch64 plus device-tree board model"
        assert board["architecture"] in {"aarch64", "arm64"}


def test_m012_off_target_run_is_blocked_before_measurement(
    tmp_path: Path,
) -> None:
    board, _ = board_metadata()
    if board is not None:
        pytest.skip("physical SBC target: gate passes, nothing to assert here")
    report = run_qualification(binary=Path("/not/a/candidate"))
    assert report["status"] == "blocked"
    assert "candidate" not in report
    assert "benchmark" not in report
    output = tmp_path / "m012.json"
    exit_code = main(["--binary", "/not/a/candidate", "--output", str(output)])
    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "blocked"


def test_m012_blocked_report_carries_manifest_and_bounded_environment() -> None:
    board, _ = board_metadata()
    if board is not None:
        pytest.skip("physical SBC target: gate passes, nothing to assert here")
    report = run_qualification(binary=Path("/not/a/candidate"))
    assert report["schema_version"] == SCHEMA_V1
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["manifest"] == MANIFEST_VERSION
    assert report["plan"] == "SBC qualification"
    assert set(report["environment"]) == {"system", "architecture"}


def test_m012_blocked_report_is_secret_free_and_bounded() -> None:
    board, _ = board_metadata()
    if board is not None:
        pytest.skip("physical SBC target: gate passes, nothing to assert here")
    report = run_qualification(binary=Path("/not/a/candidate"))
    payload = json.dumps(report, sort_keys=True)
    assert "q008-provider-key" not in payload
    assert "q008-server-key" not in payload
    assert "http://" not in payload
    assert "https://" not in payload
    assert len(payload.encode()) < 2048


def test_m012_runner_records_actual_concurrency_not_named_users() -> None:
    # Fixed contention observation: four clients, 32 requests. This is an
    # actual-concurrency measurement, never a named-user count.
    assert CONCURRENCY_WORKERS == 4
    assert CONCURRENCY_BATCH_SIZE == 32
    report = run_qualification(binary=Path("/not/a/candidate"))
    payload = json.dumps(report)
    for claim in (
        "named_users",
        "named-users",
        "user_count",
        "capacity",
        "users_per_second",
    ):
        assert claim not in payload


def test_m012_no_named_user_or_concurrency_matrix_flags() -> None:
    from scripts.qualification_sbc import _parser

    options = {
        option for action in _parser()._actions for option in action.option_strings
    }
    for forbidden in (
        "--named-users",
        "--users",
        "--concurrency",
        "--load-level",
        "--user-matrix",
    ):
        assert forbidden not in options


def test_m012_benchmark_corpus_requires_benchmark_fixture() -> None:
    with pytest.raises(ValueError, match="benchmark corpus"):
        run_qualification(binary=Path("/not/a/candidate"), benchmark_samples=30)


def test_m012_control_candidate_modes_are_explicit_and_exclusive(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="publication phase"):
        run_qualification(
            binary=Path("/not/a/candidate"),
            qualification_dedicated_checkpointer_mode="control",
        )
    with pytest.raises(ValueError, match="mutually exclusive"):
        run_qualification(
            binary=Path("/not/a/candidate"),
            config_fixture=BENCHMARK_FIXTURE,
            diagnose_publication_phases=True,
            qualification_dedicated_checkpointer_mode="control",
            qualification_persist_journal_mode="control",
            diagnostic_database_dir=tmp_path,
        )


def test_m012_sbc_fixtures_carry_no_user_identity_matrix() -> None:
    for fixture in (DEFAULT_FIXTURE, BENCHMARK_FIXTURE):
        text = fixture.read_text(encoding="utf-8")
        assert "[users]" not in text
        assert "personal_token" not in text
        assert "password" not in text
        assert "argon" not in text.lower()
        assert "__SBC_UPSTREAM__" in text
        assert "https://" not in text
