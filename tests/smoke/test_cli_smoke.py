"""Smoke: CLI help, check-config validation, and help success."""

from __future__ import annotations

import os

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


def test_cli_check_config_valid(tmp_path: object) -> None:
    """Valid temporary config file passes check-config."""
    os.environ.setdefault("SMOKE_CFG_KEY", "smoke-cfg-key")
    config_content = (
        "[server]\n"
        'api_key_env = "SMOKE_CFG_KEY"\n'
        'host = "127.0.0.1"\n'
        "port = 11300\n"
        "\n"
        "[database]\n"
        'path = ":memory:"\n'
        "\n"
        "[upstream]\n"
        'base_url = "https://smoke.example.com"\n'
        "\n"
        "[[accounts]]\n"
        'name = "smoke"\n'
        'api_key_env = "SMOKE_CFG_KEY"\n'
    )
    config_path = str(tmp_path / "valid.toml")  # type: ignore[union-attr]
    with open(config_path, "w") as f:
        f.write(config_content)
    runner = CliRunner()
    result = runner.invoke(cli, ["--config", config_path, "check-config"])
    assert result.exit_code == 0, f"check-config failed: {result.output}"
    assert "successfully" in result.output.lower() or "loaded" in result.output.lower()
