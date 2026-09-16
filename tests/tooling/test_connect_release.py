"""Desktop helper release identity, bootstrap safety, and dependency surface."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.create_release_manifest import create_manifest
from scripts.inspect_connect_artifact import (
    CONNECT_BOOTSTRAPS,
    CONNECT_TARGETS,
    ConnectInspectionError,
    connect_filename,
    inspect_connect_artifact,
    inspect_connect_bootstrap,
)
from scripts.inspect_release_raw import TARGETS
from scripts.validate_release_artifacts import ValidationError, validate_manifest
from scripts.validate_release_workflow import validate_workflow

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/release.yml"
POSIX_BOOTSTRAP = ROOT / "packaging/connect/eggpool-connect.sh"
POWERSHELL_BOOTSTRAP = ROOT / "packaging/connect/eggpool-connect.ps1"

VERSION = "0.8.0"

NATIVE_PAYLOADS = {
    "elf-62": (b"\x7fELF" + b"\0" * 14 + (62).to_bytes(2, "little") + b"helper"),
    "elf-183": (b"\x7fELF" + b"\0" * 14 + (183).to_bytes(2, "little") + b"helper"),
    "macho": (b"\xcf\xfa\xed\xfe" + (0x0100000C).to_bytes(4, "little") + b"helper"),
    "pe": (b"MZ" + b"\0" * 62 + b"helper"),
}
TARGET_PAYLOAD = {
    "connect-linux-x86_64": "elf-62",
    "connect-linux-aarch64": "elf-183",
    "connect-macos-arm64": "macho",
    "connect-windows-x86_64": "pe",
}

# Server-only dependency families the helper must never link. The helper is
# a transactional desktop configurator; Axum/SQLite/Eggress/proxy internals
# would mean the portable boundary leaked.
FORBIDDEN_HELPER_DEPS = (
    "axum-",
    "eggress-",
    "rusqlite",
    "tokio-rusqlite",
    "sqlx-",
    "nix ",
    "tower ",
)


def _helper_fixture(directory: Path, target_class: str) -> Path:
    payload = NATIVE_PAYLOADS[TARGET_PAYLOAD[target_class]]
    path = directory / connect_filename(VERSION, target_class)
    path.write_bytes(payload)
    if not target_class.startswith("connect-windows"):
        path.chmod(0o755)
    return path


def _bootstrap_fixture(directory: Path) -> None:
    for name in CONNECT_BOOTSTRAPS:
        (directory / name).write_bytes((ROOT / "packaging/connect" / name).read_bytes())


def test_helper_target_matrix_is_distinct_from_proxy_matrix() -> None:
    assert set(CONNECT_TARGETS) == {
        "connect-linux-x86_64",
        "connect-linux-aarch64",
        "connect-macos-arm64",
        "connect-windows-x86_64",
    }
    # No helper target class collides with a proxy target class, and no
    # helper filename collides with a proxy raw filename.
    assert not (set(CONNECT_TARGETS) & set(TARGETS))
    for target_class in CONNECT_TARGETS:
        name = connect_filename(VERSION, target_class)
        assert name.startswith("eggpool-connect-")
        assert "eggpool-connect-" not in {
            f"eggpool-{VERSION}-linux-x86_64",
            f"eggpool-{VERSION}-linux-aarch64",
            f"eggpool-{VERSION}-macos-arm64",
        }
    assert connect_filename(VERSION, "connect-windows-x86_64").endswith(".exe")
    with pytest.raises(ConnectInspectionError, match="unsupported helper target"):
        connect_filename(VERSION, "linux-x86_64")


def test_helper_inspector_enforces_format_and_executable_mode(
    tmp_path: Path,
) -> None:
    for target_class in CONNECT_TARGETS:
        path = _helper_fixture(tmp_path, target_class)
        result = inspect_connect_artifact(
            path, expected_version=VERSION, target_class=target_class
        )
        assert result.kind == "eggpool-connect"
        assert result.executable is True
    # Wrong arch payload fails closed.
    mismatch = tmp_path / connect_filename(VERSION, "connect-linux-x86_64")
    mismatch.write_bytes(NATIVE_PAYLOADS["elf-183"])
    mismatch.chmod(0o755)
    with pytest.raises(ConnectInspectionError, match="arch"):
        inspect_connect_artifact(
            mismatch, expected_version=VERSION, target_class="connect-linux-x86_64"
        )
    # Missing executable bit fails closed on POSIX targets.
    plain = tmp_path / connect_filename(VERSION, "connect-linux-aarch64")
    plain.write_bytes(NATIVE_PAYLOADS["elf-183"])
    plain.chmod(0o644)
    with pytest.raises(ConnectInspectionError, match="executable mode"):
        inspect_connect_artifact(
            plain, expected_version=VERSION, target_class="connect-linux-aarch64"
        )


def test_manifest_keeps_proxy_invariant_with_helper_section(
    tmp_path: Path,
) -> None:
    from tooling.test_release_artifacts import _artifact_set

    _artifact_set(tmp_path)
    # Proxy-only manifest: helper section is empty, proxy invariant untouched.
    assert create_manifest(tmp_path, tmp_path / "proxy.json")["connect_artifacts"] == []
    # A stray helper-looking file without a manifest entry fails closed.
    (tmp_path / connect_filename(VERSION, "connect-linux-x86_64")).write_bytes(b"MZ")
    with pytest.raises(ValidationError, match="unsupported or unmanifested"):
        validate_manifest(tmp_path / "proxy.json", tmp_path)


def test_manifest_validates_complete_helper_set(tmp_path: Path) -> None:
    from tooling.test_release_artifacts import _artifact_set

    _artifact_set(tmp_path)
    connect_dir = tmp_path / "connect"
    connect_dir.mkdir()
    for target_class in CONNECT_TARGETS:
        payload = NATIVE_PAYLOADS[TARGET_PAYLOAD[target_class]]
        path = connect_dir / connect_filename(VERSION, target_class)
        path.write_bytes(payload)
        if not target_class.startswith("connect-windows"):
            path.chmod(0o755)
    _bootstrap_fixture(connect_dir)
    manifest_path = tmp_path / "manifest.json"
    manifest = create_manifest(
        tmp_path, manifest_path, connect_artifact_dir=connect_dir
    )
    assert len(manifest["connect_artifacts"]) == len(CONNECT_TARGETS) + len(
        CONNECT_BOOTSTRAPS
    )
    kinds = {record["kind"] for record in manifest["connect_artifacts"]}
    assert kinds == {"eggpool-connect", "connect-bootstrap"}
    validated = validate_manifest(
        manifest_path, tmp_path, connect_artifact_dir=connect_dir
    )
    assert len(validated["connect_artifacts"]) == 6
    # Tampering with one helper byte invalidates the set.
    tampered = connect_dir / connect_filename(VERSION, "connect-linux-x86_64")
    tampered.write_bytes(tampered.read_bytes() + b"x")
    with pytest.raises(ValidationError, match="digest/size mismatch"):
        validate_manifest(manifest_path, tmp_path, connect_artifact_dir=connect_dir)


def test_posix_bootstrap_is_reviewed_and_safe() -> None:
    text = POSIX_BOOTSTRAP.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    for required in (
        "set -eu",
        "--proto '=https'",
        "SHA256SUMS",
        "SHA-256 mismatch",
        "mktemp -d",
        "trap",
        "rm -rf",
        "--profile",
        "--version",
        "epc1.",
        "eggpool-connect.ps1",
    ):
        assert required in text, f"bootstrap is missing: {required}"
    lowered = text.lower()
    for forbidden in (
        "eval ",
        "curl -k",
        "curl --insecure",
        "sh -c",
        ". $tmp",
        "echo $profile",
    ):
        assert forbidden not in lowered, f"bootstrap contains: {forbidden}"
    # The token is only ever passed as a data argument, never printed.
    for line in text.splitlines():
        if line.strip().startswith("echo"):
            for var in ("$PROFILE", "$VERSION", "$REPO", "$CLIENT"):
                assert var not in line, f"bootstrap prints user input: {line}"
    assert POSIX_BOOTSTRAP.stat().st_mode & 0o111 != 0
    record = inspect_connect_bootstrap(POSIX_BOOTSTRAP)
    assert record["kind"] == "connect-bootstrap"
    assert record["executable"] is False


def test_powershell_bootstrap_is_reviewed_and_safe() -> None:
    text = POWERSHELL_BOOTSTRAP.read_text(encoding="utf-8")
    for required in (
        "Get-FileHash",
        "-Algorithm SHA256",
        "SHA256SUMS",
        "SHA-256 mismatch",
        "Invoke-WebRequest",
        "Remove-Item",
        "-Profile",
        "-Version",
        "epc1.",
        "eggpool-connect.sh",
    ):
        assert required in text, f"bootstrap is missing: {required}"
    lowered = text.lower()
    for forbidden in (
        "invoke-expression",
        "iex ",
        "-skipcertificatecheck",
        "executionpolicy",
        "unrestricted",
    ):
        assert forbidden not in lowered, f"bootstrap contains: {forbidden}"
    record = inspect_connect_bootstrap(POWERSHELL_BOOTSTRAP)
    assert record["kind"] == "connect-bootstrap"


def test_release_workflow_keeps_helper_matrix_typed() -> None:
    summary = validate_workflow(WORKFLOW)
    assert summary["status"] == "pass"
    assert summary["targets"] == ["linux-x86_64", "linux-aarch64", "macos-arm64"]
    assert sorted(summary["connect_targets"]) == sorted(CONNECT_TARGETS)
    assert len(summary["jobs"]) == 13


def test_helper_dependency_surface_stays_narrow() -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo is unavailable")
    try:
        result = subprocess.run(
            [
                cargo,
                "tree",
                "--manifest-path",
                str(ROOT / "rust/Cargo.toml"),
                "-p",
                "eggpool-connect",
                "--edges",
                "normal",
                "--prefix",
                "none",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        pytest.skip(f"cargo tree is unavailable: {error}")
    tree = result.stdout.lower()
    for forbidden in FORBIDDEN_HELPER_DEPS:
        assert forbidden not in tree, f"helper links a server-only dep: {forbidden}"
    for expected in ("clap", "serde", "hyper", "tokio", "sha2"):
        assert expected in tree, f"helper is missing an expected dep: {expected}"
