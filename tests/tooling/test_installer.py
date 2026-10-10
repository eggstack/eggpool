"""Deterministic installer quick-installer qualification."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_quick_installer_harness_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/qualify_quick_installer.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "pass"
    assert len(report["cases"]) == 73
    # Regression guard: stale pipx must never block a fresh native install.
    cases = {item["case"] for item in report["cases"]}
    assert "fresh-linux-aarch64-stale-pipx-ignored" in cases
    assert "fresh-linux-aarch64-no-python" in cases
    assert "release-manifest-raw-contract" in cases
    # M002 corrective regressions: --force never overwrites unowned files and
    # first-time config failure rolls back the committed executable.
    assert "fresh-force-unowned-regular-refusal" in cases
    assert "fresh-init-config-failure-rolls-back-binary" in cases
    assert "fresh-init-config-failure-preserves-preexisting-config" in cases
    assert "fresh-config-failure-destination-race-refusal" in cases
    # M003 corrective regressions: transaction-owned staging + no-clobber
    # publication; final config is never rollback scratch space.
    assert "fresh-config-concurrent-writer-preserved" in cases
    assert "fresh-config-publish-race-preserves-winner" in cases
    assert "fresh-staged-first-config-created" in cases
    assert "fresh-staged-generation-failure-leaves-final-absent" in cases
    assert "fresh-staged-partial-cleaned-after-generation-failure" in cases
    assert "fresh-config-symlink-refusal" in cases
    assert "fresh-config-special-refusal" in cases
    assert "fresh-config-noclobber-never-overwrites" in cases
    assert "fresh-signal-cleanup-removes-staging" in cases
    assert "fresh-executable-rollback-on-config-generation-failure" in cases
    assert "fresh-executable-race-still-preserved" in cases
    assert "existing-owner-first-config-uses-safe-staging" in cases
    assert "existing-owner-concurrent-config-preserved" in cases
    assert "existing-owner-package-standalone-unchanged" in cases
    # M004 corrective regressions: PATH-hidden owner provenance, safe profile
    # persistence, real Bash startup resolution, and parent-shell activation.
    assert "hidden-canonical-native-owner" in cases
    assert "hidden-canonical-manager-owner" in cases
    assert "hidden-foreign-force-refusal-preserves-bytes" in cases
    assert "path-visible-command-canonical-conflict-refusal" in cases
    assert "profile-zdotdir-custom-path-idempotent" in cases
    assert "linux-bash-interactive-startup-resolution" in cases
    assert "macos-login-bash-profile-selection" in cases
    assert "profile-symlink-refusal-and-optout" in cases
    assert "active-user-path-reused-comment-only-ignored" in cases
    assert "unsupported-shell-and-unsafe-bin-fallback" in cases
    assert "failed-config-transaction-does-not-edit-profile" in cases
    assert "documented-parent-shell-activation" in cases
    assert "package-manager-profile-uses-verified-owner-bin" in cases
