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


@dataclass(frozen=True)
class RcFileChange:
    """One rc-file edit planned by :func:`preview_eggpool_path_changes`.

    ``original`` and ``new_text`` are full file contents; ``removed_lines``
    lists the lines that would disappear so the CLI can show a focused
    diff. ``changed`` is ``False`` when the file would not be touched,
    which lets callers filter the preview list cheaply.
    """

    path: Path
    original: str
    new_text: str
    removed_lines: tuple[str, ...]
    changed: bool


def detect_install_method() -> InstallMethod:
    """Determine how the running Python was installed.

    Mirrors :func:`eggpool.cli._detect_install_method` but returns the
    strongly-typed :class:`InstallMethod` enum used by the lifecycle
    module so callers can switch on it without string-matching.

    Detection works by inspecting the resolved ``eggpool`` binary path
    (not just ``sys.executable``) against the canonical tool layouts.
    ``pipx`` is detected when the binary lives under a ``pipx/venvs``
    or ``pipx/shared`` directory; ``uv-tool`` when it lives under a
    ``uv/tools`` directory. A bare ``pipx`` on PATH does **not** by
    itself justify a pipx classification — that produced false
    positives on dev machines where pipx happens to be installed for
    unrelated work.
    """
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )

    if in_venv:
        eggpool_exe = shutil.which("eggpool")
        candidates: list[Path] = []
        if eggpool_exe is not None:
            candidates.append(Path(eggpool_exe).resolve())
        candidates.append(Path(sys.executable).resolve())

        for exe in candidates:
            parts = exe.parts
            if "uv" in parts and "tools" in parts:
                return InstallMethod.UV_TOOL
            if "pipx" in parts and ("venvs" in parts or "shared" in parts):
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
    """Run ``pipx uninstall eggpool`` using the correct interpreter.

    pipx is not installed inside the eggpool venv that pipx itself
    manages, so we cannot use ``sys.executable`` directly. See
    :func:`_find_pipx_invocation` for resolution details.
    """
    cmd = _find_pipx_invocation(env)
    return subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _find_pipx_invocation(env: dict[str, str]) -> list[str]:
    """Resolve the command argv that runs ``pipx`` on this machine.

    Resolution order:

    1. The ``pipx`` binary on ``PATH`` (preferred — it has its own
       shebang pointing at the Python pipx was installed under).
    2. ``EGGPOOL_PYTHON`` env override (escape hatch for unusual setups).
    3. A Python interpreter that has ``pipx`` importable, scanned from
       common locations (``/usr/bin/python3``, ``/usr/local/bin/python3``,
       then ``shutil.which`` fallbacks).
    4. ``sys.executable`` as a last-resort fallback so we surface pipx's
       own error instead of silently no-oping.
    """
    pipx_path = shutil.which("pipx")
    if pipx_path:
        return [pipx_path, "uninstall", "eggpool"]

    override = env.get("EGGPOOL_PYTHON")
    if override and Path(override).is_file():
        return [override, "-m", "pipx", "uninstall", "eggpool"]

    for candidate in _candidate_pythons():
        if _python_has_pipx(candidate):
            return [candidate, "-m", "pipx", "uninstall", "eggpool"]

    return [sys.executable, "-m", "pipx", "uninstall", "eggpool"]


def _candidate_pythons() -> list[str]:
    """Return Python interpreters worth probing for a ``pipx`` install."""
    found: list[str] = []
    seen: set[str] = set()
    for raw in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if Path(raw).is_file() and raw not in seen:
            found.append(raw)
            seen.add(raw)
    for name in ("python3", "python"):
        resolved = shutil.which(name)
        if resolved and resolved not in seen:
            found.append(resolved)
            seen.add(resolved)
    return found


