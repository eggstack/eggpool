"""Tests for the init-config CLI command."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from eggpool.cli import cli


class TestInitConfig:
    def test_init_config_creates_config_file(self, tmp_path: Path) -> None:
        """init-config creates a config.toml file in the current directory."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["init-config"])
            assert result.exit_code == 0
            assert Path("config.toml").exists()
            assert "Config written to config.toml" in result.output

    def test_init_config_with_target(self, tmp_path: Path) -> None:
        """init-config with TARGET creates the file at the specified path."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            target = tmp_path / "my-config.toml"
            result = runner.invoke(cli, ["init-config", str(target)])
            assert result.exit_code == 0
            assert target.exists()
            assert f"Config written to {target}" in result.output

    def test_init_config_fails_if_exists_without_force(self, tmp_path: Path) -> None:
        """init-config fails with warning if config.toml already exists."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("config.toml").write_text("existing content")
            result = runner.invoke(cli, ["init-config"])
            assert result.exit_code == 1
            assert "already exists" in result.output
            assert "eggpool onboard" in result.output

    def test_init_config_overwrites_with_force(self, tmp_path: Path) -> None:
        """init-config with --force overwrites existing config.toml."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("config.toml").write_text("existing content")
            result = runner.invoke(cli, ["init-config", "--force"])
            assert result.exit_code == 0
            assert Path("config.toml").exists()
            # Verify it's not the original content
            assert Path("config.toml").read_text() != "existing content"
