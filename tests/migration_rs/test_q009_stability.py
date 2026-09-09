"""Contract tests for the bounded Q009 stability runner."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.qualification_stability import (
    DEFAULT_FIXTURE,
    MAX_SAMPLES,
    SCHEMA_VERSION,
    FaultProvider,
    _render_fixture,
    main,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_q009_fixture_is_loopback_only_and_secret_free() -> None:
    text = DEFAULT_FIXTURE.read_text(encoding="utf-8")
    assert "__Q009_UPSTREAM__" in text
    assert "q009-provider-key-a" in text
    assert "q009-provider-key-b" in text
    assert "https://" not in text


def test_q009_fault_provider_is_deterministic_and_bounded() -> None:
    with FaultProvider() as provider:
        assert provider.record("q009-fault-408") == 1
        assert provider.record("q009-fault-408") == 2
        assert provider.count("q009-fault-408") == 2


def test_q009_fixture_render_rejects_unresolved_placeholders(tmp_path: Path) -> None:
    destination = tmp_path / "config.toml"
    content = _render_fixture(
        DEFAULT_FIXTURE,
        destination,
        port=12345,
        upstream="http://127.0.0.1:54321",
        database=tmp_path / "usage.sqlite3",
        backup_dir=tmp_path / "backups",
    )
    assert "__Q009_" not in content
    assert destination.is_file()


def test_q009_missing_candidate_fails_before_creating_workload(tmp_path: Path) -> None:
    output = tmp_path / "q009.json"
    exit_code = main(["--binary", str(tmp_path / "missing"), "--output", str(output)])
    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["status"] == "fail"
    assert report.get("phases", []) == []


def test_q009_fake_candidate_failure_is_reported_without_workload(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    candidate.chmod(0o700)
    output = tmp_path / "q009.json"
    assert main(["--binary", str(candidate), "--output", str(output)]) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "fail"
    assert "candidate exited" in report["reason"]
    assert MAX_SAMPLES > 0
