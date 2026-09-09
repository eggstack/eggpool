"""Unit coverage for the Q002 aggregate runner's fail-closed contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.qualification_runner import (
    DEFAULT_MANIFEST,
    MAX_RESULT_BYTES,
    ManifestValidationError,
    ProcessResult,
    ResultStatus,
    load_q002_cells,
    run_qualification,
    write_report,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def _fake_candidate(tmp_path: Path) -> Path:
    candidate = tmp_path / "eggpool"
    candidate.write_text("candidate", encoding="utf-8")
    return candidate


def _manifest_copy(tmp_path: Path) -> Path:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _executor_for(
    failing: set[str] | None = None,
    skipped: set[str] | None = None,
):
    failing = failing or set()
    skipped = skipped or set()

    def execute(argv: Sequence[str], *, cwd: Path, timeout: float) -> ProcessResult:
        del cwd, timeout
        command = " ".join(argv)
        if any(marker in command for marker in failing):
            return ProcessResult(tuple(argv), 1, False, 7, "assertion mismatch")
        if any(marker in command for marker in skipped):
            return ProcessResult(tuple(argv), 5, False, 3, "")
        if argv[:2] == ("cargo", "build"):
            return ProcessResult(tuple(argv), 0, False, 5, "")
        return ProcessResult(tuple(argv), 0, False, 4, "")

    return execute


def test_q002_rejects_stale_or_missing_manifest_cells(tmp_path: Path) -> None:
    path = _manifest_copy(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["cells"][0]["id"] = "q001.config.removed"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ManifestValidationError, match="ownership drift"):
        load_q002_cells(path)


def test_q002_injected_mandatory_failure_is_not_hidden(tmp_path: Path) -> None:
    report = run_qualification(
        manifest_path=_manifest_copy(tmp_path),
        root=Path.cwd(),
        rust_executable=_fake_candidate(tmp_path),
        executor=_executor_for(failing={"test_f003_config_cli.py"}),
        skip_build=True,
    )

    failed = {
        result.cell.cell_id
        for result in report.results
        if result.status is ResultStatus.FAIL
    }
    assert "q001.config.resolution" in failed
    assert report.to_dict()["counts"][ResultStatus.FAIL.value] > 0


def test_q002_distinguishes_skip_and_block_from_pass(tmp_path: Path) -> None:
    report = run_qualification(
        manifest_path=_manifest_copy(tmp_path),
        root=Path.cwd(),
        rust_executable=_fake_candidate(tmp_path),
        executor=_executor_for(skipped={"test_t001_provider_transport.py"}),
        skip_build=True,
    )
    statuses = {result.status for result in report.results}
    assert ResultStatus.PASS in statuses
    assert ResultStatus.SKIP in statuses
    assert ResultStatus.BLOCK not in statuses

    blocked = run_qualification(
        manifest_path=_manifest_copy(tmp_path),
        root=Path.cwd(),
        rust_executable=tmp_path / "missing-eggpool",
        executor=_executor_for(),
    )
    assert {result.status for result in blocked.results} == {ResultStatus.BLOCK}
    assert blocked.preflight[0]["status"] == ResultStatus.INFRASTRUCTURE_ERROR.value


def test_q002_artifacts_are_bounded_and_omit_command_output(tmp_path: Path) -> None:
    report = run_qualification(
        manifest_path=_manifest_copy(tmp_path),
        root=Path.cwd(),
        rust_executable=_fake_candidate(tmp_path),
        executor=_executor_for(failing={"test_f003_config_cli.py"}),
        skip_build=True,
    )
    output = tmp_path / "q002.json"
    markdown = tmp_path / "q002.md"
    write_report(report, output, markdown)
    payload = output.read_bytes()
    assert len(payload) < MAX_RESULT_BYTES
    assert b"stdout" not in payload
    assert b"stderr" not in payload
    assert b"api_key=" not in payload
    assert markdown.read_text(encoding="utf-8").startswith("# Q002")


def test_q002_report_keeps_rule_and_structural_mismatch_fields(tmp_path: Path) -> None:
    report = run_qualification(
        manifest_path=_manifest_copy(tmp_path),
        root=Path.cwd(),
        rust_executable=_fake_candidate(tmp_path),
        executor=_executor_for(),
        skip_build=True,
    )
    row = report.to_dict()["results"][0]
    assert row["normalization_rule"] == "isolated_path_root"
    assert "python_observation" in row
    assert "rust_observation" in row
    assert "first_differing_semantic_field" in row
    assert row["owning_subsystem"] == "configuration"
