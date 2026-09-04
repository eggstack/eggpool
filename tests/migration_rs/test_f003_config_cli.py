"""F003 differential coverage for config loading and CLI surface parity."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from tests.migration_rs.harness import (
    PythonLauncher,
    RustLauncher,
    capture_config,
    isolated_environment,
)


def _launchers() -> tuple[PythonLauncher, RustLauncher]:
    python = PythonLauncher()
    rust = RustLauncher()
    if not rust.identity.executable.is_file():
        pytest.skip("Rust candidate is not built")
    return python, rust


def test_version_and_deferred_command_are_explicit() -> None:
    python, rust = _launchers()
    with isolated_environment() as environment:
        python_version = python.run(["version"], environment=environment)
        rust_version = rust.run(["version"], environment=environment)
        deferred = rust.run(["migrate"], environment=environment)

    assert (python_version.exit_code, python_version.stdout, python_version.stderr) == (
        0,
        rust_version.stdout,
        rust_version.stderr,
    )
    assert rust_version.exit_code == 0
    assert deferred.exit_code == 1
    assert "not implemented in Rust candidate" in deferred.stderr


def test_representative_config_is_accepted_and_invalid_config_is_rejected(
    tmp_path: Path,
) -> None:
    python, rust = _launchers()
    config_path = tmp_path / "representative.toml"
    config_path.write_text(
        """[server]
host = "127.0.0.1"
port = 11301
api_key_env = "SERVER_API_KEY"

[database]
path = "usage.sqlite3"

[proxies.egress]
url = "http://proxy.example.test:8080"

[providers.edge]
id = "edge"
base_url = "https://provider.example.test/v1"
protocols = ["openai", "anthropic"]
routing_priority = 2

[providers.edge.auth]
mode = "bearer"

[[providers.edge.headers]]
name = "X-Client"
value = "eggpool"

[providers.edge.wire_surfaces.openai_chat_completions]
path_template = "/chat/completions"

[providers.edge.wire_surfaces.anthropic_messages]
path_template = "/messages"

[[providers.edge.accounts]]
name = "primary"
api_key = "test-key-not-for-production"

[model_routers.smart]
selector_model = "selector-model"
default_model = "gpt-4o"
sticky = false

[model_routers.smart.routes.fast]
model = "gpt-4o"
description = "Fast route"

[transcoder]
loss_policy = "reject"

[model_info]
enabled = false
startup_refresh = false
""",
        encoding="utf-8",
    )
    with isolated_environment() as environment:
        python_result = capture_config(python, config_path, environment)
        rust_result = capture_config(rust, config_path, environment)
    assert python_result.valid and rust_result.valid
    assert python_result.error_category is None
    assert rust_result.error_category is None

    invalid_path = tmp_path / "invalid.toml"
    invalid_path.write_text("[server]\nport = 70000\n", encoding="utf-8")
    with isolated_environment() as environment:
        python_invalid = capture_config(python, invalid_path, environment)
        rust_invalid = capture_config(rust, invalid_path, environment)
    assert not python_invalid.valid and not rust_invalid.valid
    assert python_invalid.command.exit_code == rust_invalid.command.exit_code == 1
    assert rust_invalid.error_category == "schema"


def test_config_path_precedence_and_secret_safe_errors(tmp_path: Path) -> None:
    _python, rust = _launchers()
    env_path = tmp_path / "from-env.toml"
    cli_path = tmp_path / "from-cli.toml"
    for path, port in ((env_path, 11302), (cli_path, 11303)):
        path.write_text(f"[server]\nport = {port}\n", encoding="utf-8")

    with isolated_environment() as environment:
        result = rust.run(
            ["--config", str(cli_path), "check-config"], environment=environment
        )
        assert result.exit_code == 0
        assert "Server: 127.0.0.1:11303" in result.stdout
        assert "test-key-not-for-production" not in result.stdout + result.stderr


def test_parser_inventory_and_error_exit_codes() -> None:
    _python, rust = _launchers()
    expected_commands = {
        "serve",
        "connect",
        "logout",
        "check-config",
        "edit",
        "getkey",
        "newkey",
        "configsetup",
        "deploy",
        "accounts",
        "dashboard",
        "db",
        "models",
        "modelinfo",
        "stats",
        "onboard",
        "croncheck",
        "ensure-running",
        "migrate",
        "stop",
        "restart",
        "init-config",
        "help",
        "recover",
        "uninstall",
        "update",
        "set",
        "rehash",
        "runtime-status",
        "backup",
        "version",
    }
    with isolated_environment() as environment:
        help_result = rust.run(["--help"], environment=environment)
        unknown = rust.run(["unknown-command"], environment=environment)
        missing = rust.run(["accounts", "explain", "--model"], environment=environment)
    assert help_result.exit_code == 0
    assert expected_commands <= set(help_result.stdout.split())
    assert unknown.exit_code == 2
    assert missing.exit_code == 2
    assert "error" in (unknown.stderr + unknown.stdout).lower()


def test_parser_option_inventory_is_present_in_both_candidates() -> None:
    python, rust = _launchers()
    inventory = {
        ("serve",): ("--verbose", "--log-file", "--quiet", "--as-root"),
        ("connect",): ("--providers",),
        ("logout",): ("[TARGET]",),
        ("newkey",): ("--show-old",),
        ("configsetup", "aider"): (
            "--print-secret",
            "--no-clipboard",
            "--force",
            "--output",
            "--write",
            "--model",
            "--base-url",
            "--host",
        ),
        ("deploy", "all"): ("--install",),
        ("deploy", "backup-cron"): (
            "--install",
            "--uninstall",
            "--production",
            "--user",
        ),
        ("deploy", "cron"): ("--install", "--uninstall", "--interval", "--user"),
        ("deploy", "systemd"): ("--install", "--production", "--as-root"),
        ("accounts", "explain"): (
            "--model",
            "--provider",
            "--protocol",
            "--scores",
            "--gates",
        ),
        ("modelinfo", "refresh"): ("--provider-catalog-only",),
        ("stats", "explain-dashboard"): (
            "--period",
            "--bucket",
            "--group-by",
            "--json",
        ),
        ("stats", "repair-costs"): (
            "--provider",
            "--since",
            "--dry-run",
            "--limit",
        ),
        ("uninstall",): (
            "--yes",
            "--keep-data",
            "--keep-config",
            "--keep-path",
            "--deploy-artifacts",
        ),
        ("update",): ("[REQUESTED_VERSION]", "--check", "--from-source"),
    }
    with isolated_environment() as environment:
        for command, expected in inventory.items():
            python_help = python.run([*command, "--help"], environment=environment)
            rust_help = rust.run([*command, "--help"], environment=environment)
            assert python_help.exit_code == rust_help.exit_code == 0, command
            combined = rust_help.stdout + rust_help.stderr
            for token in expected:
                assert token in combined, (command, token)


def test_credential_validation_uses_environment_without_echoing_value(
    tmp_path: Path,
) -> None:
    _python, rust = _launchers()
    config_path = tmp_path / "credentials.toml"
    config_path.write_text(
        """[providers.edge]
id = "edge"
base_url = "https://provider.example.test"

[[providers.edge.accounts]]
name = "primary"
api_key_env = "F003_SECRET"
""",
        encoding="utf-8",
    )
    with isolated_environment() as environment:
        result = rust.run(
            ["--config", str(config_path), "check-config"], environment=environment
        )
    assert result.exit_code == 1
    assert "credential is not set" in result.stderr
    assert "F003_SECRET" not in result.stderr
    assert "secret" not in result.stderr.lower()
