"""Backup and restore helpers for EggPool.

A backup is a single ``.zip`` archive containing:

- ``config.toml``            -- the live configuration
- ``env`` (optional)         -- the environment / API-key file
- ``usage.sqlite3``          -- the SQLite database
- ``usage.sqlite3-wal``      -- WAL journal if present
- ``usage.sqlite3-shm``      -- shared-memory file if present
- ``META``                   -- plain-text metadata (version, install method,
                                timestamp) used for restore validation

The archive filename is ``eggpool-backup-YYYYMMDD-HHMMSS.zip`` so that
lexicographic and chronological order agree. The archive itself is
uncompressed (Python's ``zipfile.ZIP_STORED``) because the contents are
already small (a config file, an env file, and a SQLite DB). The .zip
suffix is used because the task statement asks for a "compressed file"
that the user can hand-restore on any platform without needing ``tar``
or ``gzip``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eggpool.providers.connect import TerminalMenu

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

BACKUP_FORMAT_VERSION = 1

BACKUP_FILENAME_RE = re.compile(
    r"^eggpool-backup-(?P<stamp>\d{8}-\d{6})(?:-(?P<suffix>\d+))?\.zip$"
)

CONFIG_BASENAME = "config.toml"
ENV_BASENAME = ".env"
DB_BASENAMES = ("usage.sqlite3", "usage.sqlite3-wal", "usage.sqlite3-shm")
META_BASENAME = "META"


def default_backup_dir() -> Path:
    """Return the default directory in which backups are stored.

    Uses ``$XDG_BACKUP_HOME/eggpool`` when set, otherwise checks for
    a production data directory at ``/var/lib/eggpool`` and uses its
    ``backups/`` subdirectory. Falls back to ``$HOME/backups/eggpool``
    for personal-use installs.

    This ensures automatic backups are compatible with the production
    systemd hardening (``ProtectHome=yes`` + ``ReadWritePaths``).
    """
    xdg = os.environ.get("XDG_BACKUP_HOME", "")
    if xdg:
        return Path(xdg) / "eggpool"
    prod_data = Path("/var/lib/eggpool")
    if prod_data.is_dir():
        return prod_data / "backups"
    return Path.home() / "backups" / "eggpool"


def backup_filename(now: datetime | None = None) -> str:
    """Return the canonical timestamped backup filename."""
    stamp = (now or datetime.now(UTC)).astimezone()
    return f"eggpool-backup-{stamp.strftime('%Y%m%d-%H%M%S')}.zip"


def _now_iso(now: datetime | None) -> str:
    """Format the timestamp used in META."""
    stamp = (now or datetime.now(UTC)).astimezone()
    return stamp.isoformat(timespec="seconds")


@dataclass(frozen=True)
class BackupContents:
    """Resolved list of files that will be (or were) included in a backup.

    ``config_path`` and ``db_path`` are required; everything else is
    optional.  All paths are absolute so the archive member names can be
    computed deterministically without depending on the caller's CWD.
    """

    config_path: Path
    db_path: Path
    env_path: Path | None = None
    install_method: str = "unknown"

    def member_paths(self) -> list[Path]:
        """Absolute paths of every file that should be archived."""
        members: list[Path] = [self.config_path]
        db_dir = self.db_path.parent
        for name in DB_BASENAMES:
            candidate = db_dir / name
            if candidate.exists():
                members.append(candidate)
        if self.env_path is not None and self.env_path.exists():
            members.append(self.env_path)
        return members

    def arcnames(self) -> dict[Path, str]:
        """Map absolute source path to the archive member name."""
        out: dict[Path, str] = {}
        for p in self.member_paths():
            if p.name == CONFIG_BASENAME:
                out[p] = CONFIG_BASENAME
            elif p.name == ENV_BASENAME:
                out[p] = ENV_BASENAME
            else:
                out[p] = p.name
        return out

    def member_names(self) -> list[str]:
        """List of archive member names (for inspection / metadata)."""
        return list(self.arcnames().values())


@dataclass(frozen=True)
class BackupSource:
    """Separate source paths for archive creation and restore metadata.

    Runtime backups stage a consistent SQLite snapshot into a temporary
    path while the META ``db_path`` must point at the live database so
    restore targets the correct on-disk location.
    """

    config_source: Path
    db_source: Path
    env_source: Path | None = None
    config_target: Path | None = None
    db_target: Path | None = None
    env_target: Path | None = None
    install_method: str = "unknown"

    def __post_init__(self) -> None:
        if self.config_target is None:
            object.__setattr__(self, "config_target", self.config_source)
        if self.db_target is None:
            object.__setattr__(self, "db_target", self.db_source)
        if self.env_target is None:
            object.__setattr__(self, "env_target", self.env_source)

    def to_backup_contents(self) -> BackupContents:
        """Convert to a ``BackupContents`` using source paths."""
        return BackupContents(
            config_path=self.config_source,
            db_path=self.db_source,
            env_path=self.env_source,
            install_method=self.install_method,
        )

    def meta_db_path(self) -> Path:
        """The ``db_path`` recorded in archive metadata (the restore target)."""
        assert self.db_target is not None
        return self.db_target


@dataclass(frozen=True)
class BackupEntry:
    """A backup archive discovered on disk, with parsed metadata."""

    path: Path
    timestamp: datetime
    size_bytes: int

    @property
    def label(self) -> str:
        """Human-readable label for the interactive picker."""
        size_kb = max(self.size_bytes / 1024.0, 0.1)
        stamp = self.timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        return f"{stamp}   ({size_kb:.1f} KB)   {self.path.name}"


@dataclass
class _RestorePlan:
    """Internal: per-file plan returned by ``_plan_restore``."""

    config_target: Path | None = None
    db_target: Path | None = None
    env_target: Path | None = None
    members: dict[str, bytes] = field(default_factory=dict[str, bytes])


def _plan_restore(archive: zipfile.ZipFile) -> _RestorePlan:
    """Inspect the archive and prepare an in-memory plan for restore.

    Files are read fully into memory because backups are small (a config
    file plus a SQLite database) and we want a deterministic
    all-or-nothing restore.
    """
    plan = _RestorePlan()
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = Path(info.filename).name
        if name not in {CONFIG_BASENAME, ENV_BASENAME, *DB_BASENAMES, META_BASENAME}:
            raise ValueError(f"Backup contains unexpected member: {info.filename}")
        if name == CONFIG_BASENAME:
            plan.config_target = Path(info.filename).resolve()
        elif name == ENV_BASENAME:
            plan.env_target = Path(info.filename).resolve()
        elif name == "usage.sqlite3":
            plan.db_target = Path(info.filename).resolve()
        plan.members[name] = archive.read(info.filename)
    return plan


def _snapshot_existing_files(targets: Sequence[Path], staging_dir: Path) -> None:
    """Copy any existing on-disk targets into ``staging_dir``.

    The staging directory lives next to the eventual restored files so
    a partially failed restore can be undone by moving the snapshot
    back.  Files that do not exist are silently skipped.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    for target in targets:
        if not target.exists():
            continue
        snapshot = staging_dir / target.name
        snapshot.write_bytes(target.read_bytes())


