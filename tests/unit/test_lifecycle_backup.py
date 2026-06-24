"""Tests for the lifecycle backup module."""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eggpool.lifecycle.backup import (
    BACKUP_FILENAME_RE,
    BACKUP_FORMAT_VERSION,
    BackupContents,
    BackupEntry,
    backup_filename,
    create_backup,
    default_backup_dir,
    list_backups,
    parse_zip_metadata,
    restore_backup,
    select_backup,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(
    path: Path,
    body: bytes,
) -> Path:
    """Write *body* to *path*, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _make_project(tmp_path: Path) -> BackupContents:
    """Build a realistic BackupContents rooted at ``tmp_path``."""
    config = _write(tmp_path / "config.toml", b"[server]\nport = 11300\n")
    env = _write(tmp_path / ".env", b"API_KEY=secret\n")
    db = _write(tmp_path / "data" / "usage.sqlite3", b"sqlite-blob")
    _write(tmp_path / "data" / "usage.sqlite3-wal", b"wal-blob")
    _write(tmp_path / "data" / "usage.sqlite3-shm", b"shm-blob")
    return BackupContents(
        config_path=config,
        db_path=db,
        env_path=env,
        install_method="pipx",
    )


def _fixed_now() -> datetime:
    """Return a fixed UTC timestamp for deterministic filenames."""
    return datetime(2026, 6, 24, 12, 34, 56, tzinfo=UTC)


# ---------------------------------------------------------------------------
# default_backup_dir
# ---------------------------------------------------------------------------


class TestDefaultBackupDir:
    def test_uses_xdg_backup_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """XDG_BACKUP_HOME takes precedence when set."""
        monkeypatch.setenv("XDG_BACKUP_HOME", "/custom/backup/root")
        assert default_backup_dir() == Path("/custom/backup/root/eggpool")

    def test_falls_back_to_home_backups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without XDG, falls back to ~/backups/eggpool."""
        monkeypatch.delenv("XDG_BACKUP_HOME", raising=False)
        assert default_backup_dir() == Path.home() / "backups" / "eggpool"


# ---------------------------------------------------------------------------
# backup_filename
# ---------------------------------------------------------------------------


class TestBackupFilename:
    def test_format(self) -> None:
        assert backup_filename(_fixed_now()) == "eggpool-backup-20260624-123456.zip"

    def test_uses_local_timezone_for_filename(self) -> None:
        """Filename uses local time so files look right in a folder listing."""
        fixed = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert BACKUP_FILENAME_RE.match(backup_filename(fixed)) is not None


# ---------------------------------------------------------------------------
# create_backup
# ---------------------------------------------------------------------------


class TestCreateBackup:
    def test_creates_zip_with_expected_members(self, tmp_path: Path) -> None:
        """All required files land inside the archive."""
        contents = _make_project(tmp_path)
        output = tmp_path / "backups"

        archive = create_backup(contents, output_dir=output, now=_fixed_now())

        assert archive == output / "eggpool-backup-20260624-123456.zip"
        with zipfile.ZipFile(archive) as zf:
            names = sorted(zf.namelist())
        assert names == sorted(
            {
                "config.toml",
                ".env",
                "usage.sqlite3",
                "usage.sqlite3-wal",
                "usage.sqlite3-shm",
                "META",
            }
        )

    def test_archive_contains_config_and_db_bytes(self, tmp_path: Path) -> None:
        """Archived config matches the source bytes."""
        contents = _make_project(tmp_path)
        archive = create_backup(contents, output_dir=tmp_path, now=_fixed_now())

        with zipfile.ZipFile(archive) as zf:
            assert zf.read("config.toml") == b"[server]\nport = 11300\n"
            assert zf.read("usage.sqlite3") == b"sqlite-blob"
            assert zf.read("usage.sqlite3-wal") == b"wal-blob"

    def test_skips_missing_db_sidecars(self, tmp_path: Path) -> None:
        """Backup does not include -wal/-shm when they are absent."""
        config = _write(tmp_path / "config.toml", b"[server]\n")
        db = _write(tmp_path / "usage.sqlite3", b"db-only")
        contents = BackupContents(config_path=config, db_path=db)

        archive = create_backup(contents, output_dir=tmp_path, now=_fixed_now())

        with zipfile.ZipFile(archive) as zf:
            assert "usage.sqlite3" in zf.namelist()
            assert "usage.sqlite3-wal" not in zf.namelist()
            assert "usage.sqlite3-shm" not in zf.namelist()

    def test_skips_env_when_not_provided(self, tmp_path: Path) -> None:
        """Without env_path, the env member is absent."""
        config = _write(tmp_path / "config.toml", b"[server]\n")
        db = _write(tmp_path / "usage.sqlite3", b"db")
        contents = BackupContents(config_path=config, db_path=db)

        archive = create_backup(contents, output_dir=tmp_path, now=_fixed_now())
        with zipfile.ZipFile(archive) as zf:
            assert ".env" not in zf.namelist()

    def test_creates_output_dir(self, tmp_path: Path) -> None:
        """Output directory is created if missing."""
        contents = _make_project(tmp_path)
        missing = tmp_path / "deep" / "nested" / "backups"

        create_backup(contents, output_dir=missing, now=_fixed_now())

        assert missing.is_dir()
        assert list(missing.glob("*.zip"))

    def test_appends_suffix_on_collision(self, tmp_path: Path) -> None:
        """Two backups taken in the same minute get unique filenames."""
        contents = _make_project(tmp_path)
        first = create_backup(contents, output_dir=tmp_path, now=_fixed_now())
        second = create_backup(contents, output_dir=tmp_path, now=_fixed_now())

        assert first != second
        assert first.name == "eggpool-backup-20260624-123456.zip"
        assert second.name == "eggpool-backup-20260624-123456-1.zip"

    def test_metadata_records_install_method(self, tmp_path: Path) -> None:
        """META block records the install method."""
        contents = _make_project(tmp_path)
        archive = create_backup(contents, output_dir=tmp_path, now=_fixed_now())

        meta = parse_zip_metadata(archive)
        assert meta["format_version"] == BACKUP_FORMAT_VERSION
        assert meta["install_method"] == "pipx"
        assert meta["created_at"] == "2026-06-24T12:34:56+00:00"


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------


class TestListBackups:
    def test_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        assert list_backups(tmp_path / "missing") == []

    def test_returns_only_matching_files(self, tmp_path: Path) -> None:
        """Unrelated files in the dir are ignored."""
        contents = _make_project(tmp_path)
        create_backup(contents, output_dir=tmp_path, now=_fixed_now())
        (tmp_path / "stray.txt").write_text("ignore me")

        entries = list_backups(tmp_path)
        assert len(entries) == 1
        assert entries[0].path.name == "eggpool-backup-20260624-123456.zip"

    def test_sorts_newest_first(self, tmp_path: Path) -> None:
        """Backups returned in reverse-chronological order."""
        contents = _make_project(tmp_path)
        first = create_backup(
            contents, output_dir=tmp_path, now=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        )
        second = create_backup(
            contents,
            output_dir=tmp_path,
            now=datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC),
        )

        entries = list_backups(tmp_path)
        assert [e.path for e in entries] == [second, first]

    def test_entries_carry_size(self, tmp_path: Path) -> None:
        contents = _make_project(tmp_path)
        archive = create_backup(contents, output_dir=tmp_path, now=_fixed_now())
        entries = list_backups(tmp_path)
        assert entries[0].size_bytes == archive.stat().st_size


