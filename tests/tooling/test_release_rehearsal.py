"""Deterministic contracts for the release rehearsal coordinator."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.create_release_manifest import create_manifest
from scripts.qualification_release_rehearsal import (
    MANIFEST_SCHEMA,
    _failure_injection_evidence,
    _installer_evidence,
    render_markdown,
)

from .test_release_artifacts import _artifact_set

ROOT = Path(__file__).parents[2]


def test_artifact_mutations_fail_closed(tmp_path: Path) -> None:
    _artifact_set(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    create_manifest(tmp_path, manifest_path)
    result = _failure_injection_evidence(tmp_path, manifest_path)
    assert result["status"] == "pass"
    cases = result["cases"]
    assert cases["missing_artifact_aggregation"] == "pass"
    assert cases["corrupted_wheel_before_publish"] == "pass"
    assert cases["unsupported_extra_artifact"] == "pass"
    assert cases["production_publish_from_rehearsal"] == "workflow gate"


def test_report_is_bounded_and_secret_free() -> None:
    report = {
        "schema_version": MANIFEST_SCHEMA,
        "status": "pass",
        "release": {
            "version": "0.8.0",
            "source_commit": "a" * 40,
            "manifest_sha256": "b" * 64,
        },
        "environment": {"os": "Linux", "architecture": "x86_64"},
        "target": {"status": "pass"},
        "unsupported_target": {"status": "pass"},
        "installer": {"status": "pass"},
        "workflow": {"status": "pass"},
        "failure_injection": {"status": "pass"},
        "testpypi": {"status": "not_run"},
    }
    markdown = render_markdown(report)
    assert "api_key" not in markdown
    assert "source_commit" not in markdown
    assert "0.8.0" in markdown


def test_installer_allows_only_explicit_nonproduction_sources() -> None:
    result = subprocess.run(
        ["bash", "-n", str(ROOT / "scripts/install.sh")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    text = (ROOT / "scripts/install.sh").read_text(encoding="utf-8")
    assert "EGGPOOL_INSTALL_ALLOW_NONPRODUCTION_INDEX" in text
    assert "EGGPOOL_INSTALL_FIND_LINKS" in text
    assert "EGGPOOL_INSTALL_INDEX_URL" in text


def test_installer_skips_without_a_runnable_host_target(tmp_path: Path) -> None:
    result = _installer_evidence(tmp_path, "0.8.0", None)
    assert result == {
        "status": "skipped",
        "reason": "target does not match this host",
    }


def test_runner_schema_is_machine_readable(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(
        json.dumps({"schema_version": MANIFEST_SCHEMA}), encoding="utf-8"
    )
    loaded = json.loads(report_path.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == "release-rehearsal.v1"