def _build_archive(
    archive_path: Path,
    contents: BackupContents,
    *,
    now: datetime | None = None,
    meta_db_path: Path | None = None,
) -> None:
    """Write the zip archive to ``archive_path``.

    ``meta_db_path`` overrides the ``db_path`` recorded in META so
    runtime backups can point at the live DB while staging a snapshot.
    """
    members = contents.arcnames()

    meta_lines = [
        f"format_version = {BACKUP_FORMAT_VERSION}",
        f"created_at = {_now_iso(now)!r}",
        f"install_method = {contents.install_method!r}",
        f"config_path = {str(contents.config_path)!r}",
        f"db_path = {str(meta_db_path or contents.db_path)!r}",
    ]
    if contents.env_path is not None:
        meta_lines.append(f"env_path = {str(contents.env_path)!r}")
    meta_lines.append("members = " + json.dumps(sorted(members.values())))
    meta_lines.append("")
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(META_BASENAME, "\n".join(meta_lines))
        for src, arcname in members.items():
            archive.write(src, arcname=arcname)


def create_backup(
    contents: BackupContents,
    output_dir: Path | None = None,
    *,
    now: datetime | None = None,
    meta_db_path: Path | None = None,
) -> Path:
    """Create a backup archive under ``output_dir`` and return its path.

    The archive filename is generated via :func:`backup_filename` so that
    multiple backups taken in the same minute would still be unique
    (the function appends a numeric suffix when collisions occur).

    Archive publication is atomic: the zip is written to a temporary
    path and renamed into the final location so a crash mid-write
    never leaves a partial archive at the expected filename.
    """
    target_dir = output_dir or default_backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    archive_path = target_dir / backup_filename(now)
    suffix = 0
    while archive_path.exists():
        suffix += 1
        stamp = (now or datetime.now(UTC)).astimezone().strftime("%Y%m%d-%H%M%S")
        archive_path = target_dir / f"eggpool-backup-{stamp}-{suffix}.zip"

    tmp_path = archive_path.with_suffix(".zip.tmp")
    try:
        _build_archive(tmp_path, contents, now=now, meta_db_path=meta_db_path)
        tmp_path.replace(archive_path)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise

    return archive_path


