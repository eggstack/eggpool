"""Run and summarize the frozen M002 ordinary-server qualification corpus.

The matrix separates named-account metadata from simultaneous requests. Each
cell gets a fresh Q008 root and three independent runs; the tool writes the
manifest before running any cell and only emits aggregate scalar summaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

try:
    from scripts.qualification_sbc import (
        ORDINARY_SERVER_MAX_INITIAL_IO_PRESSURE_PERCENT,
        host_io_full_avg10,
        host_io_snapshot,
        ordinary_server_metadata,
    )
except ModuleNotFoundError:  # Running this file directly from ``scripts/``.
    from qualification_sbc import (  # type: ignore[no-redef]
        ORDINARY_SERVER_MAX_INITIAL_IO_PRESSURE_PERCENT,
        host_io_full_avg10,
        host_io_snapshot,
        ordinary_server_metadata,
    )

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    ROOT / "artifacts/qualification/on-prem-concurrency-performance/ordinary-server"
)
SCHEMA_VERSION = "on-prem-ordinary-server-matrix.v1"
SUMMARY_VERSION = "on-prem-ordinary-server-summary.v1"
LIFECYCLE_ARTIFACT = "lifecycle.json"
REPETITIONS = 3
REQUESTS_PER_CELL = 1000
SEQUENTIAL_SAMPLES = 30
PRINCIPAL_CELLS = (1, 5, 10, 25, 49)
CONCURRENCY_CELLS = (1, 2, 4, 8, 16)
PROVIDER_LATENCY_PROFILE = "mixed-0-10-50ms"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else "unavailable"


def _conditions() -> list[dict[str, int]]:
    pairs = {(users, 1) for users in PRINCIPAL_CELLS}
    pairs.update((5, concurrency) for concurrency in CONCURRENCY_CELLS)
    return [
        {"principals": principals, "concurrency": concurrency}
        for principals, concurrency in sorted(pairs)
    ]


def _io_pressure_safe(snapshot: dict[str, Any]) -> bool:
    avg10 = host_io_full_avg10(snapshot)
    return avg10 is not None and avg10 < ORDINARY_SERVER_MAX_INITIAL_IO_PRESSURE_PERCENT


def _manifest(binary: Path, output_dir: Path) -> dict[str, Any]:
    target, reason = ordinary_server_metadata()
    if reason or target is None:
        raise RuntimeError(reason or "ordinary-server host facts unavailable")
    if (
        target["root_filesystem"] in {"tmpfs", "ramfs", "overlay"}
        or target["root_storage_medium_class"] != "non-rotational"
        or target["root_storage_device_class"] not in {"nvme", "scsi-disk"}
    ):
        raise RuntimeError("ordinary-server host root must use persistent SSD/NVMe")
    initial_io = host_io_snapshot()
    io_safe = _io_pressure_safe(initial_io)
    return {
        "schema_version": SCHEMA_VERSION,
        "plan": "on-prem-concurrency-performance M002",
        "status": "ready" if io_safe else "blocked",
        "preflight_io": initial_io,
        "stop_reason": (
            None
            if io_safe
            else (
                "initial full I/O pressure avg10 is missing or exceeds "
                "the 25% stop gate"
            )
        ),
        "candidate": {
            "sha256": _sha256(binary),
            "repository_commit_sha": _commit(),
            "build_profile": "release",
            "build_mode": "locked release candidate",
        },
        "qualification_tooling_commit_sha": _commit(),
        "target": target,
        "workload": {
            "fresh_root_per_run": True,
            "named_principal_counts": list(PRINCIPAL_CELLS),
            "principal_separation_condition": "concurrency=1",
            "concurrent_request_counts": list(CONCURRENCY_CELLS),
            "concurrency_separation_condition": "principals=5",
            "requests_per_condition": REQUESTS_PER_CELL,
            "repetitions_per_condition": REPETITIONS,
            "sequential_wire_samples_per_surface": SEQUENTIAL_SAMPLES,
            "provider_latency_profile": PROVIDER_LATENCY_PROFILE,
            "budget_mode": "user-global hard request limits",
            "surfaces": [
                "native Responses finite",
                "native Responses streaming",
                "translated Responses-to-Messages streaming",
            ],
        },
        "gates": {
            "all_requests_complete": True,
            "zero_http_failures": True,
            "zero_pending_requests_after_stabilization": True,
            "zero_active_reservations_after_stabilization": True,
            "p99_minimum_completed_observations": 1000,
            "three_repetitions_required": True,
            "maximum_initial_io_pressure_full_avg10_percent": (
                ORDINARY_SERVER_MAX_INITIAL_IO_PRESSURE_PERCENT
            ),
            "no_capacity_sla_claim": True,
        },
        "conditions": [
            {
                **condition,
                "run_artifacts": [
                    f"u{condition['principals']}-c{condition['concurrency']}-run-{run}.json"
                    for run in range(1, REPETITIONS + 1)
                ],
            }
            for condition in _conditions()
        ],
        "artifacts_directory": output_dir.name,
    }


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"artifact is not an object: {path.name}")
    return cast("dict[str, Any]", value)


def summarize(manifest_path: Path) -> dict[str, Any]:
    """Validate every raw cell against the frozen gates and aggregate it."""
    manifest = _load(manifest_path)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("ordinary-server manifest schema is unsupported")
    if manifest.get("status") != "complete":
        raise ValueError("ordinary-server matrix is incomplete")
    candidate = manifest.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("candidate identity is missing")
    candidate = cast("dict[str, Any]", candidate)
    lifecycle_name = manifest.get("lifecycle_artifact")
    if lifecycle_name != LIFECYCLE_ARTIFACT:
        raise ValueError("ordinary-server lifecycle artifact is missing")
    lifecycle = _load(manifest_path.parent / LIFECYCLE_ARTIFACT)
    if (
        lifecycle.get("status") != "pass"
        or lifecycle.get("schema_version") != "runtime-q009.v1"
        or not isinstance(lifecycle.get("candidate"), dict)
        or lifecycle["candidate"].get("sha256") != candidate.get("sha256")
    ):
        raise ValueError("ordinary-server lifecycle qualification did not pass")
    lifecycle_phases = lifecycle.get("phases")
    if not isinstance(lifecycle_phases, list):
        raise ValueError("ordinary-server lifecycle phases are missing")
    lifecycle_phases = cast("list[Any]", lifecycle_phases)
    required_phases = {"faults", "restart"}
    observed_phases: set[str] = set()
    for raw_phase in lifecycle_phases:
        if not isinstance(raw_phase, dict):
            continue
        phase = cast("dict[str, Any]", raw_phase)
        name = phase.get("phase")
        if isinstance(name, str) and phase.get("status") == "pass":
            observed_phases.add(name)
    if not required_phases <= observed_phases:
        raise ValueError("ordinary-server lifecycle phases are incomplete")
    phase_by_name: dict[str, dict[str, Any]] = {}
    for raw_phase in lifecycle_phases:
        if not isinstance(raw_phase, dict):
            continue
        phase = cast("dict[str, Any]", raw_phase)
        name = phase.get("phase")
        if isinstance(name, str):
            phase_by_name[name] = phase
    restart_phase = phase_by_name.get("restart", {})
    workload_policy = lifecycle.get("workload_policy")
    if not isinstance(workload_policy, dict):
        raise ValueError("ordinary-server lifecycle workload policy is missing")
    workload_policy = cast("dict[str, Any]", workload_policy)
    if (
        workload_policy.get("budget_mode") != "hard user and global request ceilings"
        or workload_policy.get("principal_count") != 2
        or restart_phase.get("hard_budgets_active") is not True
        or restart_phase.get("user_disabled_during_load") is not True
        or restart_phase.get("credential_revocation_during_load") is not True
        or restart_phase.get("identity_denial_http_status") not in {401, 403}
    ):
        raise ValueError("ordinary-server managed recovery evidence is incomplete")
    fault_phase = phase_by_name.get("faults", {})
    raw_observations = fault_phase.get("observations")
    if not isinstance(raw_observations, list):
        raise ValueError("ordinary-server fault observations are missing")
    raw_observations = cast("list[Any]", raw_observations)
    observations: list[dict[str, Any]] = [
        cast("dict[str, Any]", observation)
        for observation in raw_observations
        if isinstance(observation, dict)
    ]
    denial_counts = {
        "provider_429": sum(
            observation.get("category") == "provider"
            and observation.get("expected_upstream_status") == 429
            and observation.get("client_http_status") == 429
            for observation in observations
        ),
        "user_budget_429": sum(
            observation.get("category") == "budget"
            and observation.get("denial_kind") == "user_budget"
            and observation.get("http_status") == 429
            for observation in observations
        ),
        "global_budget_429": sum(
            observation.get("category") == "budget"
            and observation.get("denial_kind") == "global_budget"
            and observation.get("http_status") == 429
            for observation in observations
        ),
        "identity_denial": 1,
    }
    if any(
        denial_counts[name] != 1 for name in denial_counts if name != "identity_denial"
    ):
        raise ValueError("ordinary-server provider/budget denial counts are incomplete")
    output_dir = manifest_path.parent
    conditions = manifest.get("conditions")
    if not isinstance(conditions, list):
        raise ValueError("condition matrix is missing")
    conditions = cast("list[Any]", conditions)
    summaries: list[dict[str, Any]] = []
    for raw_condition in conditions:
        if not isinstance(raw_condition, dict):
            raise ValueError("condition entry is invalid")
        condition = cast("dict[str, Any]", raw_condition)
        principals = condition.get("principals")
        concurrency = condition.get("concurrency")
        raw_names = condition.get("run_artifacts")
        if not isinstance(principals, int) or not isinstance(concurrency, int):
            raise ValueError("condition counts are invalid")
        if not isinstance(raw_names, list):
            raise ValueError("each condition must name exactly three raw runs")
        raw_names = cast("list[Any]", raw_names)
        if len(raw_names) != REPETITIONS:
            raise ValueError("each condition must name exactly three raw runs")
        runs: list[dict[str, Any]] = []
        for name in raw_names:
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("run artifact name is unsafe")
            document = _load(output_dir / name)
            if document.get("status") != "pass":
                raise ValueError(f"qualification run did not pass: {name}")
            if document.get("schema_version") != "runtime-q008.v2":
                raise ValueError(f"unexpected Q008 schema: {name}")
            run_candidate = document.get("candidate")
            if not isinstance(run_candidate, dict):
                raise ValueError(f"candidate identity missing: {name}")
            run_candidate = cast("dict[str, Any]", run_candidate)
            if run_candidate.get("sha256") != candidate.get("sha256"):
                raise ValueError(f"candidate hash changed: {name}")
            if run_candidate.get("repository_commit_sha") != candidate.get(
                "repository_commit_sha"
            ):
                raise ValueError(f"candidate source commit changed: {name}")
            environment = document.get("environment", {})
            if environment.get("target_class") != "ordinary-server":
                raise ValueError(f"ordinary-server attestation missing: {name}")
            database_storage = environment.get("qualification_database_storage", {})
            if (
                database_storage.get("filesystem") in {"tmpfs", "ramfs", "overlay"}
                or database_storage.get("storage_medium_class") != "non-rotational"
                or database_storage.get("device_class") not in {"nvme", "scsi-disk"}
            ):
                raise ValueError(f"run database was not on durable SSD/NVMe: {name}")
            benchmark = document.get("benchmark", {})
            if (
                benchmark.get("principal_count") != principals
                or benchmark.get("budget_mode") != "user-global"
                or benchmark.get("provider_latency_profile") != PROVIDER_LATENCY_PROFILE
            ):
                raise ValueError(f"workload does not match condition: {name}")
            load = benchmark.get("runs", {}).get(
                "concurrent_native_responses_finite", {}
            )
            timing = load.get("timings", {})
            if (
                load.get("client_concurrency") != concurrency
                or load.get("request_count") != REQUESTS_PER_CELL
                or load.get("completed_count") != REQUESTS_PER_CELL
                or load.get("failed_count") != 0
                or timing.get("sample_count") != REQUESTS_PER_CELL
                or "p99_elapsed_ms" not in timing
            ):
                raise ValueError(f"request or p99 gate failed: {name}")
            durable = document.get("durable", {})
            if (
                durable.get("pending_requests") != 0
                or durable.get("active_reservations") != 0
            ):
                raise ValueError(f"durable reservations did not converge: {name}")
            runs.append({"artifact": name, "document": document, "load": load})

        def median(
            field: str, *, cell_runs: list[dict[str, Any]] = runs
        ) -> int | float | None:
            values = [run["load"][field] for run in cell_runs]
            if not all(isinstance(value, (int, float)) for value in values):
                return None
            return statistics.median(values)

        cpu_values = [
            run["load"]["cpu"].get("cpu_percent")
            for run in runs
            if isinstance(run["load"].get("cpu"), dict)
        ]

        def timing_median(
            field: str, *, cell_runs: list[dict[str, Any]] = runs
        ) -> int | float | None:
            values = [run["load"]["timings"].get(field) for run in cell_runs]
            if not all(isinstance(value, (int, float)) for value in values):
                return None
            return statistics.median(values)

        sequential_keys = (
            "native_responses_finite",
            "native_responses_streaming",
            "translated_responses_to_messages_streaming",
        )
        sequential_surfaces: dict[str, dict[str, int | float | None]] = {}
        for surface_key in sequential_keys:
            surface_runs = [
                run["document"]
                .get("benchmark", {})
                .get("runs", {})
                .get(surface_key, {})
                for run in runs
            ]
            surface_summary: dict[str, int | float | None] = {
                "sample_count": min(
                    (
                        surface.get("sample_count", 0)
                        for surface in surface_runs
                        if isinstance(surface.get("sample_count"), int)
                    ),
                    default=0,
                )
            }
            for field in (
                "p50_elapsed_ms",
                "p95_elapsed_ms",
                "p50_ttft_ms",
                "p95_ttft_ms",
            ):
                values = [surface.get(field) for surface in surface_runs]
                surface_summary[field] = (
                    statistics.median(values)
                    if len(values) == REPETITIONS
                    and all(isinstance(value, (int, float)) for value in values)
                    else None
                )
            sequential_surfaces[surface_key] = surface_summary

        io_pressure_values = [
            run["load"]
            .get("host_io", {})
            .get("io_pressure_after", {})
            .get("full", {})
            .get("avg10")
            for run in runs
        ]
        write_delta_values = [
            run["load"].get("host_io", {}).get("root_device_write_delta_bytes")
            for run in runs
        ]

        wal_bytes: list[int] = []
        for run in runs:
            raw_samples = run["document"].get("resource_samples", [])
            if not isinstance(raw_samples, list):
                continue
            for raw_sample in cast("list[Any]", raw_samples):
                if not isinstance(raw_sample, dict):
                    continue
                sample = cast("dict[str, Any]", raw_sample)
                value = sample.get("wal_bytes")
                if isinstance(value, int):
                    wal_bytes.append(value)
        durable_attempts = [
            run["document"].get("durable", {}).get("attempts") for run in runs
        ]

        summaries.append(
            {
                "principals": principals,
                "concurrency": concurrency,
                "repetitions": len(runs),
                "requests_per_run": REQUESTS_PER_CELL,
                "median_requests_per_second": median("requests_per_second"),
                "median_cpu_percent": statistics.median(cpu_values)
                if cpu_values
                and all(isinstance(value, (int, float)) for value in cpu_values)
                else None,
                "median_p50_ms": timing_median("p50_elapsed_ms"),
                "median_p95_ms": timing_median("p95_elapsed_ms"),
                "median_p99_ms": timing_median("p99_elapsed_ms"),
                "maximum_ms": max(
                    run["load"]["timings"]["maximum_elapsed_ms"] for run in runs
                ),
                "maximum_peak_rss_bytes": max(
                    run["document"]["benchmark"]["resource_summary"]["peak_rss_bytes"]
                    for run in runs
                ),
                "sequential_surfaces": sequential_surfaces,
                "median_io_pressure_full_avg10_after_percent": (
                    statistics.median(io_pressure_values)
                    if all(
                        isinstance(value, (int, float)) for value in io_pressure_values
                    )
                    else None
                ),
                "median_root_device_write_delta_bytes": (
                    statistics.median(write_delta_values)
                    if all(isinstance(value, int) for value in write_delta_values)
                    else None
                ),
                "maximum_observed_wal_bytes": max(wal_bytes, default=None),
                "median_durable_attempts": statistics.median(durable_attempts)
                if all(isinstance(value, int) for value in durable_attempts)
                else None,
                "settlement": (
                    "zero pending requests and active reservations in every run"
                ),
                "raw_runs": [run["artifact"] for run in runs],
            }
        )
    return {
        "schema_version": SUMMARY_VERSION,
        "plan": manifest.get("plan"),
        "candidate": candidate,
        "target": manifest.get("target"),
        "workload": manifest.get("workload"),
        "lifecycle": {
            "artifact": LIFECYCLE_ARTIFACT,
            "status": "pass",
            "phases": sorted(required_phases),
            "provider": lifecycle.get("provider"),
            "ownership_convergence": lifecycle.get("ownership_convergence"),
            "workload_policy": workload_policy,
            "denial_counts": denial_counts,
            "identity_denial_http_status": restart_phase.get(
                "identity_denial_http_status"
            ),
        },
        "interpretation": (
            "Descriptive loopback capacity characterization for this host and "
            "synthetic workload; not an SLA or provider-throughput promise."
        ),
        "conditions": summaries,
    }


def run(binary: Path, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "matrix.json"
    summary_path = output_dir / "summary.json"
    fresh_manifest = _manifest(binary, output_dir)
    manifest = fresh_manifest
    if manifest_path.is_file():
        try:
            previous = _load(manifest_path)
            resumable = (
                previous.get("schema_version") == SCHEMA_VERSION
                and previous.get("status") in {"incomplete", "complete"}
                and previous.get("candidate", {}).get("sha256")
                == fresh_manifest["candidate"]["sha256"]
                and previous.get("target") == fresh_manifest["target"]
                and previous.get("workload") == fresh_manifest["workload"]
                and previous.get("conditions") == fresh_manifest["conditions"]
                and isinstance(
                    previous.get("candidate", {}).get("repository_commit_sha"), str
                )
            )
            if resumable:
                manifest = previous
                manifest["qualification_tooling_commit_sha"] = _commit()
                manifest["preflight_io"] = fresh_manifest["preflight_io"]
                manifest["status"] = fresh_manifest["status"]
                manifest["stop_reason"] = fresh_manifest["stop_reason"]
                manifest.pop("stopped_at", None)
        except (OSError, ValueError, AttributeError):
            manifest = fresh_manifest
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if manifest["status"] == "blocked":
        return 77
    for condition in manifest["conditions"]:
        for run_number, name in enumerate(condition["run_artifacts"], start=1):
            output = output_dir / name
            if output.is_file():
                try:
                    prior = _load(output)
                    prior_candidate = prior.get("candidate", {})
                    prior_benchmark = prior.get("benchmark", {})
                    prior_load = prior_benchmark.get("runs", {}).get(
                        "concurrent_native_responses_finite", {}
                    )
                    reusable = (
                        prior.get("status") == "pass"
                        and prior.get("schema_version") == "runtime-q008.v2"
                        and prior_candidate.get("sha256")
                        == manifest["candidate"]["sha256"]
                        and prior_candidate.get("repository_commit_sha")
                        == manifest["candidate"]["repository_commit_sha"]
                        and prior_benchmark.get("principal_count")
                        == condition["principals"]
                        and prior_load.get("client_concurrency")
                        == condition["concurrency"]
                    )
                except (OSError, ValueError, AttributeError):
                    reusable = False
                if reusable:
                    continue
            command = [
                sys.executable,
                str(ROOT / "scripts/qualification_sbc.py"),
                "--binary",
                str(binary),
                "--target-class",
                "ordinary-server",
                "--config-fixture",
                str(ROOT / "tests/tooling/fixtures/qualification/sbc-benchmark.toml"),
                "--benchmark-samples",
                str(SEQUENTIAL_SAMPLES),
                "--concurrency-workers",
                str(condition["concurrency"]),
                "--concurrency-requests",
                str(REQUESTS_PER_CELL),
                "--benchmark-principals",
                str(condition["principals"]),
                "--benchmark-budget-mode",
                "user-global",
                "--provider-latency-profile",
                PROVIDER_LATENCY_PROFILE,
                "--candidate-origin",
                "on-device-release-build",
                "--output",
                str(output),
            ]
            result = subprocess.run(command, cwd=ROOT, check=False)
            if result.returncode != 0:
                manifest["status"] = "incomplete"
                manifest["stopped_at"] = {
                    "artifact": name,
                    "run_number": run_number,
                    "returncode": result.returncode,
                }
                manifest_path.write_text(
                    json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
                )
                return result.returncode
    lifecycle_path = output_dir / LIFECYCLE_ARTIFACT
    lifecycle_command = [
        sys.executable,
        str(ROOT / "scripts/qualification_stability.py"),
        "--binary",
        str(binary),
        "--phase",
        "faults",
        "--phase",
        "restart",
        "--exercise-revocation-during-restart",
        "--output",
        str(lifecycle_path),
    ]
    lifecycle_result = subprocess.run(lifecycle_command, cwd=ROOT, check=False)
    if lifecycle_result.returncode != 0:
        manifest["status"] = "incomplete"
        manifest["stopped_at"] = {
            "artifact": LIFECYCLE_ARTIFACT,
            "returncode": lifecycle_result.returncode,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return lifecycle_result.returncode
    manifest["lifecycle_artifact"] = LIFECYCLE_ARTIFACT
    manifest["status"] = "complete"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    summary = summarize(manifest_path)
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        return run(args.binary, args.output_dir)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
