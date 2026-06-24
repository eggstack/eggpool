"""Lifecycle helpers: backup, restore, and uninstall for EggPool.

This package contains the implementation behind the ``eggpool backup``,
``eggpool recover``, and ``eggpool uninstall`` CLI commands.  Keeping the
logic in a dedicated module (rather than in ``cli.py``) makes it
straightforward to unit-test in isolation from Click.

Public surface
--------------

- :func:`create_backup`     -- create a .zip backup archive
- :func:`list_backups`      -- enumerate existing backups (sorted newest-first)
- :func:`select_backup`     -- interactive picker via ``TerminalMenu``
- :func:`restore_backup`    -- restore config + env + database from an archive
- :func:`detect_install_method` -- re-exported from :mod:`eggpool.cli`
- :func:`uninstall`         -- top-level uninstall orchestrator
- :func:`verify_eggpool_directory` -- locate the verified eggpool project root

The CLI module imports these functions directly; nothing else in the
codebase should need to know about them.
"""

from __future__ import annotations

from eggpool.lifecycle.backup import (
    BACKUP_FORMAT_VERSION,
    BackupContents,
    BackupEntry,
    backup_filename,
    create_backup,
    default_backup_dir,
    list_backups,
    parse_zip_metadata,
    read_backup_contents,
    restore_backup,
    select_backup,
)
from eggpool.lifecycle.uninstall import (
    EGGPOOL_SHELL_MARKER,
    InstallMethod,
    UninstallPaths,
    detect_install_method,
    pipx_uninstall,
    remove_eggpool_path_entries,
    resolve_uninstall_paths,
    uninstall,
    uv_tool_uninstall,
    verify_binary_removed,
    verify_eggpool_directory,
)

__all__ = [
    "BACKUP_FORMAT_VERSION",
    "BackupContents",
    "BackupEntry",
    "EGGPOOL_SHELL_MARKER",
    "InstallMethod",
    "UninstallPaths",
    "backup_filename",
    "create_backup",
    "default_backup_dir",
    "detect_install_method",
    "list_backups",
    "parse_zip_metadata",
    "pipx_uninstall",
    "read_backup_contents",
    "remove_eggpool_path_entries",
    "resolve_uninstall_paths",
    "restore_backup",
    "select_backup",
    "uninstall",
    "uv_tool_uninstall",
    "verify_binary_removed",
    "verify_eggpool_directory",
]
