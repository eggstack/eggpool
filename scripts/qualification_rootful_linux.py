"""Run the bounded rootful qualification acceptance on a disposable rootful Linux host.

This is deliberately a host runner, not a container test.  It requires Linux
with systemd as PID 1 and effective root, refuses known EggPool paths before
mutating them, and records only bounded command/status/path metadata.

Usage::

    sudo -E uv run python scripts/qualification_rootful_linux.py \
        --binary rust/target/release/eggpool \
        --output artifacts/qualification/006-run.json \
        --i-understand-disposable-host

The host must be disposable.  ``--cleanup`` is a recovery mode for a runner
that was interrupted after it created its ownership marker; it has the same
explicit safety acknowledgement requirement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import pwd
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "artifacts/qualification/006-run.json"
MANIFEST_VERSION = "runtime-q006.v1"
SERVICE_NAME = "eggpool"
MARKER = Path("/var/tmp/eggpool-q006-owned")
CANDIDATE_ROOT = Path("/usr/local/lib/eggpool-q006")
CANDIDATE_BINARY = CANDIDATE_ROOT / "eggpool"
PRODUCTION_CONFIG = Path("/etc/eggpool/config.toml")
PRODUCTION_ENV = Path("/etc/eggpool/env")
MANAGED_PATHS = (
    Path("/etc/systemd/system/eggpool.service"),
    Path("/etc/logrotate.d/eggpool"),
    Path("/etc/cron.d/eggpool-backup"),
    Path("/etc/eggpool"),
    Path("/var/lib/eggpool"),
    Path("/var/log/eggpool"),
    Path("/var/backups/eggpool"),
    Path("/usr/local/bin/eggpool-backup"),
    CANDIDATE_BINARY,
    PRODUCTION_CONFIG,
    PRODUCTION_ENV,
)
MAX_REASON = 512
COMMAND_TIMEOUT = 45.0


class QualificationError(RuntimeError):
    """A mandatory rootful qualification observation failed."""


def bounded(value: str) -> str:
    """Return compact diagnostics with credential-shaped values removed."""
    text = " ".join(value.replace("\x00", " ").split())
    for marker in ("Bearer ", "bearer ", "api_key=", "token=", "sk-"):
        if marker in text:
            text = text.split(marker, 1)[0] + "<redacted>"
    return text[:MAX_REASON]


def display_path(value: str, root: Path) -> str:
    return value.replace(str(root), "<TEMP_ROOT>")


@dataclass(frozen=True)
class CommandRecord:
    """Secret-free record for one bounded command."""

    command_id: str
    argv: tuple[str, ...]
    status: str
    returncode: int | None
    duration_ms: int
    reason: str

    def as_dict(self, root: Path) -> dict[str, Any]:
        return {
            "id": self.command_id,
            "command": [display_path(item, root) for item in self.argv],
            "status": self.status,
            "returncode": self.returncode,
            "duration_ms": self.duration_ms,
            "reason": bounded(self.reason),
        }


class Runner:
    """Run commands while retaining only bounded evidence."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.records: list[CommandRecord] = []

    def run(
        self,
        command_id: str,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                env=env,
                input=input_text,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            duration = int((time.monotonic() - started) * 1000)
            self.records.append(
                CommandRecord(
                    command_id,
                    tuple(argv),
                    "timeout",
                    None,
                    duration,
                    str(error),
                )
            )
            raise QualificationError(f"{command_id} timed out") from error
        duration = int((time.monotonic() - started) * 1000)
        reason = completed.stderr or completed.stdout
        self.records.append(
            CommandRecord(
                command_id,
                tuple(argv),
                "pass" if completed.returncode == 0 else "fail",
                completed.returncode,
                duration,
                bounded(reason),
            )
        )
        return completed

    def require(self, command_id: str, argv: list[str], **kwargs: Any) -> str:
        completed = self.run(command_id, argv, **kwargs)
        if completed.returncode != 0:
            raise QualificationError(
                f"{command_id} failed with exit {completed.returncode}: "
                f"{bounded(completed.stderr or completed.stdout)}"
            )
        return completed.stdout


class LoopbackHandler(BaseHTTPRequestHandler):
    """Minimal provider response surface for the host acceptance."""

    requests: ClassVar[int] = 0

    def log_message(self, format: str, *_args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/models":
            self._respond(
                200, b'{"object":"list","data":[{"id":"q006-fixture-model"}]}'
            )
        else:
            self._respond(404, b"not found")

    def do_POST(self) -> None:  # noqa: N802
        LoopbackHandler.requests += 1
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        streaming = b'"stream":true' in body.replace(b" ", b"")
        if streaming:
            self._respond(
                200,
                b'data: {"id":"q006-stream","choices":[{"delta":{"content":"ok"}}]}\n\n'
                b"data: [DONE]\n\n",
                "text/event-stream",
            )
            return
        self._respond(
            200,
            b'{"id":"q006-finite","object":"chat.completion","model":"q006-fixture-model",'
            b'"choices":[{"index":0,"message":{"role":"assistant","content":"ok"},'
            b'"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,'
            b'"total_tokens":2}}',
        )

    def _respond(
        self, status: int, body: bytes, content_type: str = "application/json"
    ) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)


class LoopbackProvider:
    """Threaded loopback-only provider with no external network access."""

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> LoopbackProvider:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def available(name: str) -> bool:
    return shutil.which(name) is not None


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
        "is_dir": path.is_dir(),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wait_for(
    runner: Runner, command_id: str, argv: list[str], timeout: float = 20
) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        completed = runner.run(command_id, argv, timeout=5)
        last = completed.stdout or completed.stderr
        if completed.returncode == 0:
            return last
        time.sleep(0.5)
    raise QualificationError(f"{command_id} did not converge: {bounded(last)}")


def http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return int(response.status)
    except urllib.error.URLError as error:
        raise QualificationError(f"HTTP probe failed: {error}") from error


def authenticated_get_status(url: str) -> int:
    request = urllib.request.Request(
        url, headers={"authorization": "Bearer q006-server-key"}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return int(response.status)
    except urllib.error.URLError as error:
        raise QualificationError(f"HTTP probe failed: {error}") from error


def wait_http(url: str, timeout: float = 20) -> int:
    deadline = time.monotonic() + timeout
    last: QualificationError | None = None
    while time.monotonic() < deadline:
        try:
            return http_status(url)
        except QualificationError as error:
            last = error
            time.sleep(0.5)
    raise QualificationError(f"HTTP probe did not converge: {last}")


def host_facts() -> dict[str, Any]:
    release = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")
    values: dict[str, str] = {}
    for line in release.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ID", "VERSION_ID", "PRETTY_NAME"}:
            values[key.lower()] = value.strip('"')[:128]
    return {
        "os": values,
        "kernel": platform.release()[:128],
        "architecture": platform.machine()[:64],
        "systemd_pid1": Path("/proc/1/comm").read_text(encoding="utf-8").strip(),
        "systemd_version": command_version("systemctl", "--version"),
        "cron_version": command_version("crontab", "--version")
        if available("crontab")
        else None,
        "logrotate_version": command_version("logrotate", "--version")
        if available("logrotate")
        else None,
    }


def command_version(program: str, arg: str) -> str:
    try:
        result = subprocess.run(
            [program, arg], capture_output=True, text=True, check=False, timeout=3
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if result.returncode != 0:
        return "unavailable"
    return bounded(result.stdout or result.stderr)


def assert_preflight() -> None:
    if platform.system() != "Linux":
        raise QualificationError(
            "rootful qualification requires Linux; no host mutation was attempted"
        )
    if os.geteuid() != 0:
        raise QualificationError(
            "rootful qualification requires effective root for production acceptance"
        )
    if Path("/proc/1/comm").read_text(encoding="utf-8").strip() != "systemd":
        raise QualificationError("rootful qualification requires systemd as PID 1")
    required = ("systemctl", "useradd", "userdel", "runuser")
    missing = [name for name in required if not available(name)]
    if missing:
        raise QualificationError(f"missing mandatory host tools: {', '.join(missing)}")
    conflicts = [str(path) for path in MANAGED_PATHS if path.exists()]
    if CANDIDATE_ROOT.exists():
        conflicts.append(str(CANDIDATE_ROOT))
    if conflicts:
        raise QualificationError(
            "managed paths already exist; refusing takeover: " + ", ".join(conflicts)
        )
    try:
        pwd.getpwnam("eggpool")
    except KeyError:
        return
    raise QualificationError(
        "eggpool production user already exists; refusing takeover"
    )


def cleanup_owned(runner: Runner, run_root: Path, user: str | None = None) -> None:
    """Remove only artifacts created by this runner after stopping the unit."""
    if Path("/etc/systemd/system/eggpool.service").exists():
        runner.run(
            "cleanup-stop", ["systemctl", "disable", "--now", SERVICE_NAME], timeout=15
        )
    runner.run("cleanup-reload", ["systemctl", "daemon-reload"], timeout=15)
    for path in (
        Path("/etc/systemd/system/eggpool.service"),
        Path("/etc/logrotate.d/eggpool"),
        Path("/etc/cron.d/eggpool-backup"),
        Path("/usr/local/bin/eggpool-backup"),
    ):
        if path.is_file() or path.is_symlink():
            path.unlink()
    if CANDIDATE_ROOT.exists() and not CANDIDATE_ROOT.is_symlink():
        shutil.rmtree(CANDIDATE_ROOT)
    for path in (
        Path("/etc/eggpool"),
        Path("/var/lib/eggpool"),
        Path("/var/log/eggpool"),
        Path("/var/backups/eggpool"),
    ):
        if path.exists() and not path.is_symlink():
            shutil.rmtree(path)
    if user:
        runner.run("cleanup-user", ["userdel", "-r", user], timeout=15)
    if MARKER.exists():
        MARKER.unlink()
    if run_root.exists():
        shutil.rmtree(run_root)


def write_config(
    path: Path, *, database: Path, backup: Path, port: int, provider: str
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'''[server]
host = "127.0.0.1"
port = {port}
api_key = "q006-server-key"
max_request_body_bytes = 1048576

[database]
path = "{database}"

[dashboard]
enabled = false

[models]
startup_refresh = true

[model_info]
enabled = false
startup_refresh = false

[backup]
directory = "{backup}"
include_env = false

[providers.fixture]
id = "fixture"
base_url = "{provider}"
protocols = ["openai"]

[providers.fixture.auth]
mode = "bearer"

[providers.fixture.models_endpoint]
method = "GET"

[[providers.fixture.static_models]]
id = "q006-fixture-model"
protocol = "openai"

[[providers.fixture.accounts]]
name = "q006-account"
api_key = "q006-provider-key"
''',
        encoding="utf-8",
    )


def env_for(root: Path, home: Path, config: Path) -> dict[str, str]:
    values = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EGGPOOL_")
        and key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "SERVER_API_KEY"}
    }
    values.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(root / "config-home"),
            "XDG_DATA_HOME": str(root / "data-home"),
            "XDG_STATE_HOME": str(root / "state-home"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "XDG_BACKUP_HOME": str(root / "backup-home"),
            "EGGPOOL_CONFIG": str(config),
            "EGGPOOL_RUNTIME_DIR": str(root / "runtime"),
            "EGGPOOL_PID_FILE": str(root / "runtime/eggpool.pid"),
            "EGGPOOL_LOG_FILE": str(root / "state-home/eggpool.log"),
            "TZ": "UTC",
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONHASHSEED": "0",
            "RUST_BACKTRACE": "0",
        }
    )
    for directory in (
        Path(values["XDG_CONFIG_HOME"]),
        Path(values["XDG_DATA_HOME"]),
        Path(values["XDG_STATE_HOME"]),
        Path(values["XDG_RUNTIME_DIR"]),
        Path(values["XDG_BACKUP_HOME"]),
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return values


def cli(binary: Path, config: Path, *args: str) -> list[str]:
    return [str(binary), "--config", str(config), *args]


def user_cli(user: str, binary: Path, config: Path, *args: str) -> list[str]:
    return [
        "runuser",
        "-u",
        user,
        "--preserve-environment",
        "--",
        *cli(binary, config, *args),
    ]


def run_qualification(binary: Path, output: Path) -> dict[str, Any]:
    assert_preflight()
    root = Path(tempfile.mkdtemp(prefix="eggpool-q006-"))
    root.chmod(0o755)
    runner = Runner(root)
    user = f"q006-{os.getpid()}"
    user_home = root / "user-home"
    installed_binary = CANDIDATE_BINARY
    installed_binary.parent.mkdir(parents=True)
    shutil.copy2(binary, installed_binary)
    installed_binary.chmod(0o755)
    user_created = False
    production_user_created = False
    facts: dict[str, Any] = {
        "schema": MANIFEST_VERSION,
        "candidate": {"path": str(binary), "sha256": sha256(binary)},
    }
    try:
        runner.require(
            "user-create",
            ["useradd", "-m", "-d", str(user_home), "-s", "/bin/sh", user],
        )
        user_created = True
        with LoopbackProvider() as provider:
            personal_root = root / "personal"
            personal_config = personal_root / "config.toml"
            write_config(
                personal_config,
                database=user_home / ".local/share/eggpool/usage.sqlite3",
                backup=user_home / ".local/share/eggpool/backups",
                port=pick_port(),
                provider=provider.url,
            )
            personal_env = env_for(personal_root, user_home, personal_config)
            personal_env.update(
                {
                    "XDG_CONFIG_HOME": str(user_home / ".config"),
                    "XDG_DATA_HOME": str(user_home / ".local/share"),
                    "XDG_STATE_HOME": str(user_home / ".local/state"),
                    "HOME": str(user_home),
                }
            )
            for key in (
                "EGGPOOL_RUNTIME_DIR",
                "EGGPOOL_PID_FILE",
                "EGGPOOL_LOG_FILE",
                "XDG_RUNTIME_DIR",
            ):
                personal_env.pop(key, None)
            uid = str(pwd.getpwnam(user).pw_uid)
            gid = str(pwd.getpwnam(user).pw_gid)
            personal_env.update(
                {
                    "SUDO_USER": user,
                    "SUDO_UID": uid,
                    "SUDO_GID": gid,
                    "HOME": str(user_home),
                }
            )
            runner.require(
                "preflight-check-config",
                cli(installed_binary, personal_config, "check-config"),
                env=personal_env,
            )
            runner.require(
                "personal-systemd-install",
                cli(
                    installed_binary, personal_config, "deploy", "systemd", "--install"
                ),
                env=personal_env,
                input_text="yes\n",
            )
            wait_for(
                runner, "personal-active", ["systemctl", "is-active", SERVICE_NAME]
            )
            wait_http(f"http://127.0.0.1:{config_port(personal_config)}/v1/healthz")
            facts["personal_systemd"] = inspect_service(
                runner, personal_config, personal_env, provider.url
            )
            runner.require(
                "personal-rehash",
                user_cli(user, installed_binary, personal_config, "rehash", "--json"),
                env=personal_env,
            )
            runner.require("personal-restart", ["systemctl", "restart", SERVICE_NAME])
            wait_for(
                runner,
                "personal-restart-active",
                ["systemctl", "is-active", SERVICE_NAME],
            )
            runner.require(
                "personal-kill",
                [
                    "systemctl",
                    "kill",
                    "--kill-who=main",
                    "-s",
                    "SIGKILL",
                    SERVICE_NAME,
                ],
            )
            wait_for(
                runner,
                "personal-recovery",
                ["systemctl", "is-active", SERVICE_NAME],
                timeout=25,
            )
            wait_http(f"http://127.0.0.1:{config_port(personal_config)}/v1/healthz")
            if available("crontab"):
                runner.require(
                    "cron-install",
                    cli(
                        installed_binary,
                        personal_config,
                        "deploy",
                        "cron",
                        "--install",
                        "--user",
                        user,
                    ),
                    env=personal_env,
                    input_text="yes\n",
                )
                runner.require(
                    "cron-reinstall",
                    cli(
                        installed_binary,
                        personal_config,
                        "deploy",
                        "cron",
                        "--install",
                        "--user",
                        user,
                    ),
                    env=personal_env,
                    input_text="yes\n",
                )
                runner.require(
                    "cron-runtime-path",
                    [
                        "runuser",
                        "-u",
                        user,
                        "--preserve-environment",
                        "--",
                        "ls",
                        "-la",
                        str(user_home / ".local/state/eggpool"),
                    ],
                    env=personal_env,
                )
                cron_text = runner.require(
                    "cron-inspect", ["crontab", "-u", user, "-l"]
                )
                if (
                    cron_text.count("BEGIN EggPool watchdog") != 1
                    or "unrelated" in cron_text
                ):
                    raise QualificationError("cron managed block was not idempotent")
                runner.require(
                    "croncheck",
                    user_cli(user, installed_binary, personal_config, "croncheck"),
                    env=personal_env,
                )
            runner.require(
                "personal-keep-flags",
                cli(
                    installed_binary,
                    personal_config,
                    "uninstall",
                    "--yes",
                    "--keep-data",
                    "--keep-config",
                    "--keep-path",
                ),
                env=personal_env,
            )
            facts["personal_keep_flags"] = {
                "config": file_fact(personal_config),
                "data": file_fact(user_home / ".local/share/eggpool"),
                "state": file_fact(user_home / ".local/state/eggpool"),
                "unit": file_fact(Path("/etc/systemd/system/eggpool.service")),
            }
            if not all(
                item["exists"] for item in facts["personal_keep_flags"].values()
            ):
                raise QualificationError(
                    "personal uninstall keep flags did not preserve targets"
                )
            shutil.copy2(binary, installed_binary)
            installed_binary.chmod(0o755)
            runner.require(
                "personal-uninstall",
                cli(
                    installed_binary,
                    personal_config,
                    "uninstall",
                    "--yes",
                    "--deploy-artifacts",
                ),
                env=personal_env,
            )
            facts["personal_systemd"]["after_uninstall"] = managed_facts()
            shutil.copy2(binary, installed_binary)
            installed_binary.chmod(0o755)

            production_root = root / "production-source"
            production_config = production_root / "config.toml"
            write_config(
                production_config,
                database=Path("/var/lib/eggpool/usage.sqlite3"),
                backup=Path("/var/backups/eggpool"),
                port=pick_port(),
                provider=provider.url,
            )
            production_env = env_for(production_root, Path("/root"), production_config)
            production_cli_env = production_env.copy()
            for key in (
                "EGGPOOL_CONFIG",
                "EGGPOOL_RUNTIME_DIR",
                "EGGPOOL_PID_FILE",
                "EGGPOOL_LOG_FILE",
            ):
                production_cli_env.pop(key, None)
            MARKER.write_text(
                "rootful qualification disposable acceptance ownership marker\n",
                encoding="utf-8",
            )
            production_user_created = True
            runner.require(
                "production-systemd-install",
                cli(
                    installed_binary,
                    production_config,
                    "deploy",
                    "systemd",
                    "--install",
                    "--production",
                ),
                env=production_env,
                input_text="yes\n",
            )
            wait_for(
                runner, "production-active", ["systemctl", "is-active", SERVICE_NAME]
            )
            wait_http(
                f"http://127.0.0.1:{config_port(Path('/etc/eggpool/config.toml'))}/v1/healthz"
            )
            facts["production_systemd"] = inspect_service(
                runner, Path("/etc/eggpool/config.toml"), production_env, provider.url
            )
            runner.require(
                "production-rehash",
                cli(
                    installed_binary,
                    Path("/etc/eggpool/config.toml"),
                    "rehash",
                    "--json",
                ),
                env=production_cli_env,
            )
            runner.require("production-restart", ["systemctl", "restart", SERVICE_NAME])
            wait_for(
                runner,
                "production-restart-active",
                ["systemctl", "is-active", SERVICE_NAME],
            )
            if available("logrotate"):
                runner.require(
                    "logrotate-install",
                    cli(
                        installed_binary,
                        production_config,
                        "deploy",
                        "logrotate",
                        "--install",
                    ),
                    env=production_env,
                    input_text="yes\n",
                )
            else:
                facts["logrotate"] = {
                    "status": "not-applicable",
                    "reason": "optional command absent",
                }
            runner.require(
                "backup-cron-install",
                cli(
                    installed_binary,
                    production_config,
                    "deploy",
                    "backup-cron",
                    "--install",
                    "--production",
                ),
                env=production_env,
            )
            runner.require(
                "backup-command",
                [
                    "runuser",
                    "-u",
                    "eggpool",
                    "--",
                    str(Path("/usr/local/bin/eggpool-backup")),
                ],
                env=production_env,
            )
            facts["backup_artifacts"] = [
                file_fact(path) for path in Path("/var/backups/eggpool").glob("*.zip")
            ]
            facts["production_paths"] = [file_fact(path) for path in MANAGED_PATHS]
            runner.require(
                "production-uninstall",
                cli(
                    installed_binary,
                    Path("/etc/eggpool/config.toml"),
                    "uninstall",
                    "--yes",
                    "--deploy-artifacts",
                ),
                env=production_cli_env,
            )
            facts["after_uninstall"] = managed_facts()
            if any(item["exists"] for item in facts["after_uninstall"]):
                raise QualificationError(
                    "managed production leftovers remain after uninstall"
                )
            shutil.copy2(binary, installed_binary)
            installed_binary.chmod(0o755)
            runner.require(
                "production-uninstall-repeat",
                cli(
                    installed_binary,
                    PRODUCTION_CONFIG,
                    "uninstall",
                    "--yes",
                    "--deploy-artifacts",
                ),
                env=production_cli_env,
            )
            facts["after_repeat_uninstall"] = managed_facts()
            if any(item["exists"] for item in facts["after_repeat_uninstall"]):
                raise QualificationError(
                    "repeated production uninstall left managed targets"
                )
        facts["status"] = "pass"
    except Exception as error:
        facts["status"] = "fail"
        facts["reason"] = bounded(str(error))
        runner.run(
            "failure-status", ["systemctl", "status", SERVICE_NAME, "--no-pager"]
        )
        runner.run(
            "failure-journal",
            ["journalctl", "-u", SERVICE_NAME, "-n", "40", "--no-pager"],
        )
        raise
    finally:
        if user_created:
            cleanup_owned(runner, root, user)
        else:
            cleanup_owned(runner, root)
        if production_user_created:
            runner.run(
                "cleanup-production-user", ["userdel", "-r", "eggpool"], timeout=15
            )
        facts["commands"] = [record.as_dict(root) for record in runner.records]
        facts["host"] = host_facts()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return facts


def inspect_service(
    runner: Runner, config: Path, env: dict[str, str], provider: str
) -> dict[str, Any]:
    values = runner.require(
        "service-show",
        [
            "systemctl",
            "show",
            SERVICE_NAME,
            "-p",
            "Type",
            "-p",
            "User",
            "-p",
            "MainPID",
        ],
    )
    port = config_port(config)
    base_url = f"http://127.0.0.1:{port}"
    health_status = wait_http(f"{base_url}/v1/healthz")
    ready_status = wait_http(f"{base_url}/v1/readyz")
    models_status = authenticated_get_status(f"{base_url}/v1/models")
    finite = inference_observation(base_url, streaming=False)
    streaming = inference_observation(base_url, streaming=True)
    return {
        "systemd": bounded(values),
        "health_status": health_status,
        "ready_status": ready_status,
        "models_status": models_status,
        "finite_inference": finite,
        "streaming_inference": streaming,
        "config": str(config),
        "provider": provider.split("://", 1)[0],
        "unit_sha256": sha256(Path("/etc/systemd/system/eggpool.service")),
        "unit_mode_uid_gid": file_fact(Path("/etc/systemd/system/eggpool.service")),
    }


def inference_observation(base_url: str, *, streaming: bool) -> dict[str, Any]:
    body = json.dumps(
        {
            "model": "q006-fixture-model",
            "messages": [{"role": "user", "content": "q006"}],
            "stream": streaming,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=body,
        headers={
            "authorization": "Bearer q006-server-key",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read(1024 * 1024)
            status = int(response.status)
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        status = getattr(error, "code", None)
        raise QualificationError(
            f"inference probe failed: HTTP {status or 'connection error'}"
        ) from error
    if status != 200:
        raise QualificationError(f"inference probe returned HTTP {status}")
    terminal = b"data: [DONE]" in payload if streaming else b'"choices"' in payload
    if not terminal:
        raise QualificationError("inference probe lacked expected response evidence")
    return {"status": status, "bytes": len(payload), "terminal": terminal}


def config_port(path: Path) -> int:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("port ="):
            return int(line.split("=", 1)[1].strip())
    raise QualificationError(f"port missing from {path}")


def managed_facts() -> list[dict[str, Any]]:
    return [file_fact(path) for path in MANAGED_PATHS]


def pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def cleanup_mode() -> int:
    if not MARKER.is_file():
        raise QualificationError(
            f"cleanup refused: ownership marker is absent at {MARKER}"
        )
    runner = Runner(Path(tempfile.mkdtemp(prefix="eggpool-q006-cleanup-")))
    cleanup_owned(runner, runner.root, "eggpool")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="Rust candidate executable")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--cleanup", action="store_true", help="recover an interrupted marked run"
    )
    parser.add_argument(
        "--i-understand-disposable-host", action="store_true", dest="ack"
    )
    args = parser.parse_args(argv)
    if not args.ack:
        parser.error("refusing host mutation without --i-understand-disposable-host")
    try:
        if args.cleanup:
            return cleanup_mode()
        if args.binary is None or not args.binary.is_file():
            parser.error("--binary must name an existing Rust executable")
        run_qualification(args.binary.resolve(), args.output.resolve())
    except QualificationError as error:
        if args.output and not args.output.exists():
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    {
                        "schema": MANIFEST_VERSION,
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
