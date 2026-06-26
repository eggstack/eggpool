"""Tests for the automatic background backup task."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from eggpool.models.config import AppConfig


def _make_config(
    tmp_path: Path,
    *,
    enabled: bool = True,
    interval_s: int = 86_400,
    retain_count: int = 14,
    startup_delay_s: int = 0,
    db_path: str | None = None,
    directory: str | None = None,
    include_env: bool = True,
) -> tuple[AppConfig, Path]:
    """Create a minimal AppConfig with a file-backed DB for backup tests."""
    config_file = tmp_path / "config.toml"
    actual_db = db_path or str(tmp_path / "usage.sqlite3")
    config_file.write_text(
        f"""
[database]
path = "{actual_db}"

[backup]
enabled = {str(enabled).lower()}
interval_s = {interval_s}
retain_count = {retain_count}
startup_delay_s = {startup_delay_s}
include_env = {str(include_env).lower()}
"""
    )
    if directory:
        with open(config_file, "a") as f:
            f.write(f'directory = "{directory}"\n')

    config = AppConfig.from_toml(str(config_file))
    return config, config_file


def _init_sqlite_db(db_path: Path) -> None:
    """Create a minimal SQLite database with WAL mode."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO test VALUES (1, 'hello')")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# _resolve_backup_dir
# ---------------------------------------------------------------------------


class TestResolveBackupDir:
    def test_uses_custom_directory(self, tmp_path: Path) -> None:
        from eggpool.background.backup import _resolve_backup_dir

        config, _ = _make_config(tmp_path, directory="/custom/backup")
        assert _resolve_backup_dir(config) == Path("/custom/backup")

    def test_falls_back_to_default(self, tmp_path: Path) -> None:
        from eggpool.background.backup import _resolve_backup_dir

        config, _ = _make_config(tmp_path)
        result = _resolve_backup_dir(config)
        # Should be the default backup dir
        from eggpool.lifecycle.backup import default_backup_dir

        assert result == default_backup_dir()


# ---------------------------------------------------------------------------
# _resolve_env_path
# ---------------------------------------------------------------------------


class TestResolveEnvPath:
    def test_returns_none_when_disabled(self, tmp_path: Path) -> None:
        from eggpool.background.backup import _resolve_env_path

        assert _resolve_env_path(tmp_path / "config.toml", include_env=False) is None

    def test_returns_none_when_no_config(self) -> None:
        from eggpool.background.backup import _resolve_env_path

        assert _resolve_env_path(None, include_env=True) is None

    def test_returns_env_when_exists(self, tmp_path: Path) -> None:
        from eggpool.background.backup import _resolve_env_path

        env = tmp_path / ".env"
        env.write_text("KEY=val\n")
        result = _resolve_env_path(tmp_path / "config.toml", include_env=True)
        assert result == env

    def test_returns_none_when_env_missing(self, tmp_path: Path) -> None:
        from eggpool.background.backup import _resolve_env_path

        result = _resolve_env_path(tmp_path / "config.toml", include_env=True)
        assert result is None


# ---------------------------------------------------------------------------
# automatic_backup_loop (single iteration)
# ---------------------------------------------------------------------------