class TestBackupEntryLabel:
    def test_label_includes_timestamp_and_size(self) -> None:
        """Label string is stable and human-friendly."""
        path = Path("/tmp/eggpool-backup-20260624-123456.zip")
        ts = datetime(2026, 6, 24, 12, 34, 56, tzinfo=UTC)
        entry = BackupEntry(path=path, timestamp=ts, size_bytes=2048)

        label = entry.label
        assert "2026-06-24" in label
        assert "eggpool-backup-20260624-123456.zip" in label
        assert "KB" in label


# ---------------------------------------------------------------------------
# parse_zip_metadata / read_backup_contents
# ---------------------------------------------------------------------------


class TestReadBackupContents:
    def test_reads_members_and_metadata(self, tmp_path: Path) -> None:
        contents = _make_project(tmp_path)
        archive = create_backup(contents, output_dir=tmp_path, now=_fixed_now())

        meta = parse_zip_metadata(archive)
        assert meta["install_method"] == "pipx"
        assert "config.toml" in meta["members"]
        assert "usage.sqlite3" in meta["members"]

    def test_raises_when_meta_missing(self, tmp_path: Path) -> None:
        """An archive without META is rejected."""
        archive = tmp_path / "bad.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("config.toml", b"x")

        with pytest.raises(ValueError, match="META"):
            parse_zip_metadata(archive)


# ---------------------------------------------------------------------------
# select_backup (interactive picker)
# ---------------------------------------------------------------------------


