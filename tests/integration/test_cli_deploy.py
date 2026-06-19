"""Integration tests for the ``deploy`` CLI command group.

Verifies that:

- ``deploy systemd`` prints the bundled systemd unit and the canonical
  install instructions.
- ``deploy logrotate`` prints the bundled logrotate config and the
  canonical install instructions.
- ``deploy cron`` prints both the cron entry and the backup script.
- ``deploy all`` emits every snippet in sequence.
- The bundled assets stay in sync with the canonical files in ``deploy/``.

Each test invokes the CLI via ``click.testing.CliRunner`` so no real
clipboard / filesystem side effects occur.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from eggpool.cli import cli
from eggpool.deploy import (
    CRON_BACKUP_FILE,
    CRON_BACKUP_SCRIPT,
    LOGROTATE_CONF,
    SYSTEMD_UNIT,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestDeployBundledAssets:
    """Bundled assets must match the canonical files under ``deploy/``."""

    def test_systemd_matches_repo_file(self) -> None:
        repo_unit = (REPO_ROOT / "deploy" / "eggpool.service").read_text(
            encoding="utf-8"
        )
        assert repo_unit == SYSTEMD_UNIT

    def test_logrotate_matches_repo_file(self) -> None:
        repo_conf = (REPO_ROOT / "deploy" / "eggpool-logrotate.conf").read_text(
            encoding="utf-8"
        )
        assert repo_conf == LOGROTATE_CONF

    def test_systemd_unit_is_valid_ini_shape(self) -> None:
        """Sanity check: the unit has the required systemd sections."""
        for section in ("[Unit]", "[Service]", "[Install]"):
            assert section in SYSTEMD_UNIT
        assert "ExecStart=/opt/eggpool/.venv/bin/eggpool" in SYSTEMD_UNIT
        assert "EnvironmentFile=/etc/eggpool/env" in SYSTEMD_UNIT

    def test_cron_backup_file_is_valid_cron(self) -> None:
        """Cron entry must have 5 positional fields plus user + command."""
        lines = [line for line in CRON_BACKUP_FILE.splitlines() if line.strip()]
        schedule_lines = [line for line in lines if not line.startswith("#")]
        assert len(schedule_lines) == 1
        parts = schedule_lines[0].split()
        assert len(parts) == 7
        # 5 schedule fields, user, command
        assert parts[5] == "root"
        assert parts[6] == "/usr/local/bin/eggpool-backup"

    def test_cron_backup_script_has_safety_directives(self) -> None:
        """Backup script must be fail-safe."""
        assert "set -euo pipefail" in CRON_BACKUP_SCRIPT
        assert "/usr/local/bin/eggpool-backup" in CRON_BACKUP_SCRIPT
        assert ".backup" in CRON_BACKUP_SCRIPT


class TestDeploySystemd:
    """Verify ``deploy systemd`` CLI output."""

    def test_prints_unit_and_install_steps(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_path), "deploy", "systemd"])

        assert result.exit_code == 0, result.output
        # Header
        assert "EggPool systemd unit" in result.output
        # Install target
        assert "/etc/systemd/system/eggpool.service" in result.output
        # Canonical follow-up commands
        assert "sudo systemctl daemon-reload" in result.output
        assert "sudo systemctl enable eggpool" in result.output
        assert "sudo systemctl start eggpool" in result.output
        assert "sudo systemctl status eggpool" in result.output
        # The bundled unit must appear in full
        assert SYSTEMD_UNIT in result.output
        # Installable copy-paste block
        assert "sudo tee" in result.output
        assert "EGGPOOL_EOF" in result.output

    def test_exits_zero_without_config_file(self, tmp_path: Path) -> None:
        """deploy systemd does not need a config file to print its snippet."""
        missing = tmp_path / "missing.toml"

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(missing), "deploy", "systemd"])

        assert result.exit_code == 0
        assert "EggPool systemd unit" in result.output


class TestDeployLogrotate:
    """Verify ``deploy logrotate`` CLI output."""

    def test_prints_conf_and_verify_step(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(config_path), "deploy", "logrotate"]
        )

        assert result.exit_code == 0, result.output
        assert "EggPool logrotate configuration" in result.output
        assert "/etc/logrotate.d/eggpool" in result.output
        assert "sudo logrotate -d" in result.output
        assert LOGROTATE_CONF in result.output
        assert "sudo tee" in result.output
        assert "EGGPOOL_EOF" in result.output


class TestDeployCron:
    """Verify ``deploy cron`` CLI output."""

    def test_prints_backup_script_and_cron_entry(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_path), "deploy", "cron"])

        assert result.exit_code == 0, result.output
        # Both deliverables
        assert "/usr/local/bin/eggpool-backup" in result.output
        assert "/etc/cron.d/eggpool-backup" in result.output
        # Both snippets in full
        assert CRON_BACKUP_SCRIPT in result.output
        assert CRON_BACKUP_FILE in result.output
        # Required post-install step
        assert "sudo chmod +x /usr/local/bin/eggpool-backup" in result.output


class TestDeployAll:
    """Verify ``deploy all`` emits every snippet."""

    def test_emits_all_sections(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_path), "deploy", "all"])

        assert result.exit_code == 0, result.output
        # Each subsection header must appear
        assert "EggPool systemd unit" in result.output
        assert "EggPool logrotate configuration" in result.output
        assert "EggPool automated backup via cron" in result.output
        # Each asset must appear in full
        assert SYSTEMD_UNIT in result.output
        assert LOGROTATE_CONF in result.output
        assert CRON_BACKUP_SCRIPT in result.output
        assert CRON_BACKUP_FILE in result.output

    def test_does_not_overwrite_config(self, tmp_path: Path) -> None:
        """deploy all must not modify the user's config file."""
        config_path = tmp_path / "config.toml"
        original = '[server]\napi_key = "ep_keep_me"\nport = 8080\n'
        config_path.write_text(original)

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_path), "deploy", "all"])

        assert result.exit_code == 0
        assert config_path.read_text() == original
