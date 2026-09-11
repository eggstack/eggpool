"""Focused contract tests for the release artifacts wheel/raw artifact matrix."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import zipfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from scripts.create_release_manifest import create_manifest
from scripts.inspect_release_raw import RawInspectionError, inspect_raw, raw_filename
from scripts.inspect_release_wheel import TARGET_PLATFORMS
from scripts.validate_release_artifacts import ValidationError, validate_manifest

VERSION = "0.8.0"
TARGETS = {
    "linux-x86_64": ("manylinux_2_17_x86_64", 62),
    "linux-aarch64": ("manylinux_2_17_aarch64", 183),
    "macos-arm64": ("macosx_11_0_arm64", 0x0100000C),
}


def _native(machine: int, kind: str) -> bytes:
    if kind == "macho":
        return b"\xcf\xfa\xed\xfe" + machine.to_bytes(4, "little") + b"native"
    return b"\x7fELF" + b"\0" * 14 + machine.to_bytes(2, "little") + b"native"


def _make_wheel(directory: Path, target_class: str) -> Path:
    platform_tag, machine = TARGETS[target_class]
    kind = "macho" if target_class == "macos-arm64" else "elf"
    platform_tags = [platform_tag]
    if target_class.startswith("linux-"):
        platform_tags.append(platform_tag.replace("manylinux_2_17", "manylinux2014"))
    wheel = directory / (f"eggpool-{VERSION}-py3-none-{'.'.join(platform_tags)}.whl")
    dist_info = f"eggpool-{VERSION}.dist-info"
    executable = f"eggpool-{VERSION}.data/scripts/eggpool"
    members = {
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.3\nName: eggpool\nVersion: 0.8.0\n"
            b"Summary: Native EggPool proxy\nRequires-Python: >=3.11\n"
            b"License-File: LICENSE\n\n"
        ),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
            + "".join(f"Tag: py3-none-{tag}\n" for tag in platform_tags).encode()
            + b"\n"
        ),
        f"{dist_info}/licenses/LICENSE": b"MIT License\n",
        executable: _native(machine, kind),
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, content in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
        writer.writerow((name, f"sha256={digest.rstrip(b'=').decode()}", len(content)))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    members[f"{dist_info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            info = zipfile.ZipInfo(name)
            if name == executable:
                info.external_attr = 0o100755 << 16
            archive.writestr(info, content)
    raw = directory / raw_filename(VERSION, target_class)
    raw.write_bytes(members[executable])
    raw.chmod(0o755)
    return wheel


def _artifact_set(directory: Path) -> None:
    for target_class in TARGETS:
        _make_wheel(directory, target_class)


def test_raw_inspector_enforces_o008_name_mode_and_architecture(tmp_path: Path) -> None:
    path = tmp_path / raw_filename(VERSION, "linux-x86_64")
    path.write_bytes(_native(62, "elf"))
    path.chmod(0o755)
    result = inspect_raw(path, expected_version=VERSION, target_class="linux-x86_64")
    assert result.rust_target == "x86_64-unknown-linux-gnu"
    path.chmod(0o644)
    with pytest.raises(RawInspectionError, match="executable mode"):
        inspect_raw(path, expected_version=VERSION, target_class="linux-x86_64")


def test_manifest_binds_every_supported_target_and_payload_hash(tmp_path: Path) -> None:
    _artifact_set(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest = create_manifest(tmp_path, manifest_path)
    assert manifest["manifest_version"] == "release-manifest.v1"
    assert [record["product_target"] for record in manifest["artifacts"]] == sorted(
        TARGETS
    )
    assert (
        validate_manifest(manifest_path, tmp_path)["qualification_result"] == "pending"
    )


def test_manifest_rejects_unqualified_artifact(tmp_path: Path) -> None:
    _artifact_set(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    create_manifest(tmp_path, manifest_path)
    unsupported = tmp_path / "eggpool-0.8.0-windows-x86_64"
    unsupported.write_bytes(b"not a supported release asset")
    with pytest.raises(ValidationError, match="unsupported or unmanifested"):
        validate_manifest(manifest_path, tmp_path)


def test_target_matrix_keeps_only_the_frozen_platform_tags() -> None:
    assert TARGET_PLATFORMS["linux-x86_64"] == frozenset(
        {"manylinux2014_x86_64", "manylinux_2_17_x86_64"}
    )
    assert TARGET_PLATFORMS["linux-aarch64"] == frozenset(
        {"manylinux2014_aarch64", "manylinux_2_17_aarch64"}
    )
