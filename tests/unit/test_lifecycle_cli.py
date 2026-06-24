"""Tests for the ``backup``, ``recover``, and ``uninstall`` CLI commands.

The heavy lifting lives in :mod:`eggpool.lifecycle`; these tests focus
on the Click plumbing: argument handling, output format, exit codes,
and the ``--yes`` flag.
"""

from __future__ import annotations

import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from eggpool.cli import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_text(db_path: str = "usage.sqlite3") -> str:
    return (
        "[server]\n"
        'host = "0.0.0.0"\n'
        "port = 11300\n"
        'api_key = "ep_test_key_1234567890"\n'
        "[database]\n"
        f'path = "{db_path}"\n'
        "[models]\n"
    )


# ---------------------------------------------------------------------------
# eggpool backup
# ---------------------------------------------------------------------------


class TestBackupCommand:
    def test_creates_backup_with_expected_contents(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
            config = Path("config.toml")
            config.write_text(_config_text())
            Path("usage.sqlite3").write_bytes(b"db-blob")
            backup_dir = Path("backups")
            backup_dir.mkdir()

            with patch("eggpool.cli._detect_install_method", return_value="pipx"):
                result = runner.invoke(
                    cli,
                    [
                        "--config",
                        str(config),
                        "backup",
                        "--output-dir",
                        str(backup_dir),
                    ],
                )

            assert result.exit_code == 0, result.output
            assert "Wrote backup:" in result.output
            archives = list(Path(fs).glob("**/eggpool-backup-*.zip"))
            assert archives, "no archive written"
            with zipfile.ZipFile(archives[0]) as zf:
                names = set(zf.namelist())
            assert "config.toml" in names
            assert "usage.sqlite3" in names

    def test_backup_includes_env_when_present(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("config.toml").write_text(_config_text())
            Path("usage.sqlite3").write_bytes(b"db")
            Path(".env").write_text("API_KEY=secret")

            with patch("eggpool.cli._detect_install_method", return_value="pipx"):
                result = runner.invoke(cli, ["backup"])

        assert result.exit_code == 0, result.output
        assert ".env" in result.output

    def test_backup_with_output_dir(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as fs:
            Path("config.toml").write_text(_config_text())
            Path("usage.sqlite3").write_bytes(b"db")
            custom = Path(fs) / "custom-backups"

            with patch("eggpool.cli._detect_install_method", return_value="pipx"):
                result = runner.invoke(
                    cli,
                    ["backup", "--output-dir", str(custom)],
                )

            assert result.exit_code == 0, result.output
            archives = list(custom.glob("eggpool-backup-*.zip"))
            assert archives

    def test_backup_errors_on_malformed_config(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            bad = Path("config.toml")
            bad.write_text("this is not valid TOML ====")

            result = runner.invoke(cli, ["backup"])

        assert result.exit_code == 1
        assert "Error" in result.output


# ---------------------------------------------------------------------------
# eggpool recover
# ---------------------------------------------------------------------------


class TestRecoverCommand:
    def test_recovers_from_explicit_path(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            config.write_text(_config_text())
            Path("usage.sqlite3").write_bytes(b"original-db")

            from eggpool.lifecycle.backup import (
                BackupContents,
                create_backup,
            )

            archive = create_backup(
                BackupContents(
                    config_path=config,
                    db_path=Path("usage.sqlite3"),
                    env_path=None,
                    install_method="pipx",
                ),
                output_dir=Path("backups"),
                now=datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC),
            )

            # Mutate the live files so the restore is observable.
            config.write_text("[server]\nport = 0\n")
            Path("usage.sqlite3").write_bytes(b"corrupted")

            result = runner.invoke(
                cli,
                ["recover", str(archive.resolve())],
                input="y\n",
            )

            assert result.exit_code == 0, result.output
            assert config.read_text() == _config_text()
            assert Path("usage.sqlite3").read_bytes() == b"original-db"

    def test_recovers_aborts_without_confirmation(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            config.write_text(_config_text())
            Path("usage.sqlite3").write_bytes(b"db")

            from eggpool.lifecycle.backup import (
                BackupContents,
                create_backup,
            )

            archive = create_backup(
                BackupContents(
                    config_path=config,
                    db_path=Path("usage.sqlite3"),
                    env_path=None,
                    install_method="pipx",
                ),
                output_dir=Path("backups"),
                now=datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC),
            )

            original_text = config.read_text()
            result = runner.invoke(
                cli,
                ["recover", str(archive.resolve())],
                input="n\n",
            )

            assert result.exit_code == 0
            assert "Aborted" in result.output
            assert config.read_text() == original_text

    def test_recover_missing_path_exits_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("config.toml").write_text(_config_text())
            result = runner.invoke(
                cli,
                ["recover", "/absolute/no-such.zip"],
            )

        assert result.exit_code == 1
        assert "not found" in result.output

    def test_recover_no_backups_lists_default_dir(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("config.toml").write_text(_config_text())
            with runner.isolated_filesystem():
                empty_backup_dir = tmp_path / "empty-backups"
                empty_backup_dir.mkdir()
                with patch.dict(os.environ, {"XDG_BACKUP_HOME": str(empty_backup_dir)}):
                    result = runner.invoke(cli, ["recover"])

        assert result.exit_code == 0
        assert "No backups found" in result.output
        assert "Default location" in result.output

    def test_recover_with_menu_picks_archive(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            config.write_text(_config_text())
            Path("usage.sqlite3").write_bytes(b"db")

            from eggpool.lifecycle.backup import (
                BackupContents,
                create_backup,
            )

            archive = create_backup(
                BackupContents(
                    config_path=config,
                    db_path=Path("usage.sqlite3"),
                    env_path=None,
                    install_method="pipx",
                ),
                output_dir=Path("backups"),
                now=datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC),
            )

            with patch(
                "eggpool.cli.select_backup",
                return_value=MagicMock(path=archive.resolve()),
            ):
                result = runner.invoke(cli, ["recover"], input="y\n")

            assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# eggpool uninstall
# ---------------------------------------------------------------------------


def _make_uninstall_paths(
    *,
    tmp_path: Path,
    config: Path,
    method: str = "pipx",
    eggpool_dir: Path | None = None,
    binary_path: Path | None = None,
) -> Path:
    """Create a config + db layout suitable for uninstall tests."""
    config.write_text(_config_text())
    db = tmp_path / "usage.sqlite3"
    db.write_bytes(b"db")
    return db


class TestUninstallCommand:
    def test_pipx_uninstall_with_yes(self, tmp_path: Path) -> None:
        from eggpool.lifecycle import InstallMethod

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            db = _make_uninstall_paths(tmp_path=tmp_path, config=config)

            fake_paths = MagicMock()
            fake_paths.install_method = InstallMethod.PIPX
            fake_paths.config_path = config
            fake_paths.env_path = None
            fake_paths.db_path = db
            fake_paths.data_dir = tmp_path / "data"
            fake_paths.binary_path = None
            fake_paths.eggpool_dir = None

            with (
                patch(
                    "eggpool.cli.resolve_uninstall_paths",
                    return_value=fake_paths,
                ),
                patch(
                    "eggpool.cli.do_uninstall",
                    return_value=fake_paths,
                ) as mock_uninstall,
                patch(
                    "eggpool.cli.verify_binary_removed",
                    return_value=[],
                ),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=0)
                result = runner.invoke(cli, ["uninstall", "--yes"])

            assert result.exit_code == 0, result.output
            mock_uninstall.assert_called_once()
            kwargs = mock_uninstall.call_args.kwargs
            assert kwargs["cleanup_data"] is True
            assert kwargs["cleanup_config"] is True
            assert kwargs["cleanup_path"] is True

    def test_keep_flags_passed_through(self, tmp_path: Path) -> None:
        from eggpool.lifecycle import InstallMethod

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            _make_uninstall_paths(tmp_path=tmp_path, config=config)

            fake_paths = MagicMock()
            fake_paths.install_method = InstallMethod.PIPX
            fake_paths.config_path = config
            fake_paths.env_path = None
            fake_paths.db_path = tmp_path / "usage.sqlite3"
            fake_paths.data_dir = tmp_path / "data"
            fake_paths.binary_path = None
            fake_paths.eggpool_dir = None

            with (
                patch(
                    "eggpool.cli.resolve_uninstall_paths",
                    return_value=fake_paths,
                ),
                patch(
                    "eggpool.cli.do_uninstall",
                    return_value=fake_paths,
                ) as mock_uninstall,
                patch(
                    "eggpool.cli.verify_binary_removed",
                    return_value=[],
                ),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=0)
                result = runner.invoke(
                    cli,
                    [
                        "uninstall",
                        "--yes",
                        "--keep-data",
                        "--keep-config",
                        "--keep-path",
                    ],
                )

            assert result.exit_code == 0, result.output
            kwargs = mock_uninstall.call_args.kwargs
            assert kwargs["cleanup_data"] is False
            assert kwargs["cleanup_config"] is False
            assert kwargs["cleanup_path"] is False

    def test_source_install_requires_verified_dir(self, tmp_path: Path) -> None:
        from eggpool.lifecycle import InstallMethod

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            config.write_text(_config_text())

            fake_paths = MagicMock()
            fake_paths.install_method = InstallMethod.SOURCE
            fake_paths.config_path = config
            fake_paths.env_path = None
            fake_paths.db_path = tmp_path / "usage.sqlite3"
            fake_paths.data_dir = tmp_path / "data"
            fake_paths.binary_path = None
            fake_paths.eggpool_dir = None

            with patch(
                "eggpool.cli.resolve_uninstall_paths",
                return_value=fake_paths,
            ):
                result = runner.invoke(cli, ["uninstall", "--yes"])

            assert result.exit_code == 1
            assert "could not be verified" in result.output

    def test_manual_install_requires_binary(self, tmp_path: Path) -> None:
        from eggpool.lifecycle import InstallMethod

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            config.write_text(_config_text())

            fake_paths = MagicMock()
            fake_paths.install_method = InstallMethod.MANUAL
            fake_paths.config_path = config
            fake_paths.env_path = None
            fake_paths.db_path = tmp_path / "usage.sqlite3"
            fake_paths.data_dir = tmp_path / "data"
            fake_paths.binary_path = None
            fake_paths.eggpool_dir = None

            with patch(
                "eggpool.cli.resolve_uninstall_paths",
                return_value=fake_paths,
            ):
                result = runner.invoke(cli, ["uninstall", "--yes"])

            assert result.exit_code == 1
            assert "cannot locate the eggpool binary" in result.output

    def test_declined_top_level_prompt_aborts(self, tmp_path: Path) -> None:
        from eggpool.lifecycle import InstallMethod

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            config.write_text(_config_text())

            fake_paths = MagicMock()
            fake_paths.install_method = InstallMethod.PIPX
            fake_paths.config_path = config
            fake_paths.env_path = None
            fake_paths.db_path = tmp_path / "usage.sqlite3"
            fake_paths.data_dir = tmp_path / "data"
            fake_paths.binary_path = None
            fake_paths.eggpool_dir = None

            with (
                patch(
                    "eggpool.cli.resolve_uninstall_paths",
                    return_value=fake_paths,
                ),
                patch("eggpool.cli.do_uninstall") as mock_uninstall,
                patch("subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=0)
                result = runner.invoke(cli, ["uninstall"], input="n\n")

            assert result.exit_code == 0
            assert "Aborted" in result.output
            mock_uninstall.assert_not_called()

    def test_warns_when_binary_still_on_path(self, tmp_path: Path) -> None:
        from eggpool.lifecycle import InstallMethod

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            _make_uninstall_paths(tmp_path=tmp_path, config=config)

            fake_paths = MagicMock()
            fake_paths.install_method = InstallMethod.PIPX
            fake_paths.config_path = config
            fake_paths.env_path = None
            fake_paths.db_path = tmp_path / "usage.sqlite3"
            fake_paths.data_dir = tmp_path / "data"
            fake_paths.binary_path = None
            fake_paths.eggpool_dir = None

            leftover = Path("/tmp/leftover/eggpool")

            with (
                patch(
                    "eggpool.cli.resolve_uninstall_paths",
                    return_value=fake_paths,
                ),
                patch("eggpool.cli.do_uninstall", return_value=fake_paths),
                patch(
                    "eggpool.cli.verify_binary_removed",
                    return_value=[leftover],
                ),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=0)
                result = runner.invoke(cli, ["uninstall", "--yes"])

            assert result.exit_code == 0
            assert "still reachable on PATH" in result.output
            assert str(leftover) in result.output

    def test_prints_cleanup_instructions(self, tmp_path: Path) -> None:
        from eggpool.lifecycle import InstallMethod

        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            config = Path("config.toml")
            _make_uninstall_paths(tmp_path=tmp_path, config=config)

            fake_paths = MagicMock()
            fake_paths.install_method = InstallMethod.PIPX
            fake_paths.config_path = config
            fake_paths.env_path = None
            fake_paths.db_path = tmp_path / "usage.sqlite3"
            fake_paths.data_dir = tmp_path / "data"
            fake_paths.binary_path = None
            fake_paths.eggpool_dir = None

            with (
                patch(
                    "eggpool.cli.resolve_uninstall_paths",
                    return_value=fake_paths,
                ),
                patch("eggpool.cli.do_uninstall", return_value=fake_paths),
                patch(
                    "eggpool.cli.verify_binary_removed",
                    return_value=[],
                ),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.return_value = MagicMock(returncode=0)
                result = runner.invoke(cli, ["uninstall", "--yes"])

            assert result.exit_code == 0
            assert "systemctl disable" in result.output
            assert "logrotate.d/eggpool" in result.output


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


class TestCommandRegistration:
    @pytest.mark.parametrize("cmd", ["backup", "recover", "uninstall"])
    def test_command_is_registered(self, cmd: str) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, [cmd, "--help"])
        assert result.exit_code == 0, result.output
