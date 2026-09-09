"""O010 black-box qualification for the complete Rust command tree."""

from __future__ import annotations

import pytest

from tests.migration_rs.harness import (
    PythonLauncher,
    RustLauncher,
    assert_distinct_implementations,
    isolated_environment,
)
from tests.migration_rs.operations_fixtures import fixture_command_inventory


def _launchers() -> tuple[PythonLauncher, RustLauncher]:
    python = PythonLauncher()
    rust = RustLauncher()
    if not rust.identity.executable.is_file():
        pytest.skip("Rust candidate is not built")
    assert_distinct_implementations(python, rust)
    return python, rust


def test_every_frozen_command_path_has_a_real_rust_help_dispatch() -> None:
    """Exercise every documented path through the real two-sided launchers."""

    python, rust = _launchers()
    with isolated_environment() as environment:
        for row in fixture_command_inventory():
            args = [*row["path"].split(), "--help"]
            python_result = python.run(args, environment=environment)
            rust_result = rust.run(args, environment=environment)
            if row["path"] in {"croncheck", "ensure-running"}:
                # Python's stdlib-only watchdog fast path consumes these
                # commands before Click and therefore has no help probe.
                assert rust_result.exit_code == 0, row["path"]
            else:
                assert python_result.exit_code == rust_result.exit_code == 0, row[
                    "path"
                ]
            assert not rust_result.timed_out, row["path"]
            assert (
                "not implemented"
                not in (rust_result.stdout + rust_result.stderr).lower()
            )


def test_version_is_an_exact_two_sided_read_only_observation() -> None:
    python, rust = _launchers()
    with isolated_environment() as environment:
        python_result = python.run(["version"], environment=environment)
        rust_result = rust.run(["version"], environment=environment)
    assert (rust_result.exit_code, rust_result.stdout, rust_result.stderr) == (
        python_result.exit_code,
        python_result.stdout,
        python_result.stderr,
    )
