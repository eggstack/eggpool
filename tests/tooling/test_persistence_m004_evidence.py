"""Data-derived evidence guard for the committed persistence M004 artifacts.

Persistence M005 corrective pass. The M004 closure narrated cumulative
checkpoint counters as if they were activity inside the measured request
batch. This guard reads the committed ``artifacts/qualification/m004/*.json``
corpus and pins, from the artifacts themselves:

- the accepted-artifact census and phase/ordinary split;
- candidate identity, target class, and recorded status;
- measured-window checkpoint activity from the ``deltas`` fields only;
- the request/COMMIT acceptance-gate failures that support the periodic
  strategy rejection;
- ordinary-benchmark durable convergence and lifecycle checks.

Cumulative ``baseline``/``final`` snapshots are compared against ``deltas`` on
purpose: that comparison is the defect M005 corrects, so a future reader who
narrows a cumulative counter as in-batch activity fails this guard. The test
deliberately encodes no documentation wording, needs no network or hardware,
and adds no runtime dependency.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
ARTIFACT_DIR = ROOT / "artifacts/qualification/m004"

TARGET_BOARD_MODEL = "Raspberry Pi 5 Model B Rev 1.0"
TARGET_ARCHITECTURE = "aarch64"
TARGET_FILESYSTEM = "ext4"
TARGET_STORAGE_CLASS = "mmc"
TARGET_ATTESTATION = "Linux aarch64 plus device-tree board model"

EXPECTED_ACCEPTED_ARTIFACTS = 14
EXPECTED_PHASE_ARTIFACTS = 11
EXPECTED_ORDINARY_ARTIFACTS = 3

# (poll interval seconds, soft WAL-frame threshold) -> accepted artifact count.
EXPECTED_PHASE_CENSUS: dict[tuple[float, int], int] = {
    (60.0, 256): 3,
    (60.0, 128): 3,
    (60.0, 64): 3,
    (30.0, 64): 1,
    (1.0, 64): 1,
}
# (poll interval seconds, soft WAL-frame threshold) -> accepted artifact name.
EXPECTED_PHASE_NAMES: dict[tuple[float, int], str] = {
    (60.0, 256): "256-60s-run-1.json",
    (60.0, 128): "128-60s-run-1.json",
    (60.0, 64): "64-60s-run-1.json",
    (30.0, 64): "64-30s-run-1.json",
    (1.0, 64): "64-1s-run-1.json",
}

# M004 plan section 13 acceptance gates.
P95_TOTAL_LATENCY_GATE_MS = 100
MAX_TOTAL_LATENCY_GATE_MS = 500
FOREGROUND_COMMIT_GATE_US = 50_000

# Cumulative maintenance counters and their measured-window delta names.
MAINTENANCE_COUNTERS = (
    "not_due",
    "gate_busy",
    "below_threshold",
    "checkpointed",
    "failures",
)

PHASE_DIAGNOSTIC_MODE = "publication_commit_checkpoint_phase"
MINIMUM_TESTED_CADENCE_S = 1.0
MINIMUM_TESTED_FRAMES = 64
LIFECYCLE_FUNCTIONAL_IDS = (
    "backup-recover-isolated",
    "bounded-maintenance",
    "rehash-runtime-status",
    "restart-reconcile",
    "graceful-shutdown",
)
EXPECTED_FUNCTIONAL_IDS = 24


def _load(name: str) -> dict[str, Any]:
    path = ARTIFACT_DIR / name
    assert path.is_file(), f"missing committed M004 artifact: {name}"
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _accepted_names() -> list[str]:
    return sorted(path.name for path in ARTIFACT_DIR.glob("*.json"))


def _is_phase(document: dict[str, Any]) -> bool:
    return document["benchmark"].get("diagnostic_mode") == PHASE_DIAGNOSTIC_MODE


def _phase_documents() -> dict[str, dict[str, Any]]:
    documents = {name: _load(name) for name in _accepted_names()}
    return {
        name: document for name, document in documents.items() if _is_phase(document)
    }


def _phase_candidate(document: dict[str, Any]) -> tuple[float, int]:
    tuning = document["benchmark"]["checkpoint_tuning"]
    return (float(tuning["poll_interval_s"]), int(tuning["soft_threshold_frames"]))


def _maintenance(document: dict[str, Any], name: str) -> dict[str, Any]:
    maintenance = document["benchmark"].get("checkpoint_maintenance")
    assert isinstance(maintenance, dict), (
        f"{name}: missing benchmark.checkpoint_maintenance; the committed artifact "
        "cannot separate cumulative counters from measured-window deltas"
    )
    for key in ("baseline", "deltas", "final"):
        assert key in maintenance, f"{name}: benchmark.checkpoint_maintenance.{key}"
    return maintenance


def _checkpoint_tick_delta(document: dict[str, Any], name: str) -> int:
    quiescence = document["benchmark"].get("task_quiescence")
    assert isinstance(quiescence, dict), f"{name}: missing benchmark.task_quiescence"
    return int(quiescence["deltas"]["checkpoint"]["tick_count_delta"])


def _in_batch_maintenance(document: dict[str, Any], name: str) -> dict[str, int]:
    """Measured-window maintenance activity, read only from ``deltas``."""
    deltas = _maintenance(document, name)["deltas"]
    return {
        counter: int(deltas[f"{counter}_delta"]) for counter in MAINTENANCE_COUNTERS
    }


def _cumulative_as_in_batch(document: dict[str, Any], name: str) -> dict[str, int]:
    """The M004 narration error: cumulative ``final`` read as in-batch activity."""
    final = _maintenance(document, name)["final"]
    return {counter: int(final[counter]) for counter in MAINTENANCE_COUNTERS}


def _phase_diagnostic(document: dict[str, Any], name: str) -> dict[str, Any]:
    diagnostic = document["benchmark"]["runs"].get("publication_phase_diagnostic")
    assert isinstance(diagnostic, dict), (
        f"{name}: missing runs.publication_phase_diagnostic"
    )
    return diagnostic


def _documented_gate_failures(diagnostic: dict[str, Any], name: str) -> list[str]:
    failures: list[str] = []
    if int(diagnostic["p95_total_ms"]) >= P95_TOTAL_LATENCY_GATE_MS:
        failures.append(
            f"p95 total {diagnostic['p95_total_ms']} ms is at or above the "
            f"{P95_TOTAL_LATENCY_GATE_MS} ms gate"
        )
    if int(diagnostic["maximum_total_ms"]) >= MAX_TOTAL_LATENCY_GATE_MS:
        failures.append(
            f"maximum total {diagnostic['maximum_total_ms']} ms >= "
            f"{MAX_TOTAL_LATENCY_GATE_MS} ms"
        )
    for phase in ("publication_phase_summary", "finalization_phase_summary"):
        commit_us = int(diagnostic[phase]["maximum_commit_us"])
        if commit_us >= FOREGROUND_COMMIT_GATE_US:
            failures.append(
                f"{phase} COMMIT {commit_us} us >= {FOREGROUND_COMMIT_GATE_US} us"
            )
    assert failures, f"{name}: expected at least one M004 acceptance-gate failure"
    return failures


def test_m004_committed_census_is_fourteen_accepted_artifacts() -> None:
    names = _accepted_names()
    assert len(names) == EXPECTED_ACCEPTED_ARTIFACTS, (
        f"artifacts/qualification/m004/ holds {len(names)} committed artifacts, "
        f"expected {EXPECTED_ACCEPTED_ARTIFACTS}: {names}"
    )
    for name in names:
        document = _load(name)
        assert document["status"] == "pass", (
            f"{name}: status is {document['status']!r}, expected 'pass'"
        )


def test_m004_census_splits_eleven_phase_and_three_ordinary_artifacts() -> None:
    phase = _phase_documents()
    ordinary = [name for name in _accepted_names() if not _is_phase(_load(name))]
    assert len(phase) == EXPECTED_PHASE_ARTIFACTS, (
        f"expected {EXPECTED_PHASE_ARTIFACTS} phase-diagnostic artifacts, found "
        f"{len(phase)}: {sorted(phase)}"
    )
    assert len(ordinary) == EXPECTED_ORDINARY_ARTIFACTS, (
        f"expected {EXPECTED_ORDINARY_ARTIFACTS} ordinary benchmark artifacts, found "
        f"{len(ordinary)}: {ordinary}"
    )
    for name in ordinary:
        document = _load(name)
        assert "diagnostic_mode" not in document["benchmark"], (
            f"{name}: ordinary benchmark artifact must not claim a diagnostic mode"
        )
        assert document["benchmark"]["sample_count"] == 10, (
            f"{name}: ordinary benchmark sample_count is "
            f"{document['benchmark']['sample_count']}, expected 10"
        )


def test_m004_phase_candidate_census_matches_the_recorded_matrix() -> None:
    observed: dict[tuple[float, int], list[str]] = {}
    for name, document in _phase_documents().items():
        observed.setdefault(_phase_candidate(document), []).append(name)
    assert {key: len(value) for key, value in sorted(observed.items())} == (
        EXPECTED_PHASE_CENSUS
    ), (
        "phase candidate census mismatch: expected "
        f"{dict(sorted(EXPECTED_PHASE_CENSUS.items()))}, observed "
        f"{ {key: sorted(value) for key, value in sorted(observed.items())} }"
    )
    for candidate, expected_name in EXPECTED_PHASE_NAMES.items():
        assert expected_name in observed[candidate], (
            f"candidate {candidate} should include {expected_name}, found "
            f"{sorted(observed[candidate])}"
        )


def test_m004_artifacts_attest_the_raspberry_pi_5_aarch64_ext4_mmc_target() -> None:
    for name in _accepted_names():
        environment = _load(name)["environment"]
        assert environment["board_model"] == TARGET_BOARD_MODEL, f"{name}: board_model"
        assert environment["architecture"] == TARGET_ARCHITECTURE, (
            f"{name}: architecture"
        )
        assert environment["filesystem"] == TARGET_FILESYSTEM, f"{name}: filesystem"
        assert environment["root_storage_device_class"] == TARGET_STORAGE_CLASS, (
            f"{name}: root_storage_device_class"
        )
        assert environment["attestation"] == TARGET_ATTESTATION, f"{name}: attestation"
        assert environment["storage_medium_class"] == "non-rotational", (
            f"{name}: storage_medium_class"
        )


def test_m004_phase_artifacts_keep_wal_normal_and_the_automatic_ceiling() -> None:
    for name, document in _phase_documents().items():
        effective = document["benchmark"]["effective"]
        assert effective["journal_mode"] == "wal", (
            f"{name}: benchmark.effective.journal_mode"
        )
        assert effective["synchronous"] == "NORMAL", (
            f"{name}: benchmark.effective.synchronous"
        )
        assert effective["wal_autocheckpoint_pages"] == 1000, (
            f"{name}: benchmark.effective.wal_autocheckpoint_pages"
        )
        assert document["benchmark"]["wal_autocheckpoint_override_pages"] is None, (
            f"{name}: automatic-checkpoint ceiling was overridden during the run"
        )


def test_long_cadence_phase_candidates_ran_no_checkpoint_tick_in_the_batch() -> None:
    long_cadence = [
        (name, document)
        for name, document in _phase_documents().items()
        if _phase_candidate(document)[0] >= 30.0
    ]
    assert len(long_cadence) == 10, (
        f"expected 10 long-cadence (30 s/60 s) phase artifacts, found "
        f"{len(long_cadence)}"
    )
    for name, document in long_cadence:
        tick_delta = _checkpoint_tick_delta(document, name)
        assert tick_delta == 0, (
            f"{name}: task_quiescence.deltas.checkpoint.tick_count_delta is "
            f"{tick_delta}, expected 0 checkpoint task ticks in the measured batch"
        )
        activity = _in_batch_maintenance(document, name)
        for counter, value in activity.items():
            assert value == 0, (
                f"{name}: checkpoint_maintenance.deltas.{counter}_delta is {value}, "
                "expected 0 measured-window maintenance actions"
            )


def test_minimum_cadence_stress_defers_every_in_window_checkpoint() -> None:
    name = EXPECTED_PHASE_NAMES[(MINIMUM_TESTED_CADENCE_S, MINIMUM_TESTED_FRAMES)]
    document = _load(name)
    assert _checkpoint_tick_delta(document, name) == 3, (
        f"{name}: expected 3 in-batch checkpoint task ticks, found "
        f"{_checkpoint_tick_delta(document, name)}"
    )
    assert _in_batch_maintenance(document, name) == {
        "not_due": 0,
        "gate_busy": 3,
        "below_threshold": 0,
        "checkpointed": 0,
        "failures": 0,
    }, (
        f"{name}: every in-window checkpoint opportunity must defer on the foreground "
        f"gate, found {_in_batch_maintenance(document, name)}"
    )


def test_no_phase_candidate_completed_an_in_batch_maintenance_checkpoint() -> None:
    for name, document in _phase_documents().items():
        assert _in_batch_maintenance(document, name)["checkpointed"] == 0, (
            f"{name}: checkpoint_maintenance.deltas.checkpointed_delta is non-zero; "
            "the rejection argument assumes no in-batch PASSIVE checkpoint"
        )


def test_cumulative_snapshots_are_not_measured_window_deltas() -> None:
    for name, document in _phase_documents().items():
        maintenance = _maintenance(document, name)
        baseline = maintenance["baseline"]
        deltas = maintenance["deltas"]
        final = maintenance["final"]
        for counter in MAINTENANCE_COUNTERS:
            expected_final = int(baseline[counter]) + int(deltas[f"{counter}_delta"])
            assert int(final[counter]) == expected_final, (
                f"{name}: checkpoint_maintenance.{counter} cumulative identity broken: "
                f"baseline {baseline[counter]} + delta {deltas[f'{counter}_delta']} != "
                f"final {final[counter]}"
            )
        tick_baseline = document["benchmark"]["task_quiescence"]["baseline"][
            "checkpoint"
        ]
        tick_final = document["benchmark"]["task_quiescence"]["final"]["checkpoint"]
        assert int(tick_final["tick_count"]) == int(tick_baseline["tick_count"]) + (
            _checkpoint_tick_delta(document, name)
        ), f"{name}: checkpoint tick cumulative identity broken"

    stress = EXPECTED_PHASE_NAMES[(MINIMUM_TESTED_CADENCE_S, MINIMUM_TESTED_FRAMES)]
    stress_document = _load(stress)
    narration = _cumulative_as_in_batch(stress_document, stress)
    truth = _in_batch_maintenance(stress_document, stress)
    assert narration["checkpointed"] == 2 and narration["below_threshold"] == 1, (
        f"{stress}: the committed cumulative snapshot should still contain pre-batch "
        f"history, found {narration}"
    )
    assert truth["checkpointed"] == 0 and truth["below_threshold"] == 0, (
        f"{stress}: in-batch deltas must not inherit pre-batch cumulative history"
    )
    assert narration != truth, (
        f"{stress}: cumulative narration ({narration}) must differ from the "
        f"measured-window deltas ({truth}) so this guard detects substitution"
    )

    for name, document in _phase_documents().items():
        if _phase_candidate(document)[0] < 30.0:
            continue
        assert _cumulative_as_in_batch(document, name)["below_threshold"] == 1, (
            f"{name}: long-cadence cumulative snapshot should record one pre-batch "
            "below-threshold observation"
        )
        assert _in_batch_maintenance(document, name)["below_threshold"] == 0, (
            f"{name}: that pre-batch observation is not in-batch maintenance work"
        )


def test_current_candidate_60s_256_fails_m004_acceptance_gates() -> None:
    names = sorted(
        name
        for name, document in _phase_documents().items()
        if _phase_candidate(document) == (60.0, 256)
    )
    assert len(names) == 3, f"expected three accepted 60s/256 runs, found {names}"
    for name in names:
        diagnostic = _phase_diagnostic(_load(name), name)
        assert int(diagnostic["sample_count"]) == 60, f"{name}: phase sample_count"
        assert int(diagnostic["completed_count"]) == 60, f"{name}: completed_count"
        assert int(diagnostic["failed_count"]) == 0, f"{name}: failed_count"
        assert int(diagnostic["foreground_record_count"]) == 120, (
            f"{name}: foreground_record_count"
        )
        assert _documented_gate_failures(diagnostic, name), (
            f"{name}: the landed 60s/256 candidate must fail at least one M004 gate"
        )


def test_bounded_matrix_and_stress_candidates_also_fail_the_gates() -> None:
    for candidate, expected_count in EXPECTED_PHASE_CENSUS.items():
        names = [
            name
            for name, document in _phase_documents().items()
            if _phase_candidate(document) == candidate
        ]
        assert len(names) == expected_count, f"{candidate}: {names}"
        for name in names:
            _documented_gate_failures(_phase_diagnostic(_load(name), name), name)


def test_minimum_cadence_stress_still_fails_request_and_commit_gates() -> None:
    name = EXPECTED_PHASE_NAMES[(MINIMUM_TESTED_CADENCE_S, MINIMUM_TESTED_FRAMES)]
    diagnostic = _phase_diagnostic(_load(name), name)
    assert int(diagnostic["maximum_total_ms"]) >= MAX_TOTAL_LATENCY_GATE_MS, (
        f"{name}: minimum-cadence stress run must still miss the maximum-request gate"
    )
    publication = diagnostic["publication_phase_summary"]
    assert int(publication["maximum_commit_us"]) >= FOREGROUND_COMMIT_GATE_US, (
        f"{name}: minimum-cadence stress run must still miss the foreground COMMIT gate"
    )
    assert int(diagnostic["maximum_total_ms_with_checkpoint_sequence_change"]) == int(
        diagnostic["maximum_total_ms"]
    ), (
        f"{name}: the slowest request must be the WAL checkpoint sequence change "
        "request for the periodic rejection to hold"
    )
    assert int(diagnostic["maximum_total_ms_without_checkpoint_sequence_change"]) < (
        MAX_TOTAL_LATENCY_GATE_MS
    ), (
        f"{name}: requests without a WAL checkpoint sequence change stay inside the "
        "gate, localizing the tail to foreground automatic-checkpoint work"
    )


def test_foreground_commit_owns_the_wal_checkpoint_tail_in_every_phase_run() -> None:
    for name, document in _phase_documents().items():
        diagnostic = _phase_diagnostic(document, name)
        assert int(diagnostic["wal_checkpoint_sequence_change_count"]) == 3, (
            f"{name}: wal_checkpoint_sequence_change_count"
        )
        assert int(diagnostic["wal_checkpoint_sequence_unchanged_count"]) == 57, (
            f"{name}: wal_checkpoint_sequence_unchanged_count"
        )
        correlated = sorted(
            diagnostic["slowest_five_correlated"],
            key=lambda entry: int(entry["total_ms"]),
            reverse=True,
        )
        slowest = correlated[0]
        assert slowest["wal_checkpoint_sequence_changed"] is True, (
            f"{name}: slowest request {slowest['request_sequence']} must cross a WAL "
            "checkpoint sequence boundary"
        )
        assert int(slowest["publication"]["commit_us"]) >= FOREGROUND_COMMIT_GATE_US, (
            f"{name}: slowest request publication COMMIT is "
            f"{slowest['publication']['commit_us']} us"
        )
        assert int(slowest["publication"]["gate_wait_us"]) < (
            FOREGROUND_COMMIT_GATE_US
        ), (
            f"{name}: foreground gate-wait must stay small; a large value would mean "
            "requests queued behind maintenance work"
        )
        assert int(slowest["pre_provider_ms"]) * 2 >= int(slowest["total_ms"]), (
            f"{name}: the slowest request must be pre-provider dominated, so the "
            "tail is database-owned rather than provider-owned"
        )


def test_maintenance_failures_are_zero_and_provider_control_stays_ordinary() -> None:
    for name, document in _phase_documents().items():
        activity = _in_batch_maintenance(document, name)
        assert activity["failures"] == 0, f"{name}: in-batch maintenance failures"
        assert _maintenance(document, name)["final"]["failures"] == 0, (
            f"{name}: cumulative maintenance failures"
        )
        assert int(document["benchmark"]["sample_count"]) == 60, (
            f"{name}: phase sample_count"
        )
        control = document["benchmark"]["runs"]["direct_provider_control"]
        assert int(control["failed_count"]) == 0, f"{name}: direct control failures"
        assert int(control["p50_total_ms"]) <= 5, (
            f"{name}: direct-provider control p50 is {control['p50_total_ms']} ms; a "
            "larger control would move the tail ownership question"
        )


def test_phase_runs_converge_with_no_pending_work() -> None:
    for name, document in _phase_documents().items():
        durable = document["durable"]
        assert int(durable["pending_requests"]) == 0, (
            f"{name}: durable.pending_requests"
        )
        assert int(durable["active_reservations"]) == 0, (
            f"{name}: durable.active_reservations"
        )
        assert durable["request_statuses"] == {"completed": int(durable["requests"])}, (
            f"{name}: durable.request_statuses"
        )
        assert document["backup"]["member_count"] > 0, f"{name}: backup archive members"


def test_ordinary_benchmarks_converge_and_keep_lifecycle_checks_green() -> None:
    for name in _accepted_names():
        document = _load(name)
        if _is_phase(document):
            continue
        summary = document["benchmark"]["resource_summary"]
        assert int(summary["final_pending_requests"]) == 0, f"{name}: pending requests"
        assert int(summary["final_active_reservations"]) == 0, (
            f"{name}: active reservations"
        )
        assert int(summary["final_finalization_jobs"]) == 0, (
            f"{name}: finalization jobs"
        )
        assert int(document["durable"]["requests"]) == 85, f"{name}: durable.requests"
        assert int(document["durable"]["attempts"]) == 85, f"{name}: durable.attempts"
        functional = {entry["id"]: entry["status"] for entry in document["functional"]}
        assert len(functional) == EXPECTED_FUNCTIONAL_IDS, (
            f"{name}: expected {EXPECTED_FUNCTIONAL_IDS} functional ids, found "
            f"{len(functional)}"
        )
        for lifecycle_id in LIFECYCLE_FUNCTIONAL_IDS:
            assert functional.get(lifecycle_id) == "pass", (
                f"{name}: functional check {lifecycle_id} is "
                f"{functional.get(lifecycle_id)!r}"
            )
        stability = document["repeated_run_stability"]
        assert stability["logical_leaks"] is False, f"{name}: logical leaks"
        assert set(stability["fd_counts"]) == {14}, f"{name}: open fd counts"
        assert set(stability["thread_counts"]) == {2}, f"{name}: thread counts"
        assert document["isolated_temporary_root"] is True, (
            f"{name}: the runner always isolates config/data-home; the target-class "
            "claim rests on the ext4/MMC attestation, not on this flag"
        )
        assert document["python_reference"]["status"] == "not-run", (
            f"{name}: a Python reference result is not target-class evidence"
        )


def test_ordinary_candidate_differs_from_the_diagnostic_candidate() -> None:
    documents = {name: _load(name) for name in _accepted_names()}
    shas = {
        name: document["candidate"]["sha256"]
        for name, document in documents.items()
        if not _is_phase(document)
    }
    stress_name = EXPECTED_PHASE_NAMES[
        (MINIMUM_TESTED_CADENCE_S, MINIMUM_TESTED_FRAMES)
    ]
    stress_sha = documents[stress_name]["candidate"]["sha256"]
    assert len(set(shas.values())) == 1, (
        f"ordinary benchmark runs used inconsistent candidates: {shas}"
    )
    assert stress_sha not in set(shas.values()), (
        "the 1s/64 stress run must use the qualification-db-diagnostics "
        f"candidate, not the ordinary release candidate used by the runs ({shas})"
    )
