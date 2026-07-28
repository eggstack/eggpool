"""Smoke: CLI help and check-config invocation."""

from __future__ import annotations

from click.testing import CliRunner

from eggpool.cli_full import cli


def test_cli_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "eggpool" in result.output.lower() or "Usage" in result.output


def test_cli_check_config_missing() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["check-config", "--config", "/nonexistent/path.toml"])
    assert result.exit_code != 0