class TestAutomaticBackupLoop:
    @pytest.mark.asyncio()
    async def test_creates_backup_and_prunes(self, tmp_path: Path) -> None:
        """A single backup iteration creates an archive and runs retention."""
        from eggpool.background.backup import _run_backup_once

        config, config_path = _make_config(tmp_path, startup_delay_s=0, retain_count=2)
        db_path = Path(config.database.path)
        _init_sqlite_db(db_path)

        backup_dir = tmp_path / "backups"

        # Override directory in config
        config.backup.directory = str(backup_dir)

        await _run_backup_once(
            config=config,
            db=None,  # type: ignore[arg-type]
            config_path=config_path,
            env_path=None,
        )

        archives = list(backup_dir.glob("eggpool-backup-*.zip"))
        assert len(archives) == 1

    @pytest.mark.asyncio()
    async def test_skips_in_memory_database(self, tmp_path: Path, caplog) -> None:
        """In-memory databases are skipped with a warning."""
        from eggpool.background.backup import automatic_backup_loop

        config, _ = _make_config(tmp_path, db_path=":memory:")

        # The loop returns immediately for in-memory databases.
        await automatic_backup_loop(
            config=config,
            db=None,  # type: ignore[arg-type]
            config_path=tmp_path / "config.toml",
            env_path=None,
        )

        assert any("in-memory" in record.message for record in caplog.records)

    @pytest.mark.asyncio()
    async def test_skips_when_no_config_path(self, tmp_path: Path, caplog) -> None:
        """Skips backup when config_path is None."""
        from eggpool.background.backup import _run_backup_once

        config, _ = _make_config(tmp_path)

        await _run_backup_once(
            config=config,
            db=None,  # type: ignore[arg-type]
            config_path=None,
            env_path=None,
        )

        assert any("no config path" in record.message for record in caplog.records)

    @pytest.mark.asyncio()
    async def test_metadata_records_live_db_path(self, tmp_path: Path) -> None:
        """Archive META db_path points at the live DB, not the staged snapshot."""
        from eggpool.background.backup import _run_backup_once

        config, config_path = _make_config(tmp_path, startup_delay_s=0)
        db_path = Path(config.database.path)
        _init_sqlite_db(db_path)

        backup_dir = tmp_path / "backups"
        config.backup.directory = str(backup_dir)

        await _run_backup_once(
            config=config,
            db=None,  # type: ignore[arg-type]
            config_path=config_path,
            env_path=None,
        )

        from eggpool.lifecycle.backup import parse_zip_metadata

        archives = list(backup_dir.glob("eggpool-backup-*.zip"))
        assert len(archives) == 1
        meta = parse_zip_metadata(archives[0])
        # META should reference the live DB path, not a staging temp
        assert meta["db_path"] == str(db_path)
        assert "staging" not in str(meta["db_path"])

    @pytest.mark.asyncio()
    async def test_sqlite_snapshot_is_consistent(self, tmp_path: Path) -> None:
        """The archived SQLite snapshot contains expected data."""
        from eggpool.background.backup import _run_backup_once

        config, config_path = _make_config(tmp_path, startup_delay_s=0)
        db_path = Path(config.database.path)
        _init_sqlite_db(db_path)

        backup_dir = tmp_path / "backups"
        config.backup.directory = str(backup_dir)

        await _run_backup_once(
            config=config,
            db=None,  # type: ignore[arg-type]
            config_path=config_path,
            env_path=None,
        )

        import zipfile

        archives = list(backup_dir.glob("eggpool-backup-*.zip"))
        assert len(archives) == 1

        with zipfile.ZipFile(archives[0]) as zf:
            # Extract the SQLite snapshot
            snapshot_data = zf.read("usage.sqlite3")
            snapshot_path = tmp_path / "snapshot.sqlite3"
            snapshot_path.write_bytes(snapshot_data)

        # Open the snapshot and verify data
        conn = sqlite3.connect(str(snapshot_path))
        rows = conn.execute("SELECT * FROM test").fetchall()
        conn.close()
        assert rows == [(1, "hello")]

    @pytest.mark.asyncio()
    async def test_no_wal_shm_in_archive(self, tmp_path: Path) -> None:
        """Runtime backup archives do not contain WAL/SHM sidecars."""
        from eggpool.background.backup import _run_backup_once

        config, config_path = _make_config(tmp_path, startup_delay_s=0)
        db_path = Path(config.database.path)
        _init_sqlite_db(db_path)

        backup_dir = tmp_path / "backups"
        config.backup.directory = str(backup_dir)

        await _run_backup_once(
            config=config,
            db=None,  # type: ignore[arg-type]
            config_path=config_path,
            env_path=None,
        )

        import zipfile

        archives = list(backup_dir.glob("eggpool-backup-*.zip"))
        with zipfile.ZipFile(archives[0]) as zf:
            names = zf.namelist()
        assert "usage.sqlite3-wal" not in names
        assert "usage.sqlite3-shm" not in names
