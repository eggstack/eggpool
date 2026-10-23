"""Guards for the dashboard parity tooling's default inputs.

The parity tooling defaults its JSON and Markdown report paths into the
``migration-rs/closure/`` tree. A clean clone must be able to resolve those
defaults, so the default fixtures are tracked, and the harness's recorded
baselines stay untouched.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from scripts.dashboard_parity._shared import DEFAULT_JSON, DEFAULT_MARKDOWN

ROOT = Path(__file__).parents[2]
ORACLE_REPORT = ROOT / "tests/fixtures/dashboard-python-oracle/current-gap-report.json"
SECRET_MARKERS = ("q012-server-key", "sk-proj-", "OPENAI_API_KEY", "EGGPOOL_API_KEY")
ABSOLUTE_HOME = re.compile(r"/Users/|/home/[A-Za-z0-9._-]+/")


def tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def repository_relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def test_parity_default_inputs_resolve_inside_the_repository() -> None:
    assert repository_relative(DEFAULT_JSON).startswith("migration-rs/closure/")
    assert repository_relative(DEFAULT_MARKDOWN).startswith("migration-rs/closure/")


def test_parity_default_inputs_are_tracked() -> None:
    tracked = tracked_paths()
    for default in (DEFAULT_JSON, DEFAULT_MARKDOWN):
        relative = repository_relative(default)
        assert relative in tracked, relative
        assert default.is_file(), relative
        assert default.stat().st_size > 0, relative


def test_parity_default_inputs_carry_no_secret_or_operator_path() -> None:
    for default in (DEFAULT_JSON, DEFAULT_MARKDOWN):
        text = default.read_text(encoding="utf-8")
        for marker in SECRET_MARKERS:
            assert marker not in text, (default, marker)
        assert ABSOLUTE_HOME.search(text) is None, default


def test_parity_recorded_baseline_is_unchanged() -> None:
    report = json.loads(ORACLE_REPORT.read_text(encoding="utf-8"))
    assert report["schema_version"] == "dashboard-parity-current-gaps.v1"
    assert report["candidate_sha"] == "3cf7671"
    assert report["parity_status"] == "gaps"
    assert report["mismatch_count"] > 0
    assert sum(report["mismatch_groups"].values()) == report["mismatch_count"]


def test_parity_default_report_carries_the_same_report_shape() -> None:
    report = json.loads(DEFAULT_JSON.read_text(encoding="utf-8"))
    baseline = json.loads(ORACLE_REPORT.read_text(encoding="utf-8"))
    assert report["schema_version"] == baseline["schema_version"]
    assert set(baseline) <= set(report)
    assert report["parity_status"] == baseline["parity_status"]
    assert set(report["mismatch_groups"]) == set(baseline["mismatch_groups"])