async def create_runtime_backup(
    *,
    db_path: Path,
    config_path: Path,
    env_path: Path | None,
    output_dir: Path,
    install_method: str,
    include_env: bool,
    busy_timeout_ms: int = 5000,
    now: datetime | None = None,
) -> Path:
    """Create a restore-compatible backup using an in-process SQLite snapshot.

    Uses stdlib ``sqlite3.Connection.backup()`` to produce a consistent
    snapshot of the live database, then writes it into the lifecycle
    ``.zip`` archive format.  The archive metadata always records the
    live ``db_path``, not the temporary snapshot, so restore targets
    the correct on-disk location.
    """
    import asyncio
    import shutil
    import sqlite3
    import tempfile

    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(
        tempfile.mkdtemp(
            prefix=".eggpool-backup-staging-",
            dir=str(output_dir),
        )
    )
    staged_db = staging_dir / "usage.sqlite3"

    try:

        def _snapshot_sqlite() -> None:
            src_conn = sqlite3.connect(str(db_path), timeout=busy_timeout_ms / 1000.0)
            try:
                dst_conn = sqlite3.connect(str(staged_db))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()

        await asyncio.to_thread(_snapshot_sqlite)

        source = BackupSource(
            config_source=config_path,
            db_source=staged_db,
            env_source=env_path if include_env else None,
            config_target=config_path,
            db_target=db_path,
            env_target=env_path if include_env else None,
            install_method=install_method,
        )
        return create_backup(
            source.to_backup_contents(),
            output_dir=output_dir,
            now=now,
            meta_db_path=db_path,
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def prune_backups(*, backup_dir: Path, retain_count: int) -> list[Path]:
    """Remove old backup archives, keeping the newest ``retain_count``.

    Returns a list of deleted paths.  Ignores files that do not match
    the lifecycle filename pattern and logs but does not raise on
    individual deletion failures.
    """
    backups = list_backups(backup_dir)
    if len(backups) <= retain_count:
        return []

    to_delete = backups[retain_count:]
    deleted: list[Path] = []
    for entry in to_delete:
        try:
            entry.path.unlink()
            deleted.append(entry.path)
        except OSError:
            logger.warning("Failed to prune backup %s", entry.path, exc_info=True)
    return deleted


def list_backups(backup_dir: Path | None = None) -> list[BackupEntry]:
    """Return all backups in ``backup_dir`` (default location) sorted newest-first.

    Files that match :data:`BACKUP_FILENAME_RE` are accepted. Anything else
    in the directory is ignored so an operator can drop hand-written
    archives alongside without confusing the picker.
    """
    target = backup_dir or default_backup_dir()
    if not target.exists():
        return []

    entries: list[BackupEntry] = []
    for child in target.iterdir():
        if not child.is_file():
            continue
        match = BACKUP_FILENAME_RE.match(child.name)
        if match is None:
            continue
        stamp = datetime.strptime(match.group("stamp"), "%Y%m%d-%H%M%S").replace(
            tzinfo=UTC
        )
        entries.append(
            BackupEntry(
                path=child,
                timestamp=stamp,
                size_bytes=child.stat().st_size,
            )
        )
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    return entries


def parse_zip_metadata(archive_path: Path) -> dict[str, Any]:
    """Read the META member of ``archive_path`` and return its parsed fields.

    Unknown fields are preserved so older readers can ignore new ones.
    """
    with zipfile.ZipFile(archive_path) as archive:
        try:
            raw = archive.read(META_BASENAME)
        except KeyError as exc:
            raise ValueError(
                f"Backup {archive_path} is missing the META member"
            ) from exc
    return tomllib.loads(raw.decode("utf-8"))


def read_backup_contents(archive_path: Path) -> BackupContents:
    """Return the ``BackupContents`` that ``archive_path`` would restore.

    The restore targets are taken from the archive's META block when
    present, otherwise they default to the active configuration /
    database paths.
    """
    meta = parse_zip_metadata(archive_path)
    config = meta.get("config_path")
    db = meta.get("db_path")
    env = meta.get("env_path")
    install_method = meta.get("install_method", "unknown")
    return BackupContents(
        config_path=Path(config) if config else _active_config_path(),
        db_path=Path(db) if db else _active_db_path(),
        env_path=Path(env) if env else None,
        install_method=str(install_method),
    )


def _active_config_path() -> Path:
    """Default config path used when the archive's META omits one."""

    config_env = os.environ.get("EGGPOOL_CONFIG", "")
    if config_env:
        return Path(config_env).resolve()
    return Path("config.toml").resolve()


def _active_db_path() -> Path:
    """Default DB path used when the archive's META omits one."""
    from eggpool.constants import DEFAULT_DATABASE_PATH  # noqa: PLC0415

    return Path(DEFAULT_DATABASE_PATH).resolve()


def select_backup(
    backups: Sequence[BackupEntry],
    title: str = "Select a backup to restore:",
) -> BackupEntry | None:
    """Show an interactive picker for ``backups`` (newest-first).

    Returns the chosen entry or ``None`` when the user quits the menu.
    """
    if not backups:
        return None
    options = [entry.label for entry in backups]
    menu = TerminalMenu(title, options)
    chosen = menu.run()
    if chosen is None:
        return None
    return backups[options.index(chosen)]


def restore_backup(
    archive_path: Path,
    contents: BackupContents | None = None,
) -> None:
    """Restore config + env + database from ``archive_path``.

    The current on-disk files are first snapshotted into a sibling
    ``.restore-snapshot`` directory so the operation can be undone if
    extraction fails partway through. After successful restore the
    snapshot is removed.
    """
    if not archive_path.exists():
        raise FileNotFoundError(f"Backup archive not found: {archive_path}")

    if contents is None:
        contents = read_backup_contents(archive_path)

    targets: list[Path] = []
    if contents.config_path.exists():
        targets.append(contents.config_path)
    if contents.env_path is not None and contents.env_path.exists():
        targets.append(contents.env_path)
    for name in DB_BASENAMES:
        candidate = contents.db_path.parent / name
        if candidate.exists():
            targets.append(candidate)

    staging = archive_path.parent / f"{archive_path.stem}.restore-snapshot"
    if staging.exists():
        raise RuntimeError(
            f"Restore staging directory already exists: {staging}. "
            "Remove it before restoring again."
        )
    _snapshot_existing_files(targets, staging)

    try:
        with zipfile.ZipFile(archive_path) as archive:
            plan = _plan_restore(archive)

        if "config.toml" in plan.members:
            contents.config_path.parent.mkdir(parents=True, exist_ok=True)
            contents.config_path.write_bytes(plan.members["config.toml"])

        if contents.env_path is not None and ".env" in plan.members:
            contents.env_path.parent.mkdir(parents=True, exist_ok=True)
            contents.env_path.write_bytes(plan.members[".env"])

        for name in DB_BASENAMES:
            if name not in plan.members:
                continue
            target = contents.db_path.parent / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plan.members[name])
    except Exception:
        # Best-effort rollback from the snapshot. Failures here are
        # propagated after the rollback attempt so the caller can
        # diagnose; we do not silently swallow them.
        for target in targets:
            snapshot = staging / target.name
            if snapshot.exists():
                target.write_bytes(snapshot.read_bytes())
        raise
    finally:
        for snap in staging.iterdir():
            snap.unlink()
        staging.rmdir()
