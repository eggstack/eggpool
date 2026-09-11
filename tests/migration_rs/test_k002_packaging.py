"""Focused contract tests for the K002 Rust wheel publication boundary."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.inspect_cutover_wheel import WheelInspectionError, inspect_wheel

ROOT = Path(__file__).parents[2]
PUBLICATION_MANIFEST = ROOT / "packaging/pypi/pyproject.toml"
WHEEL_DIR = ROOT / "dist/cutover"


def _fake_wheel(path: Path, *, platform_tag: str = "macosx_11_0_arm64") -> None:
    binary = b"\xcf\xfa\xed\xfe" + (0x0100000C).to_bytes(4, "little") + b"native"
    dist_info = "eggpool-0.8.0.dist-info"
    executable = "eggpool-0.8.0.data/scripts/eggpool"
    metadata = (
        b"Metadata-Version: 2.3\n"
        b"Name: eggpool\n"
        b"Version: 0.8.0\n"
        b"Summary: Native EggPool proxy\n"
        b"Requires-Python: >=3.11\n"
        b"License-File: LICENSE\n\n"
    )
    wheel = (
        b"Wheel-Version: 1.0\n"
        b"Generator: maturin (1.14.1)\n"
        b"Root-Is-Purelib: false\n" + f"Tag: cp311-abi3-{platform_tag}\n\n".encode()
    )
    members = {
        f"{dist_info}/METADATA": metadata,
        f"{dist_info}/WHEEL": wheel,
        f"{dist_info}/licenses/LICENSE": b"MIT License\n",
        executable: binary,
    }
    record = io.StringIO()
    writer = csv.writer(record, lineterminator="\n")
    for name, content in members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
        writer.writerow((name, f"sha256={digest.rstrip(b'=').decode()}", len(content)))
    writer.writerow((f"{dist_info}/RECORD", "", ""))
    members[f"{dist_info}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            info = zipfile.ZipInfo(name)
            if name == executable:
                info.external_attr = 0o100755 << 16
            archive.writestr(info, content)


def test_publication_manifest_is_a_pinned_binary_wheel_definition() -> None:
    with PUBLICATION_MANIFEST.open("rb") as handle:
        manifest = tomllib.load(handle)
    assert manifest["project"] == {
        "name": "eggpool",
        "dynamic": ["version"],
        "description": (
            "A lightweight proxy that aggregates multiple LLM provider accounts "
            "behind an OpenAI Chat Completions-compatible endpoint"
        ),
        "readme": "README.md",
        "requires-python": ">=3.11",
        "license": {"file": "LICENSE"},
        "authors": [{"name": "David Bowman", "email": "dbowman91@proton.me"}],
        "keywords": [
            "llm",
            "proxy",
            "openai",
            "anthropic",
            "aggregation",
            "router",
            "multi-account",
            "opencode",
        ],
        "classifiers": [
            "Development Status :: 4 - Beta",
            "Intended Audience :: System Administrators",
            "License :: OSI Approved :: MIT License",
            "Operating System :: MacOS",
            "Operating System :: POSIX :: Linux",
            "Programming Language :: Rust",
            "Topic :: System :: Monitoring",
        ],
        "urls": {
            "Homepage": "https://github.com/eggstack/eggpool",
            "Repository": "https://github.com/eggstack/eggpool",
            "Issues": "https://github.com/eggstack/eggpool/issues",
            "Documentation": "https://github.com/eggstack/eggpool/tree/main/docs",
            "Changelog": "https://github.com/eggstack/eggpool/blob/main/CHANGELOG.md",
        },
    }
    assert manifest["build-system"] == {
        "requires": ["maturin==1.14.1"],
        "build-backend": "maturin",
    }
    assert manifest["tool"]["maturin"] == {
        "bindings": "bin",
        "manifest-path": "../../rust/Cargo.toml",
        "locked": True,
        "compatibility": "pypi",
        "strip": False,
    }


def test_root_python_oracle_packaging_is_unchanged() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        root_manifest = tomllib.load(handle)
    root_project = root_manifest["project"]
    with (ROOT / "rust/Cargo.toml").open("rb") as handle:
        cargo_project = tomllib.load(handle)["package"]
    assert root_project["version"] == "0.7.4"
    assert root_project["scripts"]["eggpool"] == "eggpool.cli:main"
    assert root_project["dependencies"]
    assert root_manifest["tool"]["eggpool"] == {
        "project_role": "historical-development-only",
        "current_runtime": "rust",
        "publication_manifest": "packaging/pypi/pyproject.toml",
        "historical_artifacts": "immutable-external-pypi",
        "requires_python_semantics": "package-manager-compatibility-only",
    }
    assert cargo_project["version"] == "0.8.0"


def test_inspector_accepts_a_native_platform_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "eggpool-0.8.0-cp311-abi3-macosx_11_0_arm64.whl"
    _fake_wheel(wheel)
    result = inspect_wheel(wheel, expected_version="0.8.0", target_class="macos-arm64")
    assert result.executable_member == "eggpool-0.8.0.data/scripts/eggpool"
    assert result.dependencies == ()


def test_inspector_rejects_universal_or_wrong_target_wheels(tmp_path: Path) -> None:
    wheel = tmp_path / "eggpool-0.8.0-py3-none-any.whl"
    _fake_wheel(wheel, platform_tag="any")
    with pytest.raises(WheelInspectionError, match="platform tag"):
        inspect_wheel(wheel, expected_version="0.8.0", target_class="macos-arm64")


def test_built_wheel_obeys_the_contract_when_available() -> None:
    wheels = list(WHEEL_DIR.glob("*.whl"))
    if not wheels:
        pytest.skip("run the K002 build before artifact-level inspection")
    result = inspect_wheel(
        wheels[0], expected_version="0.8.0", target_class="macos-arm64"
    )
    assert result.wheel_size < 100_000_000