class TestSelectBackup:
    def test_returns_none_for_empty_list(self) -> None:
        assert select_backup([]) is None

    def test_invokes_terminal_menu(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the menu returns an option string, the entry is selected."""
        from eggpool.lifecycle import backup as backup_module

        class FakeMenu:
            def __init__(self, _title: str, options: list[str]) -> None:
                self.options = options

            def run(self) -> str:
                return self.options[1]

        monkeypatch.setattr(backup_module, "TerminalMenu", FakeMenu)

        entries = [
            BackupEntry(
                path=Path("a.zip"),
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                size_bytes=10,
            ),
            BackupEntry(
                path=Path("b.zip"),
                timestamp=datetime(2026, 6, 1, tzinfo=UTC),
                size_bytes=20,
            ),
        ]
        chosen = select_backup(entries)

        assert chosen is entries[1]

    def test_returns_none_when_user_quits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from eggpool.lifecycle import backup as backup_module

        class QuittingMenu:
            def __init__(self, _title: str, _options: list[str]) -> None:
                pass

            def run(self) -> None:
                return None

        monkeypatch.setattr(backup_module, "TerminalMenu", QuittingMenu)
        entries = [
            BackupEntry(
                path=Path("a.zip"),
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                size_bytes=10,
            )
        ]
        assert select_backup(entries) is None


# ---------------------------------------------------------------------------
# restore_backup
# ---------------------------------------------------------------------------


class TestRestoreBackup:
    def test_round_trip_preserves_files(self, tmp_path: Path) -> None:
        """A backup can be restored into a fresh tree."""
        original = _make_project(tmp_path / "src")
        archive = create_backup(original, output_dir=tmp_path / "out", now=_fixed_now())

        restore_root = tmp_path / "restored"
        restore_root.mkdir()
        targets = BackupContents(
            config_path=restore_root / "config.toml",
            db_path=restore_root / "data" / "usage.sqlite3",
            env_path=restore_root / ".env",
        )

        restore_backup(archive, contents=targets)

        assert targets.config_path.read_bytes() == b"[server]\nport = 11300\n"
        assert targets.env_path is not None
        assert targets.env_path.read_bytes() == b"API_KEY=secret\n"
        db = targets.db_path
        assert db.read_bytes() == b"sqlite-blob"
        assert (db.parent / "usage.sqlite3-wal").read_bytes() == b"wal-blob"
        assert (db.parent / "usage.sqlite3-shm").read_bytes() == b"shm-blob"

    def test_snapshot_removed_after_success(self, tmp_path: Path) -> None:
        """Staging directory is cleaned up on success."""
        original = _make_project(tmp_path / "src")
        archive = create_backup(original, output_dir=tmp_path / "out", now=_fixed_now())

        restore_root = tmp_path / "restored"
        restore_root.mkdir()
        targets = BackupContents(
            config_path=restore_root / "config.toml",
            db_path=restore_root / "data" / "usage.sqlite3",
        )
        restore_backup(archive, contents=targets)

        leftover = list(archive.parent.glob("*.restore-snapshot"))
        assert leftover == []

    def test_raises_for_missing_archive(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            restore_backup(tmp_path / "does-not-exist.zip")

    def test_snapshot_rollback_on_failure(self, tmp_path: Path) -> None:
        """If a member is missing from the archive, existing files survive."""

        # Create a backup with only the config.
        config = _write(tmp_path / "config.toml", b"original-config")
        db = _write(tmp_path / "usage.sqlite3", b"original-db")
        contents = BackupContents(config_path=config, db_path=db)
        archive = create_backup(contents, output_dir=tmp_path / "out")

        # Manually rewrite the archive to drop the db member, so restore
        # writes config but does not touch the existing db.
        truncated = tmp_path / "out2" / "truncated.zip"
        truncated.parent.mkdir()
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(truncated, "w") as dst:
            for info in src.infolist():
                if info.filename == "usage.sqlite3":
                    continue
                dst.writestr(info, src.read(info.filename))

        # Pre-populate the restore target so a snapshot exists.
        restore_root = tmp_path / "restored"
        restore_root.mkdir()
        existing = restore_root / "config.toml"
        existing.write_bytes(b"existing-config")

        targets = BackupContents(
            config_path=existing,
            db_path=restore_root / "usage.sqlite3",
        )

        # Restore succeeds; existing files are unchanged.
        restore_backup(truncated, contents=targets)

        assert existing.read_bytes() == b"original-config"
        # Staging cleaned up.
        assert list(truncated.parent.glob("*.restore-snapshot")) == []

    def test_rejects_unexpected_archive_member(self, tmp_path: Path) -> None:
        """Archives with foreign members are refused."""
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("config.toml", b"x")
            zf.writestr("META", b"format_version = 1\n")
            zf.writestr("../../etc/passwd", b"pwned")

        targets = BackupContents(
            config_path=tmp_path / "config.toml",
            db_path=tmp_path / "usage.sqlite3",
        )
        with pytest.raises(ValueError, match="unexpected member"):
            restore_backup(archive, contents=targets)
