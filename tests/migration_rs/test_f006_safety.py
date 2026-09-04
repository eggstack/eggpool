"""Black-box closure coverage for the F006 startup and serve contracts."""

from __future__ import annotations

import socket
import sqlite3
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from tests.migration_rs.harness import (
    Implementation,
    PythonLauncher,
    RustLauncher,
    allocate_tcp_port,
    capture_startup_state,
    isolated_environment,
    wait_for_tcp,
)


def _write_config(
    root: Path,
    port: int,
    *,
    database: Path | None = None,
    account: bool = False,
    threads: int = 1,
) -> Path:
    database = database or root / "server.sqlite3"
    account_config = (
        """
[providers.fixture]
id = "fixture"
base_url = "https://provider.example.test"
protocols = ["openai"]

[providers.fixture.auth]
mode = "none"

[[providers.fixture.accounts]]
name = "fixture-account"
api_key_env = "FIXTURE_API_KEY"
"""
        if account
        else ""
    )
    config = root / "server.toml"
    config.write_text(
        f"""[server]
host = "127.0.0.1"
port = {port}
threads = {threads}
api_key = "server-key"

[database]
path = "{database}"

[dashboard]
enabled = false

[models]
startup_refresh = false

[model_info]
enabled = false
startup_refresh = false
{account_config}
""",
        encoding="utf-8",
    )
    return config


def _assert_port_reusable(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))


def _rust_or_skip() -> RustLauncher:
    rust = RustLauncher()
    if not rust.identity.executable.is_file():
        pytest.skip("Rust candidate is not built")
    return rust


def test_bind_rejection_does_not_create_nonexistent_database() -> None:
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        occupied_root = environment.implementation_root(Implementation.RUST)
        target_root = environment.root / "target"
        target_root.mkdir()
        port = allocate_tcp_port()
        occupied_config = _write_config(occupied_root, port)
        target_config = _write_config(target_root, port)
        target_database = target_root / "server.sqlite3"
        assert not target_database.exists()

        with rust.spawn(
            ["--config", str(occupied_config), "serve", "--verbose"],
            environment=environment,
        ) as occupier:
            wait_for_tcp("127.0.0.1", port)
            result = rust.run(
                ["--config", str(target_config), "serve", "--verbose"],
                environment=environment,
                timeout=5,
            )
            assert result.exit_code == 1
            assert "cannot bind listener" in result.stderr
            assert not target_database.exists()
            assert occupier.process.poll() is None


def test_bind_rejection_preserves_existing_migration_and_account_state() -> None:
    rust = _rust_or_skip()
    python = PythonLauncher()
    with isolated_environment() as environment:
        fixture_root = environment.root / "python-fixture"
        fixture_root.mkdir()
        target_database = fixture_root / "server.sqlite3"
        fixture_config = _write_config(
            fixture_root,
            allocate_tcp_port(),
            database=target_database,
            account=True,
        )
        migration = python.run(
            ["--config", str(fixture_config), "migrate"], environment=environment
        )
        assert migration.exit_code == 0, migration.stderr
        with sqlite3.connect(target_database) as connection:
            connection.execute(
                "INSERT INTO accounts "
                "(name, api_key_env, enabled, weight, provider_id) "
                "VALUES (?, ?, ?, ?, ?)",
                ("fixture-account", "FIXTURE_API_KEY", 1, 1.0, "fixture"),
            )
            connection.commit()
        before = capture_startup_state(target_database)
        assert len(before["migrations"]) == 54
        assert before["accounts"]

        occupier_root = environment.root / "rust-occupier"
        occupier_root.mkdir()
        port = allocate_tcp_port()
        occupier_config = _write_config(occupier_root, port)
        target_config = _write_config(
            fixture_root, port, database=target_database, account=True
        )
        with rust.spawn(
            ["--config", str(occupier_config), "serve", "--verbose"],
            environment=environment,
        ) as occupier:
            wait_for_tcp("127.0.0.1", port)
            result = rust.run(
                ["--config", str(target_config), "serve", "--verbose"],
                environment=environment,
                timeout=5,
            )
            assert result.exit_code == 1
            assert "cannot bind listener" in result.stderr
            assert capture_startup_state(target_database) == before
            assert occupier.process.poll() is None


def test_post_bind_database_failure_releases_listener() -> None:
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        root = environment.implementation_root(Implementation.RUST)
        port = 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        database_directory = root / "database-directory"
        database_directory.mkdir()
        config = _write_config(root, port, database=database_directory)

        result = rust.run(
            ["--config", str(config), "serve", "--verbose"],
            environment=environment,
            timeout=5,
        )
        assert result.exit_code == 1
        assert not result.timed_out
        assert "Rust server failed" in result.stderr
        _assert_port_reusable(port)


@pytest.mark.parametrize(
    ("args", "marker"),
    [
        (("serve",), "deferred daemon mode"),
        (("serve", "--verbose", "--log-file", "candidate.log"), "--log-file"),
        (("serve", "--verbose", "--quiet"), "--quiet"),
        (("serve", "--verbose", "--as-root"), "--as-root"),
    ],
)
def test_unsupported_serve_modes_fail_before_state_side_effects(
    args: tuple[str, ...], marker: str
) -> None:
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        root = environment.implementation_root(Implementation.RUST)
        config = _write_config(root, 0)
        database = root / "server.sqlite3"
        result = rust.run(
            ["--config", str(config), *args], environment=environment, timeout=5
        )
        assert result.exit_code == 1
        assert marker in result.stderr
        assert not database.exists()
        assert not (root / "candidate.log").exists()


def test_non_default_threads_are_accepted_but_report_staged_runtime() -> None:
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        root = environment.implementation_root(Implementation.RUST)
        port = allocate_tcp_port()
        config = _write_config(root, port, threads=2)
        server = rust.spawn(
            ["--config", str(config), "serve", "--verbose"],
            environment=environment,
        )
        try:
            wait_for_tcp("127.0.0.1", port)
        finally:
            server.stop()
        stdout, stderr = server.process.communicate(timeout=5)
        text = (stdout + stderr).decode("utf-8", errors="replace")
        assert "server.threads is accepted for config compatibility" in text
        assert "single-threaded" in text
