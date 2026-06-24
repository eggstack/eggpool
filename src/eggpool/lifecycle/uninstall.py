"""Uninstall helpers for EggPool.

This module isolates the policy decisions involved in removing an
installation:

- which installer to call (``pipx`` / ``uv tool`` / none);
- how to verify the eggpool project root when no installer manages it
  (manual ``pip install`` or a source checkout);
- how to scrub PATH entries that the install script added.

The CLI's ``eggpool uninstall`` command orchestrates these helpers and
gathers user confirmation; the helpers themselves take already-decided
parameters so they can be exercised by the test suite without
simulating terminal interaction.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

EGGPOOL_SHELL_MARKER = "# Added by eggpool"


class InstallMethod(StrEnum):
    """Supported install methods that ``uninstall`` knows how to reverse."""

    PIPX = "pipx"
    UV_TOOL = "uv-tool"
    SOURCE = "source"
    MANUAL = "manual"


@dataclass(frozen=True)
class UninstallPaths:
    """Resolved filesystem targets for an uninstall.

    The CLI uses these to print the concrete actions it will perform
    before asking the user to confirm.
    """

    install_method: InstallMethod
    config_path: Path
    db_path: Path
    env_path: Path | None
    data_dir: Path
    binary_path: Path | None
    eggpool_dir: Path | None


def detect_install_method() -> InstallMethod:
    """Determine how the running Python was installed.

    Mirrors :func:`eggpool.cli._detect_install_method` but returns the
    strongly-typed :class:`InstallMethod` enum used by the lifecycle
    module so callers can switch on it without string-matching.
    """
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )

    if in_venv:
        exe = Path(sys.executable).resolve()
        parts = exe.parts
        if "uv" in parts and "tools" in parts:
            return InstallMethod.UV_TOOL

        if shutil.which("pipx") is not None:
            return InstallMethod.PIPX

        return InstallMethod.MANUAL

    # Not in a venv: look for the eggpool source checkout by walking
    # up from this module's file. The eggpool project always carries
    # a pyproject.toml declaring ``name = "eggpool"`` at its root.
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / "pyproject.toml"
        if not candidate.is_file():
            continue
        if _is_eggpool_project(ancestor, env=dict(os.environ)):
            return InstallMethod.SOURCE
        break

    return InstallMethod.MANUAL


def verify_eggpool_directory(
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Locate the verified eggpool project root.

    Resolution order:

    1. Two levels up from this source file (handles source installs
       where the running Python is the one created by ``uv sync``).
    2. The current working directory if it contains ``pyproject.toml``
       declaring ``name = "eggpool"``.
    3. ``$HOME/eggpool`` for the common "I cloned the repo there" case.
    4. The grandparent of ``sys.executable`` for ``pip install .`` style
       installs (the venv's parent directory usually contains the repo).

    Raises :class:`RuntimeError` if none of the candidates look like a
    legitimate eggpool project root. Callers should treat the raised
    error as "we cannot safely remove anything — ask the user to clean
    up by hand".
    """
    env_map = env if env is not None else dict(os.environ)
    workdir = cwd or Path.cwd()

    candidates: list[Path] = []
    src_root = Path(__file__).resolve().parent.parent.parent
    candidates.append(src_root)
    candidates.append(workdir)
    candidates.append(Path.home() / "eggpool")
    candidates.append(Path(sys.executable).resolve().parent.parent)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_eggpool_project(resolved, env=env_map):
            return resolved

    raise RuntimeError(
        "Cannot verify the eggpool project directory. Pass --yes only "
        "if you have already removed the eggpool binary yourself, or "
        "re-run from inside a cloned eggpool checkout."
    )


def _is_eggpool_project(candidate: Path, *, env: dict[str, str]) -> bool:
    """Return True if ``candidate`` contains a pyproject.toml for eggpool.

    Reads ``pyproject.toml`` and looks for ``name = "eggpool"`` in the
    ``[project]`` table. The check is intentionally simple — we only
    need enough confidence to refuse deletion when the file is missing
    or refers to some other project.
    """
    pyproject = candidate / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        with open(pyproject, "rb") as f:
            data = tomllib_load(f.read())
    except (OSError, ValueError):
        return False
    project_obj: object = data.get("project", {})
    if not isinstance(project_obj, dict):
        return False
    project = cast("dict[str, object]", project_obj)
    name_obj: object = project.get("name", "")
    return str(name_obj).strip().lower() == "eggpool"


def tomllib_load(blob: bytes) -> dict[str, object]:
    """Tiny indirection over :mod:`tomllib` so tests can monkeypatch it."""
    import tomllib

    return tomllib.loads(blob.decode("utf-8"))


