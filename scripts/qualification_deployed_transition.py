#!/usr/bin/env python3
"""Qualify the K007 deployed Python/Rust service transition on disposable Linux.

The runner uses one package environment and one systemd unit for all four
legs.  It refuses existing EggPool paths, records only bounded structural
facts, and never reads configuration values into the report.  The personal
mode uses an isolated venv; production mode uses a system-owned pipx root at
``/var/lib/eggpool/pipx`` and the stable ``/usr/local/bin/eggpool`` exposure.

Example (disposable Linux VM)::

    sudo -E uv run python scripts/qualification_deployed_transition.py \
      --python-wheel eggpool-0.7.4-py3-none-any.whl \
      --rust-wheel eggpool-0.8.0-*.whl \
      --mode personal --output migration-rs/closure/cutover/007-run.json \
      --i-understand-disposable-host
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import pwd
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "eggpool"
MARKER = Path("/var/tmp/eggpool-k007-owned")
PRODUCTION_BINARY = Path("/usr/local/bin/eggpool")
PRODUCTION_PIPX_HOME = Path("/var/lib/eggpool/pipx")
MANAGED = (
    Path("/etc/systemd/system/eggpool.service"),
    Path("/etc/eggpool"),
    Path("/var/lib/eggpool"),
    Path("/var/log/eggpool"),
    Path("/var/backups/eggpool"),
    PRODUCTION_BINARY,
)
MAX_OUTPUT = 1024
TIMEOUT = 120


class QualificationError(RuntimeError):
    """A bounded qualification failure."""


def bounded(value: str) -> str:
    text = " ".join(value.replace("\x00", " ").split())
    for marker in ("Bearer ", "token=", "api_key=", "sk-"):
        if marker in text:
            text = text.split(marker, 1)[0] + "<redacted>"
    return text[:MAX_OUTPUT]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_fact(path: Path) -> dict[str, Any]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "sha256": sha256(path) if path.is_file() else None,
    }


class Runner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[dict[str, Any]] = []

    def run(
        self,
        name: str,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,
        timeout: int = TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        try:
            result = subprocess.run(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise QualificationError(f"{name}: {error.__class__.__name__}") from error
        self.commands.append(
            {
                "id": name,
                "argv": [
                    str(item).replace(str(self.root), "<TEMP_ROOT>") for item in argv
                ],
                "returncode": result.returncode,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "output": bounded(result.stderr or result.stdout),
            }
        )
        if check and result.returncode != 0:
            raise QualificationError(
                f"{name} failed with exit {result.returncode}: "
                f"{bounded(result.stderr or result.stdout)}"
            )
        return result


def require_linux() -> None:
    if platform.system() != "Linux":
        raise QualificationError("K007 requires Linux; no host mutation was attempted")
    if os.geteuid() != 0:
        raise QualificationError("K007 requires effective root")
    if Path("/proc/1/comm").read_text(encoding="utf-8").strip() != "systemd":
        raise QualificationError("K007 requires systemd as PID 1")
    missing = [
        name
        for name in ("systemctl", "useradd", "userdel")
        if shutil.which(name) is None
    ]
    if missing:
        raise QualificationError(f"missing host tools: {', '.join(missing)}")


def preflight(mode: str) -> None:
    conflicts = [str(path) for path in MANAGED if path.exists()]
    if mode == "personal":
        conflicts = [str(path) for path in conflicts if path != str(PRODUCTION_BINARY)]
    if conflicts:
        raise QualificationError("managed paths already exist: " + ", ".join(conflicts))
    if mode == "production" and shutil.which("pipx") is None:
        raise QualificationError("production mode requires a system-owned pipx")
    if mode == "production":
        try:
            pwd.getpwnam("eggpool")
        except KeyError:
            return
        raise QualificationError("the production eggpool user already exists")


def user_command(user: str, env: dict[str, str], argv: list[str]) -> list[str]:
    return [
        "runuser",
        "-u",
        user,
        "--",
        "env",
        *[f"{key}={value}" for key, value in env.items()],
        *argv,
    ]


def wait_http(url: str, timeout: int = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.25)
    raise QualificationError(f"health did not converge: {url}")


def write_config(path: Path, database: Path, backup: Path, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            "[server]",
            'host = "127.0.0.1"',
            f"port = {port}",
            'api_key = "q007-server-key"',
            "",
            "[database]",
            f'path = "{database}"',
            "",
            "[dashboard]",
            "enabled = false",
            "",
            "[models]",
            "startup_refresh = false",
            "",
            "[model_info]",
            "enabled = false",
            "startup_refresh = false",
            "",
            "[backup]",
            f'directory = "{backup}"',
            "include_env = false",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")


def write_unit(path: Path, binary: Path, config: Path, user: str, home: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(
        [
            "[Unit]",
            "Description=EggPool K007 qualification",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"User={user}",
            f"Group={user}",
            f"ExecStart={binary} --config {config} serve --verbose",
            f"WorkingDirectory={home}",
            f"Environment=HOME={home}",
            f"Environment=EGGPOOL_CONFIG={config}",
            "Restart=on-failure",
            "RestartSec=1",
            "TimeoutStopSec=15",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(0o644)
    return sha256(path)


def service_command(mode: str, user: str, env: dict[str, str], *args: str) -> list[str]:
    if mode == "production":
        return ["systemctl", *args]
    return user_command(user, env, ["systemctl", "--user", *args])


def install_initial(
    runner: Runner,
    mode: str,
    user: str,
    env: dict[str, str],
    python_wheel: Path,
    rust_wheel: Path,
    python: Path,
    root: Path,
) -> tuple[Path, Path]:
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir()
    shutil.copy2(python_wheel, wheelhouse / python_wheel.name)
    shutil.copy2(rust_wheel, wheelhouse / rust_wheel.name)
    if mode == "production":
        manager_env = os.environ.copy()
        manager_env.update(
            {
                "PIPX_HOME": str(PRODUCTION_PIPX_HOME),
                "PIPX_BIN_DIR": "/usr/local/bin",
                "PIP_FIND_LINKS": str(wheelhouse),
                "PIP_NO_INDEX": "1",
            }
        )
        runner.run(
            "production-python",
            ["pipx", "install", "--force", str(python_wheel)],
            env=manager_env,
        )
        return PRODUCTION_BINARY, PRODUCTION_BINARY
    runner.run(
        "personal-python",
        user_command(
            user,
            env,
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                str(python_wheel),
            ],
        ),
        env=os.environ.copy(),
    )
    return root / "venv/bin/eggpool", root / "venv/bin/python"


def install_leg(
    runner: Runner,
    mode: str,
    user: str,
    env: dict[str, str],
    wheel: Path,
    python: Path,
    wheelhouse: Path,
) -> None:
    if mode == "production":
        manager_env = os.environ.copy()
        manager_env.update(
            {
                "PIPX_HOME": str(PRODUCTION_PIPX_HOME),
                "PIPX_BIN_DIR": "/usr/local/bin",
                "PIP_FIND_LINKS": str(wheelhouse),
                "PIP_NO_INDEX": "1",
            }
        )
        runner.run(
            "package-leg", ["pipx", "install", "--force", str(wheel)], env=manager_env
        )
    else:
        runner.run(
            "package-leg",
            user_command(
                user,
                env,
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    str(wheelhouse),
                    str(wheel),
                ],
            ),
            env=os.environ.copy(),
        )


def run_cycle(
    mode: str,
    python_wheel: Path,
    rust_wheel: Path,
    output: Path,
) -> dict[str, Any]:
    require_linux()
    preflight(mode)
    root = Path(tempfile.mkdtemp(prefix="eggpool-k007-"))
    runner = Runner(root)
    user = f"k007-{os.getpid()}"
    user_home = root / "home"
    port = 18000 + os.getpid() % 1000
    config = (
        Path("/etc/eggpool/config.toml")
        if mode == "production"
        else user_home / ".config/eggpool/config.toml"
    )
    database = (
        Path("/var/lib/eggpool/usage.sqlite3")
        if mode == "production"
        else user_home / ".local/share/eggpool/usage.sqlite3"
    )
    backup = (
        Path("/var/backups/eggpool")
        if mode == "production"
        else user_home / ".local/share/eggpool/backups"
    )
    report: dict[str, Any] = {"schema": "m11-k007.v1", "mode": mode, "status": "fail"}
    env: dict[str, str] = {}
    try:
        if mode == "personal":
            runner.run(
                "user-create",
                ["useradd", "-m", "-d", str(user_home), "-s", "/bin/sh", user],
            )
            uid = pwd.getpwnam(user).pw_uid
            (root / "venv").mkdir()
            runner.run("venv", ["python3", "-m", "venv", str(root / "venv")])
            shutil.chown(root, user=user, group=user)
            shutil.chown(root / "venv", user=user, group=user)
            env = {
                "HOME": str(user_home),
                "XDG_CONFIG_HOME": str(user_home / ".config"),
                "XDG_DATA_HOME": str(user_home / ".local/share"),
                "XDG_RUNTIME_DIR": f"/run/user/{uid}",
                "PATH": f"{root / 'venv/bin'}:/usr/bin:/bin",
                "PIP_NO_INDEX": "1",
                "TZ": "UTC",
            }
            runner.run("linger", ["loginctl", "enable-linger", user], check=False)
        else:
            user = "eggpool"
            env = {
                "HOME": "/var/lib/eggpool",
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PIPX_HOME": str(PRODUCTION_PIPX_HOME),
                "PIPX_BIN_DIR": "/usr/local/bin",
                "PIP_NO_INDEX": "1",
                "TZ": "UTC",
            }
            runner.run(
                "user-create",
                [
                    "useradd",
                    "-r",
                    "-d",
                    "/var/lib/eggpool",
                    "-s",
                    "/usr/sbin/nologin",
                    user,
                ],
            )
            MARKER.write_text("K007 disposable ownership marker\n", encoding="utf-8")
            for directory in (
                Path("/etc/eggpool"),
                Path("/var/lib/eggpool"),
                Path("/var/log/eggpool"),
                Path("/var/backups/eggpool"),
            ):
                directory.mkdir(parents=True)
        config.parent.mkdir(parents=True, exist_ok=True)
        database.parent.mkdir(parents=True, exist_ok=True)
        backup.mkdir(parents=True, exist_ok=True)
        write_config(config, database, backup, port)
        python = root / "venv/bin/python"
        if mode == "personal":
            shutil.chown(config.parent, user=user, group=user)
            shutil.chown(config, user=user, group=user)
            shutil.chown(database.parent, user=user, group=user)
            shutil.chown(backup, user=user, group=user)
        installed, manager_python = install_initial(
            runner, mode, user, env, python_wheel, rust_wheel, python, root
        )
        if mode == "personal":
            unit = user_home / ".config/systemd/user/eggpool.service"
            unit_hash = write_unit(unit, installed, config, user, user_home)
            runner.run(
                "daemon-reload",
                service_command(mode, user, env, "daemon-reload"),
                env=os.environ.copy(),
            )
            runner.run(
                "enable",
                service_command(mode, user, env, "enable", "eggpool"),
                env=os.environ.copy(),
            )
            runner.run(
                "start",
                service_command(mode, user, env, "start", "eggpool"),
                env=os.environ.copy(),
            )
        else:
            unit = Path("/etc/systemd/system/eggpool.service")
            unit_hash = write_unit(
                unit, installed, config, user, Path("/var/lib/eggpool")
            )
            runner.run("daemon-reload", ["systemctl", "daemon-reload"])
            runner.run("enable", ["systemctl", "enable", SERVICE])
            runner.run("start", ["systemctl", "start", SERVICE])
        wait_http(f"http://127.0.0.1:{port}/v1/healthz")
        baseline_config = sha256(config)
        baseline_unit = unit_hash
        legs = [
            ("python", python_wheel),
            ("rust", rust_wheel),
            ("python", python_wheel),
            ("rust", rust_wheel),
        ]
        observations: list[dict[str, Any]] = []
        for index, (era, wheel) in enumerate(legs):
            if index:
                runner.run(
                    f"stop-{index}",
                    service_command(mode, user, env, "stop", SERVICE),
                    env=os.environ.copy(),
                )
                install_leg(
                    runner, mode, user, env, wheel, manager_python, root / "wheelhouse"
                )
                runner.run(
                    f"start-{index}",
                    service_command(mode, user, env, "start", SERVICE),
                    env=os.environ.copy(),
                )
                wait_http(f"http://127.0.0.1:{port}/v1/healthz")
            observations.append(
                {
                    "leg": index,
                    "era": era,
                    "config_sha256": sha256(config),
                    "unit_sha256": sha256(unit),
                    "database": file_fact(database),
                    "active": True,
                }
            )
        if any(
            item["config_sha256"] != baseline_config
            or item["unit_sha256"] != baseline_unit
            for item in observations
        ):
            raise QualificationError("config or deployment unit changed during cycle")
        report.update(
            {
                "status": "pass",
                "candidate": {"python": str(python_wheel), "rust": str(rust_wheel)},
                "unit": file_fact(unit),
                "observations": observations,
                "commands": runner.commands,
            }
        )
        return report
    finally:
        try:
            runner.run(
                "cleanup-stop",
                service_command(mode, user, env, "disable", "--now", SERVICE),
                check=False,
                timeout=30,
            )
            runner.run("cleanup-reload", ["systemctl", "daemon-reload"], check=False)
        except (NameError, QualificationError):
            pass
        if mode == "production":
            for path in (
                Path("/etc/systemd/system/eggpool.service"),
                Path("/etc/eggpool"),
                Path("/var/lib/eggpool"),
                Path("/var/log/eggpool"),
                Path("/var/backups/eggpool"),
                PRODUCTION_BINARY,
            ):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif path.exists() or path.is_symlink():
                    path.unlink()
            runner.run("user-delete", ["userdel", "-r", "eggpool"], check=False)
        elif user:
            try:
                pwd.getpwnam(user)
            except KeyError:
                pass
            else:
                runner.run("user-delete", ["userdel", "-r", user], check=False)
        report["commands"] = runner.commands
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if MARKER.exists():
            MARKER.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-wheel", type=Path, required=True)
    parser.add_argument("--rust-wheel", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=("personal", "production"), default="personal"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--i-understand-disposable-host", action="store_true", dest="ack"
    )
    args = parser.parse_args(argv)
    if not args.ack:
        parser.error("refusing host mutation without --i-understand-disposable-host")
    try:
        run_cycle(
            args.mode,
            args.python_wheel.resolve(),
            args.rust_wheel.resolve(),
            args.output.resolve(),
        )
    except QualificationError as error:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "schema": "m11-k007.v1",
                    "status": "blocked",
                    "reason": bounded(str(error)),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
