"""Evidence guard for the Plan M001 Pi 5 MMC/tmpfs paired benchmark."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
ARTIFACT_DIR = ROOT / "artifacts/qualification/on-prem-concurrency-performance"
MATRIX_PATH = ARTIFACT_DIR / "matrix.json"
TARGET_BOARD = "Raspberry Pi 5 Model B Rev 1.0"
TARGET_ATTESTATION = "Linux aarch64 plus device-tree board model"
RUNS_PER_STORAGE = 3
EXPECTED_CONCURRENCY = 4
EXPECTED_REQUESTS = 32


def _load_run(name: str) -> dict[str, Any]:
    path = ARTIFACT_DIR / name
    assert path.is_file(), f"missing Plan M001 run artifact: {name}"
    return json.loads(path.read_text(encoding="utf-8"))


def _matrix() -> dict[str, Any]:
    assert MATRIX_PATH.is_file(), "missing Plan M001 matrix manifest"
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def test_m001_matrix_manifest_freezes_target_workload_and_thresholds() -> None:
    matrix = _matrix()
    assert matrix["schema_version"] == "on-prem-concurrency-matrix.v1"
    assert matrix["plan"] == "on-prem-concurrency-performance M001"
    assert matrix["thresholds"]["g3_p95_ms"] == 1_000
    assert matrix["thresholds"]["g3_max_ms"] == 3_000
    assert matrix["workload"]["concurrency"] == EXPECTED_CONCURRENCY
    assert matrix["workload"]["request_count_per_run"] == EXPECTED_REQUESTS
    assert matrix["workload"]["sample_count"] == 30
    assert matrix["target"]["board_model"] == TARGET_BOARD
    assert matrix["target"]["architecture"] == "aarch64"
    assert matrix["target"]["filesystem"] == "ext4"
    assert matrix["target"]["attestation"] == TARGET_ATTESTATION
    assert matrix["scope_limits"]["tested_concurrency_levels"] == [4]
    assert matrix["scope_limits"]["tested_named_user_count"] == 1
    assert matrix["scope_limits"]["hard_user_or_global_budgets_enabled"] is False


def test_m001_has_three_same_candidate_runs_per_storage_condition() -> None:
    matrix = _matrix()
    all_runs: list[tuple[str, dict[str, Any]]] = []
    for storage in ("mmc", "tmpfs"):
        runs = matrix["conditions"][storage]["runs"]
        assert len(runs) == RUNS_PER_STORAGE
        assert len(set(runs)) == RUNS_PER_STORAGE
        for name in runs:
            document = _load_run(name)
            assert document["status"] == "pass", name
            assert document["schema_version"] == "runtime-q008.v2", name
            assert document["candidate"]["sha256"] == matrix["candidate"]["sha256"]
            assert document["benchmark"]["config_fixture"] == "sbc-benchmark.toml"
            env = document["environment"]
            assert env["board_model"] == TARGET_BOARD
            assert env["architecture"] == "aarch64"
            assert env["filesystem"] == "ext4"
            assert env["root_storage_device_class"] == "mmc"
            assert env["attestation"] == TARGET_ATTESTATION
            concurrent = document["benchmark"]["runs"][
                "concurrent_native_responses_finite"
            ]
            assert concurrent["status"] == "measured"
            assert concurrent["client_concurrency"] == EXPECTED_CONCURRENCY
            assert concurrent["request_count"] == EXPECTED_REQUESTS
            assert concurrent["completed_count"] == EXPECTED_REQUESTS
            assert concurrent["failed_count"] == 0
            assert document["durable"]["pending_requests"] == 0
            assert document["durable"]["active_reservations"] == 0
            all_runs.append((storage, document))

    assert len({doc["candidate"]["sha256"] for _, doc in all_runs}) == 1
    assert (
        len(
            {
                (
                    doc["candidate"]["repository_commit_sha"],
                    doc["candidate"]["rust_toolchain"],
                )
                for _, doc in all_runs
            }
        )
        == 1
    )


def test_m001_storage_control_reproduces_and_removes_the_g3_tail() -> None:
    matrix = _matrix()
    summaries: dict[str, list[tuple[int, int]]] = {"mmc": [], "tmpfs": []}
    for storage in summaries:
        for name in matrix["conditions"][storage]["runs"]:
            benchmark = _load_run(name)["benchmark"]["runs"]
            concurrent = benchmark["concurrent_native_responses_finite"]["timings"]
            summaries[storage].append(
                (
                    int(concurrent["p95_elapsed_ms"]),
                    int(concurrent["maximum_elapsed_ms"]),
                )
            )

    assert all(p95 > 1_000 and maximum > 3_000 for p95, maximum in summaries["mmc"])
    assert all(p95 <= 1_000 and maximum <= 3_000 for p95, maximum in summaries["tmpfs"])


def test_m001_reports_are_redacted_and_have_no_p99_claim() -> None:
    matrix = _matrix()
    for storage in ("mmc", "tmpfs"):
        for name in matrix["conditions"][storage]["runs"]:
            content = (ARTIFACT_DIR / name).read_text(encoding="utf-8")
            assert "SBC synthetic bootstrap password" not in content
            assert "epk1_" not in content
            document = json.loads(content)
            assert (
                "p99_elapsed_ms"
                not in document["benchmark"]["runs"][
                    "concurrent_native_responses_finite"
                ]["timings"]
            )