def _pipx_uninstall(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``pipx uninstall eggpool`` in the detected Python."""
    python = env.get("EGGPOOL_PYTHON") or sys.executable
    cmd = [python, "-m", "pipx", "uninstall", "eggpool"]
    return subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _uv_tool_uninstall(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run ``uv tool uninstall eggpool``."""
    cmd = ["uv", "tool", "uninstall", "eggpool"]
    return subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def pipx_uninstall(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke pipx to remove the eggpool venv. Exposed for tests."""
    return (runner or _pipx_uninstall)(env=env if env is not None else dict(os.environ))


def uv_tool_uninstall(
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke uv tool to remove the eggpool tool. Exposed for tests."""
    return (runner or _uv_tool_uninstall)(
        env=env if env is not None else dict(os.environ)
    )


def verify_binary_removed(
    *,
    which: Callable[[str], str | None] | None = None,
    extra_search_paths: Sequence[Path] = (),
) -> list[Path]:
    """Confirm that no ``eggpool`` binary is reachable on ``PATH``.

    Returns the list of locations where an eggpool binary was still
    found. An empty list means the cleanup is complete.
    """
    resolver = which or shutil.which
    found: list[Path] = []
    direct = resolver("eggpool")
    if direct:
        found.append(Path(direct).resolve())
    for extra in extra_search_paths:
        candidate = extra / "eggpool"
        if candidate.exists():
            found.append(candidate.resolve())
    return found


def remove_eggpool_path_entries(
    rc_files: Sequence[Path],
    *,
    marker: str = EGGPOOL_SHELL_MARKER,
) -> list[Path]:
    """Scrub ``eggpool``-attributable PATH entries from ``rc_files``.

    A line is considered attributable to eggpool if:

    - It begins with the ``marker`` comment line, or
    - It contains the literal string ``eggpool`` in a PATH export, or
    - It is the standalone ``uv tool update-shell`` directive that the
      install script appended.

    Generic lines like ``export PATH="$HOME/.local/bin:$PATH"`` are
    preserved because removing them would also strip other tools that
    happen to live in ``~/.local/bin``.

    Returns the list of files that were actually modified.
    """
    modified: list[Path] = []
    for rc in rc_files:
        if not rc.exists():
            continue
        original = rc.read_text(encoding="utf-8")
        lines = original.splitlines()
        kept: list[str] = []
        skip_block = False
        changed = False
        for line in lines:
            if marker in line:
                changed = True
                skip_block = True
                continue
            if skip_block:
                if line.strip() == "":
                    skip_block = False
                else:
                    continue
            if _is_eggpool_path_line(line):
                changed = True
                continue
            kept.append(line)
        if not changed:
            continue
        rc.write_text("\n".join(kept) + "\n", encoding="utf-8")
        modified.append(rc)
    return modified


def _is_eggpool_path_line(line: str) -> bool:
    """Return True if ``line`` is a single eggpool-attributable PATH line."""
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("uv tool update-shell"):
        return True
    if "eggpool" in line.lower() and ("PATH" in line or "export" in line):
        return True
    return "pipx/venvs/eggpool" in line


def _default_rc_files() -> list[Path]:
    """Return the shell rc files the uninstaller will scan by default."""
    home = Path.home()
    candidates = [".zshrc", ".bashrc", ".bash_profile", ".profile"]
    return [home / name for name in candidates]


def uninstall(
    *,
    paths: UninstallPaths,
    confirm: Callable[[str], bool],
    cleanup_data: bool,
    cleanup_config: bool,
    cleanup_path: bool,
    env: dict[str, str] | None = None,
    rc_files: Sequence[Path] | None = None,
    pipx_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    uv_tool_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> UninstallPaths:
    """Run the uninstall sequence described by ``paths``.

    ``confirm`` is called with a human-readable message and must return
    ``True`` to proceed. The CLI wires ``confirm`` to a y/n prompt; the
    test suite passes a deterministic stub.

    Returns ``paths`` so the caller can chain additional steps
    (e.g. emit post-action hints) without re-resolving them.
    """
    env_map = env if env is not None else dict(os.environ)

    if paths.install_method in (InstallMethod.PIPX, InstallMethod.UV_TOOL):
        if paths.install_method is InstallMethod.PIPX:
            if not confirm("Run 'pipx uninstall eggpool' to remove the eggpool venv?"):
                raise RuntimeError("Uninstall aborted before pipx ran.")
            result = pipx_uninstall(runner=pipx_runner, env=env_map)
            if result.returncode != 0:
                raise RuntimeError(
                    f"pipx uninstall failed (exit {result.returncode}): "
                    f"{result.stderr.strip()}"
                )
        else:
            if not confirm(
                "Run 'uv tool uninstall eggpool' to remove the eggpool tool?"
            ):
                raise RuntimeError("Uninstall aborted before uv tool ran.")
            result = uv_tool_uninstall(runner=uv_tool_runner, env=env_map)
            if result.returncode != 0:
                raise RuntimeError(
                    f"uv tool uninstall failed (exit {result.returncode}): "
                    f"{result.stderr.strip()}"
                )
    elif paths.install_method is InstallMethod.SOURCE:
        if paths.eggpool_dir is None:
            raise RuntimeError(
                "Source install detected but no eggpool directory is known."
            )
        confirmed = confirm(
            f"Delete the eggpool source checkout at {paths.eggpool_dir}?"
        )
        if not confirmed:
            raise RuntimeError("Uninstall aborted before source dir removal.")
        _safe_rm_tree(paths.eggpool_dir)
    else:  # MANUAL
        if paths.binary_path is None or not paths.binary_path.exists():
            raise RuntimeError(
                "Cannot locate the eggpool binary for this manual install. "
                "Remove it by hand, then re-run with --keep-binary to skip "
                "this check."
            )
        confirmed = confirm(f"Delete the eggpool binary at {paths.binary_path}?")
        if not confirmed:
            raise RuntimeError("Uninstall aborted before binary removal.")
        try:
            paths.binary_path.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"Could not delete binary {paths.binary_path}: {exc}"
            ) from exc

    if cleanup_config:
        if confirm(f"Delete the configuration file at {paths.config_path}?"):
            _safe_unlink(paths.config_path)
        if paths.env_path is not None and confirm(
            f"Delete the environment file at {paths.env_path}?"
        ):
            _safe_unlink(paths.env_path)

    if cleanup_data and confirm(
        f"Delete the eggpool data directory at {paths.data_dir}?"
    ):
        _safe_rm_tree(paths.data_dir)

    if cleanup_path:
        files = rc_files if rc_files is not None else _default_rc_files()
        modified = remove_eggpool_path_entries(files)
        if modified and not confirm(
            f"Removed eggpool PATH entries from: "
            f"{', '.join(str(p) for p in modified)}. Proceed?"
        ):
            raise RuntimeError("Uninstall aborted during PATH cleanup.")

    return paths


def resolve_uninstall_paths(
    config_path: Path,
    *,
    env: dict[str, str] | None = None,
) -> UninstallPaths:
    """Compute the concrete files / directories affected by ``uninstall``.

    Helper exposed for both the CLI and the test suite so the resolution
    logic is exercised regardless of how the user invokes the command.
    """
    env_map = env if env is not None else dict(os.environ)
    method = detect_install_method()

    db_path = _resolve_db_path(config_path=config_path, env=env_map)
    env_file = _resolve_env_file(config_path=config_path)
    data_dir = db_path.parent
    binary = _resolve_binary_path()
    eggpool_dir: Path | None = None
    if method is InstallMethod.SOURCE:
        try:
            eggpool_dir = verify_eggpool_directory(env=env_map)
        except RuntimeError:
            eggpool_dir = None

    return UninstallPaths(
        install_method=method,
        config_path=config_path,
        db_path=db_path,
        env_path=env_file,
        data_dir=data_dir,
        binary_path=binary,
        eggpool_dir=eggpool_dir,
    )


def _resolve_db_path(*, config_path: Path, env: dict[str, str]) -> Path:
    """Find the SQLite database path used by the current config.

    Reads ``[database].path`` from the active config TOML. Falls back
    to the XDG default when the file is missing or malformed so the
    uninstaller can still clean up a corrupted install.

    ``config_path`` is the explicit config location passed to the CLI
    (or supplied by ``EGGPOOL_CONFIG``); ``env`` is the surrounding
    environment used only as a tie-breaker for resolution.
    """
    from eggpool.constants import DEFAULT_DATABASE_PATH  # noqa: PLC0415

    if not config_path.is_file():
        return Path(DEFAULT_DATABASE_PATH).expanduser().resolve()
    try:
        data = tomllib_load(config_path.read_bytes())
    except (OSError, ValueError):
        return Path(DEFAULT_DATABASE_PATH).expanduser().resolve()
    database_obj: object = data.get("database", {})
    if not isinstance(database_obj, dict):
        return Path(DEFAULT_DATABASE_PATH).expanduser().resolve()
    database = cast("dict[str, object]", database_obj)
    path_value_obj: object = database.get("path")
    if isinstance(path_value_obj, str) and path_value_obj.strip():
        return Path(path_value_obj).expanduser().resolve()
    return Path(DEFAULT_DATABASE_PATH).expanduser().resolve()


def _resolve_env_file(*, config_path: Path) -> Path | None:
    """Return the conventional env file path next to ``config_path``."""
    candidate = config_path.parent / ".env"
    return candidate if candidate.exists() else None


def _resolve_binary_path() -> Path | None:
    """Locate the eggpool binary, preferring the bare ``which`` result."""
    which = shutil.which("eggpool")
    if which is not None:
        return Path(which).resolve()
    exe = Path(sys.executable).resolve()
    if exe.parent.name == "bin":
        candidate = exe.parent / "eggpool"
        if candidate.exists():
            return candidate
    return None


def _safe_rm_tree(target: Path) -> None:
    """Recursively delete ``target`` without following symlinks to /."""
    import contextlib

    if not target.exists():
        return
    if target.is_symlink():
        target.unlink()
        return
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            _safe_rm_tree(child)
        else:
            with contextlib.suppress(FileNotFoundError):
                child.unlink()
    with contextlib.suppress(OSError):
        # Directory not empty (some files survived) — leave it; the
        # caller will surface the residual state.
        target.rmdir()


def _safe_unlink(target: Path) -> None:
    """Delete ``target`` if it exists, silently ignoring missing files."""
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        _safe_rm_tree(target)
