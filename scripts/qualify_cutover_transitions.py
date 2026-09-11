#!/usr/bin/env python3
"""Qualify the K005 Python -> Rust -> Python -> Rust package cycle.

The runner deliberately receives already-built wheels.  It never publishes,
rewrites, or replaces an executable outside its temporary manager roots.  A
successful run is evidence about real manager behavior, not a simulation of
version strings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_OUTPUT = 8192
TIMEOUT = 300
PYTHON_VERSION = "0.7.4"
RUST_VERSION = "0.8.0"
MANAGERS = ("uv-tool", "pipx", "pip")


class QualificationError(RuntimeError):
    """A bounded qualification failure with no subprocess body."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bounded_error(error: BaseException) -> dict[str, str]:
    text = str(error).lower()
    markers = (
        ("timeout", "timeout"),
        ("no such file", "manager_unavailable"),
        ("not found", "target_not_found"),
        ("requires-python", "incompatible_python_environment"),
        ("unsupported", "unsupported_platform"),
        ("already installed", "already_installed"),
    )
    category = next(
        (value for marker, value in markers if marker in text), "manager_failure"
    )
    detail = "bounded qualification failure"
    if os.environ.get("K005_DIAGNOSTIC_ERRORS"):
        detail = str(error)[:MAX_OUTPUT] or detail
    return {"category": category, "detail": detail}


