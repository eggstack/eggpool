"""Integration tests for the ``deploy`` CLI command group.

Verifies that:

- ``deploy systemd`` prints the dynamic personal-use unit and the
  production snippet, plus an auto-install hint.
- ``deploy logrotate`` prints the logrotate config and auto-install hint.
- ``deploy cron`` prints the dynamic backup script and auto-install hint.
- ``deploy all`` emits every snippet in sequence.
- The bundled assets stay in sync with the canonical files in ``deploy/``.
- ``--install`` flag is rejected when not running as root.

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

    def test_prints_personal_and_production_snippets(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_path), "deploy", "systemd"])

        assert result.exit_code == 0, result.output
        # Personal-use header
        assert "EggPool systemd unit (personal use)" in result.output
        # Production snippet header
        assert "Production snippet" in result.output
        # Dynamic content: ExecStart should reference the actual config path
        assert str(config_path) in result.output
        # Personal-use unit does NOT have User=eggpool
        assert "User=eggpool" not in result.output.split("Production snippet")[0]
        # Production unit DOES have User=eggpool
        assert "User=eggpool" in result.output
        # Install hint
        assert "eggpool deploy systemd --install" in result.output

    def test_prints_install_steps(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_path), "deploy", "systemd"])

        assert result.exit_code == 0, result.output
        assert "sudo systemctl daemon-reload" in result.output
        assert "sudo systemctl enable eggpool" in result.output
        assert "sudo systemctl start eggpool" in result.output
        assert "sudo systemctl status eggpool" in result.output
        # Installable copy-paste block
        assert "sudo tee" in result.output
        assert "EGGPOOL_EOF" in result.output

    def test_exits_zero_without_config_file(self, tmp_path: Path) -> None:
        """deploy systemd does not need a config file to print its snippet."""
        missing = tmp_path / "missing.toml"

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(missing), "deploy", "systemd"])

        assert result.exit_code == 0
        assert "EggPool systemd unit (personal use)" in result.output

    def test_install_flag_requires_root(self, tmp_path: Path) -> None:
        """--install must be rejected when not running as root."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(config_path), "deploy", "systemd", "--install"]
        )

        # Should fail because we're not root (geteuid != 0)
        assert result.exit_code != 0 or "requires root" in result.output.lower()


class TestDeployLogrotate:
    """Verify ``deploy logrotate`` CLI output."""

    def test_prints_conf_and_install_hint(self, tmp_path: Path) -> None:
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
        # Install hint
        assert "eggpool deploy logrotate --install" in result.output


class TestDeployCron:
    """Verify ``deploy cron`` CLI output."""

    def test_prints_dynamic_script_and_hint(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_path), "deploy", "cron"])

        assert result.exit_code == 0, result.output
        # Personal-use header
        assert "EggPool cron setup (personal use)" in result.output
        # Dynamic content: config path should appear in the script
        assert str(config_path) in result.output
        # Production snippets also shown
        assert "Production snippet" in result.output
        assert CRON_BACKUP_SCRIPT in result.output
        assert CRON_BACKUP_FILE in result.output
        # Install hint
        assert "eggpool deploy cron --install" in result.output


class TestDeployAll:
    """Verify ``deploy all`` emits every snippet."""

    def test_emits_all_sections(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(cli, ["--config", str(config_path), "deploy", "all"])

        assert result.exit_code == 0, result.output
        # Each subsection header must appear
        assert "EggPool systemd unit (personal use)" in result.output
        assert "EggPool logrotate configuration" in result.output
        assert "EggPool cron setup (personal use)" in result.output
        # Production snippets
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

    def test_install_flag_passes_through(self, tmp_path: Path) -> None:
        """--install on deploy all should be passed to each subcommand."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\napi_key = "ep_test"\nport = 8080\n')

        runner = CliRunner()
        result = runner.invoke(
            cli, ["--config", str(config_path), "deploy", "all", "--install"]
        )

        # Should fail because we're not root
        assert result.exit_code != 0 or "requires root" in result.output.lower()


class TestDynamicSnippets:
    """Verify that dynamic snippet builders produce correct content."""

    def test_systemd_unit_uses_provided_paths(self) -> None:
        from eggpool.deploy import build_personal_systemd_unit

        unit = build_personal_systemd_unit(
            binary_path="/home/test/.local/bin/eggpool",
            config_path="/home/test/config.toml",
            data_dir="/home/test/.local/share/eggpool",
            env_path="/home/test/.env",
        )
        expected_exec = (
            "ExecStart=/home/test/.local/bin/eggpool"
            " --config /home/test/config.toml serve"
        )
        assert expected_exec in unit
        assert "WorkingDirectory=/home/test/.local/share/eggpool" in unit
        assert "EnvironmentFile=/home/test/.env" in unit
        assert "User=" not in unit

    def test_systemd_unit_without_env(self) -> None:
        from eggpool.deploy import build_personal_systemd_unit

        unit = build_personal_systemd_unit(
            binary_path="/usr/bin/eggpool",
            config_path="/etc/eggpool/config.toml",
            data_dir="/var/lib/eggpool",
            env_path=None,
        )
        assert "EnvironmentFile=" not in unit

    def test_backup_script_uses_provided_paths(self) -> None:
        from eggpool.deploy import build_personal_backup_script

        script = build_personal_backup_script(
            config_path="/home/test/config.toml",
            db_path="/home/test/.local/share/eggpool/usage.sqlite3",
        )
        assert 'CONFIG_PATH="/home/test/config.toml"' in script
        assert 'DB_PATH="/home/test/.local/share/eggpool/usage.sqlite3"' in script
        assert "sudo -u eggpool" not in script
