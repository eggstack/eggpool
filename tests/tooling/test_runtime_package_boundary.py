"""package boundary Rust production authority and historical-package boundary tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.validate_runtime_package_boundary import (
    PackageBoundaryError,
    validate_package_boundary,
)

ROOT = Path(__file__).parents[2]


def test_current_publication_has_one_rust_authority() -> None:
    result = validate_package_boundary()
    assert result == {
        "status": "pass",
        "current_runtime": "rust",
        "publication_manifest": "packaging/pypi/pyproject.toml",
        "historical_python_version": "0.7.4",
        "native_release_version": "0.8.0",
    }


def test_release_workflow_rejects_root_python_build_paths() -> None:
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    with pytest.raises(PackageBoundaryError, match="root/historical Python"):
        validate_package_boundary(
            workflow_text=workflow.replace(
                "packaging/pypi/pyproject.toml", "pyproject.toml", 1
            ).replace("validate_runtime_package_boundary.py", "uv build", 1)
        )


def test_current_release_manifest_declares_no_python_runtime_dependency() -> None:
    text = (ROOT / "packaging/pypi/pyproject.toml").read_text(encoding="utf-8")
    assert 'publication_role = "current-rust-production"' in text
    assert 'requires_python_semantics = "package-manager-compatibility-only"' in text