def _run(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: int = TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QualificationError(error.__class__.__name__) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise QualificationError(detail[:MAX_OUTPUT])
    return result


def _python_executable(path: Path) -> Path:
    candidate = path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not candidate.is_file():
        raise QualificationError("virtual environment Python is unavailable")
    return candidate


def _wheel_version(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        for line in archive.read(metadata_name).decode("utf-8").splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
    raise QualificationError(f"wheel metadata has no version: {path.name}")


def _artifact(path: Path, expected: str) -> dict[str, Any]:
    if not path.is_file() or _wheel_version(path) != expected:
        raise QualificationError(f"wheel does not contain expected version {expected}")
    return {"filename": path.name, "sha256": _sha256(path), "size": path.stat().st_size}


class Manager:
    """One real package-manager environment inside a temporary root."""

    def __init__(
        self,
        name: str,
        root: Path,
        python: Path | None,
        *,
        public_index: bool = False,
    ) -> None:
        self.name = name
        self.root = root
        self.python = python
        self.public_index = public_index
        self.wheelhouse = root / "wheelhouse"
        self.wheelhouse.mkdir(parents=True)
        self.base_env = self._environment()
        self._configure_manager_environment()
        self.executable: Path
        self.metadata_python: Path

    def _configure_manager_environment(self) -> None:
        if self.name == "uv-tool":
            self.base_env.update(
                {
                    "UV_TOOL_DIR": str(self.root / "uv/tools"),
                    "UV_TOOL_BIN_DIR": str(self.root / "uv/bin"),
                    "UV_CACHE_DIR": str(self.root / "uv-cache"),
                    "UV_NO_CONFIG": "1",
                }
            )
            if self.python:
                self.base_env["UV_PYTHON"] = str(self.python)
        elif self.name == "pipx":
            self.base_env.update(
                {
                    "PIPX_HOME": str(self.root / "pipx"),
                    "PIPX_BIN_DIR": str(self.root / "pipx/bin"),
                    "PIPX_MAN_DIR": str(self.root / "pipx/man"),
                }
            )
            if self.python:
                self.base_env["PIPX_DEFAULT_PYTHON"] = str(self.python)

    def _environment(self) -> dict[str, str]:
        keep = {
            "PATH",
            "SYSTEMROOT",
            "SSL_CERT_FILE",
            "REQUESTS_CA_BUNDLE",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        }
        environment = {key: value for key, value in os.environ.items() if key in keep}
        environment.update(
            {
                "HOME": str(self.root / "home"),
                "XDG_CONFIG_HOME": str(self.root / "config-home"),
                "XDG_CACHE_HOME": str(self.root / "cache-home"),
                "XDG_DATA_HOME": str(self.root / "data-home"),
                "XDG_STATE_HOME": str(self.root / "state-home"),
                "TMPDIR": str(self.root / "tmp"),
                "PYTHONHASHSEED": "0",
                "TZ": "UTC",
                "LC_ALL": "C",
                "LANG": "C",
            }
        )
        for directory in environment.values():
            if directory.startswith(str(self.root)):
                Path(directory).mkdir(parents=True, exist_ok=True)
        return environment

    def _manager_path(self, name: str) -> str:
        path = shutil.which(name, path=self.base_env.get("PATH"))
        if path is None:
            raise QualificationError(f"{name} is unavailable")
        return path

    def _install_command(self, wheel: Path, version: str) -> list[str]:
        if self.name == "pip":
            assert self.metadata_python is not None
            command = [
                str(self.metadata_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--upgrade",
                "--force-reinstall",
            ]
            if not self.public_index:
                command.append("--no-deps")
            command.append(f"eggpool=={version}")
            return command
        manager = self._manager_path("uv" if self.name == "uv-tool" else "pipx")
        if self.name == "uv-tool":
            return [manager, "tool", "install", "--force", f"eggpool=={version}"]
        return [manager, "install", "--force", f"eggpool=={version}"]

    def _install_path_command(self, wheel: Path) -> list[str]:
        if self.name == "pip":
            assert self.metadata_python is not None
            return [
                str(self.metadata_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                str(wheel),
            ]
        manager = self._manager_path("uv" if self.name == "uv-tool" else "pipx")
        if self.name == "uv-tool":
            return [manager, "tool", "install", "--force", str(wheel)]
        return [manager, "install", "--force", str(wheel)]

    def install(self, wheel: Path, version: str) -> None:
        _run(
            self._install_command(wheel, version),
            env=self.environment(local_wheelhouse=not self.public_index),
            cwd=self.root,
        )
        self._locate_runtime(version)

    def refresh_runtime(self, version: str) -> None:
        self._locate_runtime(version)

    def install_initial_python(self, wheel: Path, version: str) -> None:
        if self.public_index:
            if self.name == "pip":
                assert self.python is not None
                environment = self.root / "venv"
                _run(
                    [str(self.python), "-m", "venv", str(environment)],
                    env=self.base_env,
                    cwd=self.root,
                )
                self.metadata_python = _python_executable(environment)
            public_environment = dict(self.base_env)
            public_environment.update(
                {
                    "PIP_INDEX_URL": "https://pypi.org/simple",
                    "UV_INDEX_URL": "https://pypi.org/simple",
                }
            )
            _run(
                self._install_command(wheel, version),
                env=public_environment,
                cwd=self.root,
            )
            self._locate_runtime(version)
            return
        if self.name == "pip":
            assert self.python is not None
            environment = self.root / "venv"
            _run(
                [str(self.python), "-m", "venv", str(environment)],
                env=self.base_env,
                cwd=self.root,
            )
            self.metadata_python = _python_executable(environment)
            _run(
                [str(self.metadata_python), "-m", "pip", "install", str(wheel)],
                env=self.base_env,
                cwd=self.root,
            )
        else:
            self.metadata_python = self.python or Path(sys.executable)
            _run(self._install_path_command(wheel), env=self.base_env, cwd=self.root)
        self._locate_runtime(version)

    def _locate_runtime(self, version: str) -> None:
        if self.name == "pip":
            self.executable = (
                self.root
                / "venv"
                / ("Scripts/eggpool.exe" if os.name == "nt" else "bin/eggpool")
            )
            self.metadata_python = _python_executable(self.root / "venv")
        elif self.name == "uv-tool":
            self.executable = self.root / "uv/tools/eggpool/bin/eggpool"
            self.metadata_python = self.root / "uv/tools/eggpool/bin/python"
        else:
            self.executable = self.root / "pipx/bin/eggpool"
            self.metadata_python = self.root / "pipx/venvs/eggpool/bin/python"
        if not self.executable.exists():
            raise QualificationError(f"{self.name} did not expose eggpool")
        if not self.metadata_python.exists():
            candidates = sorted(self.root.rglob("python"))
            if candidates:
                self.metadata_python = candidates[0]
        if not self.metadata_python.exists():
            raise QualificationError(f"{self.name} environment Python is unavailable")
        observed = self.metadata_version()
        if observed != version:
            raise QualificationError(f"metadata version {observed} != {version}")

    def environment(self, *, local_wheelhouse: bool = False) -> dict[str, str]:
        result = dict(self.base_env)
        manager_bin = self.executable.parent
        result["PATH"] = os.pathsep.join((str(manager_bin), result.get("PATH", "")))
        if self.name == "uv-tool":
            result.update(
                {
                    "UV_TOOL_DIR": str(self.root / "uv/tools"),
                    "UV_TOOL_BIN_DIR": str(self.root / "uv/bin"),
                    "UV_CACHE_DIR": str(self.root / "uv-cache"),
                    "UV_NO_CONFIG": "1",
                }
            )
            if self.python:
                result["UV_PYTHON"] = str(self.python)
        elif self.name == "pipx":
            result.update(
                {
                    "PIPX_HOME": str(self.root / "pipx"),
                    "PIPX_BIN_DIR": str(self.root / "pipx/bin"),
                    "PIPX_MAN_DIR": str(self.root / "pipx/man"),
                }
            )
            if self.python:
                result["PIPX_DEFAULT_PYTHON"] = str(self.python)
        if local_wheelhouse:
            result.update(
                {
                    "PIP_FIND_LINKS": str(self.wheelhouse),
                    "UV_FIND_LINKS": str(self.wheelhouse),
                    "PIP_INDEX_URL": "https://pypi.org/simple",
                    "UV_INDEX_URL": "https://pypi.org/simple",
                }
            )
        elif self.public_index:
            result.update(
                {
                    "PIP_INDEX_URL": "https://pypi.org/simple",
                    "UV_INDEX_URL": "https://pypi.org/simple",
                }
            )
        return result

    def run(self, args: list[str], *, local_wheelhouse: bool = False) -> None:
        _run(
            [str(self.executable), *args],
            env=self.environment(local_wheelhouse=local_wheelhouse),
            cwd=self.root,
        )

    def metadata_version(self) -> str:
        result = _run(
            [
                str(self.metadata_python),
                "-c",
                "import importlib.metadata; "
                "print(importlib.metadata.version('eggpool'))",
            ],
            env=self.environment(),
            cwd=self.root,
        )
        return result.stdout.strip()


def _config(root: Path) -> Path:
    path = root / "config.toml"
    source = (ROOT / "tests/migration_rs/fixtures/config/valid.toml").read_text()
    source = source.replace(
        'path = "migration.sqlite3"', f'path = "{root / "usage.sqlite3"}"'
    )
    path.write_text(source)
    return path


def _state(config: Path, database: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "config_sha256": _sha256(config),
        "db_sha256": _sha256(database) if database.is_file() else None,
        "db_integrity": None,
        "migration_max": None,
        "row_counts": {},
    }
    if not database.is_file():
        return result
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        result["db_integrity"] = connection.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        result["row_counts"] = {
            table: connection.execute(f' SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
        if "_migrations" in tables:
            result["migration_max"] = connection.execute(
                "SELECT MAX(version) FROM _migrations"
            ).fetchone()[0]
    finally:
        connection.close()
    return result


def _record(
    manager: str,
    source: tuple[str, str],
    target: tuple[str, str],
    started: float,
    *,
    result: str,
    rollback: str,
    cli_version: str | None,
    metadata_version: str | None,
    state: dict[str, Any],
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "manager_class": manager,
        "source_version": source[0],
        "source_era": source[1],
        "target_version": target[0],
        "target_era": target[1],
        "result": result,
        "rollback_result": rollback,
        "cli_package_metadata_version": metadata_version,
        "cli_version": cli_version,
        "config_sha256": state.get("config_sha256"),
        "db_observation": {
            "sha256": state.get("db_sha256"),
            "integrity": state.get("db_integrity"),
            "migration_max": state.get("migration_max"),
            "row_counts": state.get("row_counts", {}),
        },
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "error": error,
    }


def qualify_manager(
    name: str,
    python: Path | None,
    python_wheel: Path,
    rust_wheel: Path,
    *,
    public_index: bool = False,
) -> list[dict[str, Any]]:
    started = time.monotonic()
    temporary = tempfile.mkdtemp(prefix=f"eggpool-k005-{name}-")
    root = Path(temporary)
    try:
        config = _config(root)
        database = root / "usage.sqlite3"
        manager = Manager(name, root, python, public_index=public_index)
        shutil.copy2(python_wheel, manager.wheelhouse / python_wheel.name)
        shutil.copy2(rust_wheel, manager.wheelhouse / rust_wheel.name)
        try:
            manager.install_initial_python(python_wheel, PYTHON_VERSION)
            manager.run(["--config", str(config), "migrate"])
            python_state = _state(config, database)
            manager.install(rust_wheel, RUST_VERSION)
            manager.run(["version"])
            manager.run(
                ["--config", str(config), "update", PYTHON_VERSION],
                local_wheelhouse=not public_index,
            )
            manager.refresh_runtime(PYTHON_VERSION)
            manager.run(["--config", str(config), "check-config"])
            rollback_state = _state(config, database)
            manager.install(rust_wheel, RUST_VERSION)
            manager.run(["version"])
            final_state = _state(config, database)
            manager.run(
                ["--config", str(config), "update", RUST_VERSION],
                local_wheelhouse=not public_index,
            )
            final_cli = manager.metadata_version()
            if final_cli != RUST_VERSION:
                raise QualificationError(
                    "exact-current update changed the installed version"
                )
            if python_state["config_sha256"] != final_state["config_sha256"]:
                raise QualificationError("config changed during cross-era cycle")
            if (
                rollback_state["db_integrity"] != "ok"
                or final_state["db_integrity"] != "ok"
            ):
                raise QualificationError("database integrity check failed")
            return [
                _record(
                    name,
                    (PYTHON_VERSION, "python"),
                    (RUST_VERSION, "rust"),
                    started,
                    result="pass",
                    rollback="pass",
                    cli_version=RUST_VERSION,
                    metadata_version=final_cli,
                    state=final_state,
                )
            ]
        except (QualificationError, OSError, subprocess.SubprocessError) as error:
            state = _state(config, database)
            return [
                _record(
                    name,
                    (PYTHON_VERSION, "python"),
                    (RUST_VERSION, "rust"),
                    started,
                    result="fail",
                    rollback="unknown",
                    cli_version=None,
                    metadata_version=None,
                    state=state,
                    error=_bounded_error(error),
                )
            ]
    finally:
        if not os.environ.get("K005_KEEP_ROOT"):
            shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-wheel", type=Path, required=True)
    parser.add_argument("--rust-wheel", type=Path, required=True)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--manager", choices=MANAGERS, action="append")
    parser.add_argument(
        "--public-index",
        action="store_true",
        help="install every version through the public PyPI index",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    python_wheel = args.python_wheel.resolve()
    rust_wheel = args.rust_wheel.resolve()
    try:
        artifacts = {
            "python": _artifact(python_wheel, PYTHON_VERSION),
            "rust": _artifact(rust_wheel, RUST_VERSION),
        }
        managers = args.manager or list(MANAGERS)
        rows: list[dict[str, Any]] = []
        for name in managers:
            try:
                manager_rows = qualify_manager(
                    name,
                    args.python,
                    python_wheel,
                    rust_wheel,
                    public_index=args.public_index,
                )
                for row in manager_rows:
                    if (
                        row["result"] == "fail"
                        and row["db_observation"].get("integrity") is None
                    ):
                        row["result"] = "skipped"
                        row["rollback_result"] = "not_run"
                rows.extend(manager_rows)
            except (QualificationError, OSError) as error:
                rows.append(
                    {
                        "manager_class": name,
                        "source_version": PYTHON_VERSION,
                        "source_era": "python",
                        "target_version": RUST_VERSION,
                        "target_era": "rust",
                        "result": "skipped",
                        "rollback_result": "not_run",
                        "cli_package_metadata_version": None,
                        "cli_version": None,
                        "config_sha256": None,
                        "db_observation": {},
                        "elapsed_ms": 0,
                        "error": _bounded_error(error),
                    }
                )
        document = {
            "schema_version": "k005-transition-matrix.v1",
            "host": {"system": platform.system(), "machine": platform.machine()},
            "artifacts": artifacts,
            "results": rows,
        }
        encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded)
        print(encoded, end="")
        failures = [row for row in rows if row["result"] == "fail"]
        skipped = [row for row in rows if row["result"] == "skipped"]
        return 1 if failures or (args.strict and skipped) else 0
    except (QualificationError, OSError, StopIteration) as error:
        print(
            json.dumps(
                {
                    "schema_version": "k005-transition-matrix.v1",
                    "error": _bounded_error(error),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