def _python_has_pipx(python: str) -> bool:
    """Return True if ``python -m pipx --version`` exits 0."""
    try:
        proc = subprocess.run(  # noqa: S603
            [python, "-m", "pipx", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


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

    For an interactive flow, prefer :func:`preview_eggpool_path_changes`
    followed by :func:`apply_eggpool_path_changes` so the user can
    confirm the diff before it is written.
    """
    previews = preview_eggpool_path_changes(rc_files, marker=marker)
    if not any(p.changed for p in previews):
        return []
    return apply_eggpool_path_changes(previews)


def preview_eggpool_path_changes(
    rc_files: Sequence[Path],
    *,
    marker: str = EGGPOOL_SHELL_MARKER,
) -> list[RcFileChange]:
    """Compute the would-be edits without touching disk.

    The returned list contains one :class:`RcFileChange` per existing
    rc file so the caller can show a diff or print the list of files
    that would change before confirming the write.
    """
    previews: list[RcFileChange] = []
    for rc in rc_files:
        if not rc.exists():
            continue
        original = rc.read_text(encoding="utf-8")
        kept = _filter_eggpool_lines(original.splitlines(), marker=marker)
        new_text = "\n".join(kept) + ("\n" if original.endswith("\n") else "")
        changed = new_text != original
        removed = [line for line in original.splitlines() if line not in kept]
        previews.append(
            RcFileChange(
                path=rc,
                original=original,
                new_text=new_text,
                removed_lines=tuple(removed),
                changed=changed,
            )
        )
    return previews


def apply_eggpool_path_changes(previews: Sequence[RcFileChange]) -> list[Path]:
    """Persist the edits planned by :func:`preview_eggpool_path_changes`.

    Returns the list of files that were actually modified.
    """
    modified: list[Path] = []
    for preview in previews:
        if not preview.changed:
            continue
        preview.path.write_text(preview.new_text, encoding="utf-8")
        modified.append(preview.path)
    return modified


def _filter_eggpool_lines(lines: Sequence[str], *, marker: str) -> list[str]:
    """Return *lines* with EggPool-attributable entries removed."""
    kept: list[str] = []
    skip_block = False
    for line in lines:
        if marker in line:
            skip_block = True
            continue
        if skip_block:
            if line.strip() == "":
                skip_block = False
            else:
                continue
        if _is_eggpool_path_line(line):
            continue
        kept.append(line)
    return kept


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
        _assert_safe_path(paths.eggpool_dir, label="source checkout directory")
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
                "Remove it by hand, then re-run --yes after the binary is gone."
            )
        _assert_safe_path(paths.binary_path, label="binary")
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

    if cleanup_data:
        _cleanup_data(paths=paths, confirm=confirm)

    if cleanup_path:
        files = list(rc_files) if rc_files is not None else _default_rc_files()
        previews = preview_eggpool_path_changes(files)
        changeable = [p for p in previews if p.changed]
        if changeable:
            _render_rc_preview(changeable)
            if not confirm("Apply the PATH cleanup shown above?"):
                raise RuntimeError("Uninstall aborted during PATH cleanup.")
            apply_eggpool_path_changes(changeable)

    return paths


def _cleanup_data(*, paths: UninstallPaths, confirm: Callable[[str], bool]) -> None:
    """Delete EggPool's SQLite storage without risking broad directory removal."""
    if _is_safe_path(paths.data_dir):
        if confirm(f"Delete the eggpool data directory at {paths.data_dir}?"):
            _safe_rm_tree(paths.data_dir)
        return

    if not confirm(f"Delete the SQLite database files at {paths.db_path}?"):
        return

    _assert_safe_database_file_path(paths.db_path)
    _safe_unlink(paths.db_path)
    _safe_unlink(paths.db_path.with_name(f"{paths.db_path.name}-wal"))
    _safe_unlink(paths.db_path.with_name(f"{paths.db_path.name}-shm"))


def _render_rc_preview(changeable: Sequence[RcFileChange]) -> None:
    """Print the planned edits for each rc file to stderr.

    The CLI surfaces these via :func:`preview_eggpool_path_changes` so
    the operator can read the diff before confirming. Intended for
    stderr output so the dry-run command line flow still works.
    """
    import sys  # noqa: PLC0415

    for preview in changeable:
        print(f"  {preview.path}:", file=sys.stderr)
        for line in preview.removed_lines:
            print(f"    - {line}", file=sys.stderr)


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
        db_path = Path(path_value_obj).expanduser().resolve()
        _assert_safe_database_file_path(db_path)
        return db_path
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


def _assert_safe_path(target: Path, *, label: str) -> None:
    """Raise ``RuntimeError`` if *target* is a dangerous path to delete.

    This prevents the uninstaller from recursively removing the user's
    home directory, ``/``, or other critical system paths when config
    values or path resolution produce an unexpected result.
    """
    resolved = target.expanduser().resolve()
    home = Path.home().resolve()

    # The most dangerous case: the target is / or a parent of /.
    if resolved == Path("/"):
        raise RuntimeError(
            f"Refusing to delete '/' as the {label}. "
            "Check your configuration — the resolved path is the root directory."
        )

    # Target is the user's home directory.
    if resolved == home:
        raise RuntimeError(
            f"Refusing to delete your home directory ({home}) as the {label}. "
            "Check your [database].path setting — it should point to a "
            "file inside a data directory, not ~ itself."
        )

    # Target is a direct child of / that looks like a system directory.
    if resolved.parent == Path("/"):
        raise RuntimeError(
            f"Refusing to delete '{resolved}' as the {label}. "
            "This is a top-level system directory."
        )

    # Target is a direct child of the home directory that is not an
    # eggpool-specific path (e.g. ~ itself was already caught above,
    # but ~/.config, ~/.cache, etc. should also be rejected).
    if resolved.parent == home:
        raise RuntimeError(
            f"Refusing to delete '{resolved}' as the {label}. "
            "This is a top-level directory in your home and is not "
            "an eggpool data path."
        )


def _is_safe_path(target: Path) -> bool:
    try:
        _assert_safe_path(target, label="path")
    except RuntimeError:
        return False
    return True


def _assert_safe_database_file_path(target: Path) -> None:
    """Raise if a configured database path points at a broad delete target."""
    try:
        _assert_safe_path(target, label="database path")
    except RuntimeError:
        resolved = target.expanduser().resolve()
        home = Path.home().resolve()
        if resolved.parent == home and (resolved.is_file() or resolved.suffix):
            return
        raise


def _safe_rm_tree(target: Path, *, label: str = "directory") -> None:
    """Recursively delete ``target`` without following symlinks to /."""
    import contextlib  # noqa: PLC0415

    _assert_safe_path(target, label=label)

    if not target.exists():
        return
    if target.is_symlink():
        target.unlink()
        return
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            _safe_rm_tree(child, label=label)
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
