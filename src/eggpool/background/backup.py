"""Automatic background backup task.

Provides a supervised background loop that periodically creates
restore-compatible ``.zip`` backups of the configuration and database,
then prunes old archives according to count-based retention.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from eggpool.lifecycle.backup import (
    create_runtime_backup,
    default_backup_dir,
    prune_backups,
)

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.models.config import AppConfig

logger = logging.getLogger(__name__)


def _resolve_backup_dir(config: AppConfig) -> Path:
    """Resolve the backup output directory from config."""
    if config.backup.directory:
        return Path(config.backup.directory)
    return default_backup_dir()


def _resolve_env_path(config_path: Path | None, include_env: bool) -> Path | None:
    """Resolve the environment file path for backup inclusion."""
    if not include_env:
        return None
    if config_path is None:
        return None
    env = config_path.parent / ".env"
    return env if env.exists() else None


async def automatic_backup_loop(
    *,
    config: AppConfig,
    db: Database,
    config_path: Path | None,
    env_path: Path | None,
) -> None:
    """Supervised background loop for automatic backups.

    Waits ``startup_delay_s`` before the first attempt, then runs
    every ``interval_s`` seconds.  Failures are logged and reflected
    in the task monitor heartbeat but never crash the server.
    """
    if config.database.path == ":memory:":
        logger.warning("Automatic backup skipped: database is in-memory")
        return

    delay = config.backup.startup_delay_s
    if delay > 0:
        logger.info("Automatic backup: waiting %ds before first attempt", delay)
        await asyncio.sleep(delay)

    while True:
        try:
            await _run_backup_once(
                config=config,
                db=db,
                config_path=config_path,
                env_path=env_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic backup failed")

        try:
            await asyncio.sleep(config.backup.interval_s)
        except asyncio.CancelledError:
            raise


async def _run_backup_once(
    *,
    config: AppConfig,
    db: Database,
    config_path: Path | None,
    env_path: Path | None,
) -> None:
    """Run a single backup attempt: snapshot, archive, prune."""
    if config_path is None:
        logger.warning("Automatic backup skipped: no config path available")
        return

    output_dir = _resolve_backup_dir(config)
    effective_env = _resolve_env_path(config_path, config.backup.include_env)

    logger.info("Automatic backup started")

    archive_path = await create_runtime_backup(
        db_path=Path(config.database.path),
        config_path=config_path,
        env_path=effective_env,
        output_dir=output_dir,
        install_method="runtime",
        include_env=config.backup.include_env,
        busy_timeout_ms=config.database.busy_timeout_ms,
    )

    size_kb = archive_path.stat().st_size / 1024.0
    logger.info(
        "Automatic backup succeeded: %s (%.1f KB)",
        archive_path.name,
        size_kb,
    )

    deleted = await asyncio.to_thread(
        prune_backups,
        backup_dir=output_dir,
        retain_count=config.backup.retain_count,
    )
    if deleted:
        logger.info(
            "Retention pruned %d backup(s): %s",
            len(deleted),
            ", ".join(p.name for p in deleted),
        )
