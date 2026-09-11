"""Run the bounded M10 Q005 target-build and non-root portability check.

The runner is intentionally qualification-only.  It starts the supplied Rust
candidate against a private loopback provider, exercises the ordinary local
runtime contract, and emits scalar evidence without retaining process output,
request bodies, or environment secrets.

Usage::

    uv run python scripts/qualification_portability.py \
        --binary rust/target/release/eggpool \
        --config-fixture \
        migration-rs/fixtures/qualification/config/q005-portability.toml \
        --target-id macos-arm64 \
        --output /tmp/q005.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = (
    ROOT / "migration-rs/fixtures/qualification/config/q005-portability.toml"
)
DEFAULT_OUTPUT = ROOT / "migration-rs/closure/qualification/005-run.json"
MAX_DIAGNOSTIC_BYTES = 768
MAX_METADATA_BYTES = 4096
Q005_MANIFEST_VERSION = "m10-q001.v1"

TARGETS: dict[str, dict[str, str]] = {
    "linux-x86_64": {
        "classification": "supported",
        "environment_class": "linux-x86_64-disposable",
    },
    "linux-aarch64": {
        "classification": "supported",
        "environment_class": "linux-aarch64-sbc",
    },
    "macos-arm64": {
        "classification": "supported-development",
        "environment_class": "macos-arm64-development",
    },
    "other-unix": {
        "classification": "not-qualified",
        "environment_class": "manual-local-deterministic",
    },
    "windows": {
        "classification": "unsupported",
        "environment_class": "manual-local-deterministic",
    },
}


class QualificationError(RuntimeError):
    """A mandatory Q005 observation failed."""


@dataclass(frozen=True)
class CommandResult:
    """Secret-free result for one bounded child-process command."""

    command_id: str
    argv: tuple[str, ...]
    returncode: int | None
    timed_out: bool
    duration_ms: int
    reason: str

    @property
    def status(self) -> str:
        if self.timed_out:
            return "timeout"
        if self.returncode is None:
            return "infrastructure-error"
        return "pass" if self.returncode == 0 else "fail"

    def to_dict(self, root: Path | None = None) -> dict[str, Any]:
        argv = [str(value) for value in self.argv]
        if root is not None:
            argv = [_display_path(value, root) for value in argv]
        return {
            "id": self.command_id,
            "command": argv,
            "status": self.status,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "reason": _bounded_text(self.reason),
        }


def _bounded_text(value: str) -> str:
    """Keep diagnostics small and remove common secret-shaped values."""
    text = " ".join(value.replace("\x00", " ").split())
    for marker in ("Bearer ", "bearer ", "sk-", "api_key=", "token="):
        if marker in text:
            text = text.split(marker, 1)[0] + "<redacted>"
    return text[:MAX_DIAGNOSTIC_BYTES]


def _display_path(value: str, root: Path) -> str:
    return value.replace(str(root), "<TEMP_ROOT>")


def target_for_platform(system: str | None = None, machine: str | None = None) -> str:
    """Map platform facts to the frozen Q001 target vocabulary."""
    system_name = (system or platform.system()).lower()
    machine_name = (machine or platform.machine()).lower()
    if system_name == "windows":
        return "windows"
    if system_name == "linux" and machine_name in {"x86_64", "amd64"}:
        return "linux-x86_64"
    if system_name == "linux" and machine_name in {"aarch64", "arm64"}:
        return "linux-aarch64"
    if system_name == "darwin" and machine_name in {"arm64", "aarch64"}:
        return "macos-arm64"
    if system is None and machine is None and _macos_arm64_hardware():
        return "macos-arm64"
    return "other-unix"


def _macos_arm64_hardware() -> bool:
    """Recognize an ARM Mac even when the shell is running under Rosetta."""
    if platform.system() != "Darwin":
        return False
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.optional.arm64"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return platform.machine().lower() in {"arm64", "aarch64"}
    return result.returncode == 0 and result.stdout.strip() == "1"


def _target_can_execute(target_id: str) -> bool:
    system_name = platform.system().lower()
    machine_name = platform.machine().lower()
    if target_id == "linux-x86_64":
        return system_name == "linux" and machine_name in {"x86_64", "amd64"}
    if target_id == "linux-aarch64":
        return system_name == "linux" and machine_name in {"aarch64", "arm64"}
    if target_id == "macos-arm64":
        return system_name == "darwin" and (
            machine_name in {"arm64", "aarch64"} or _macos_arm64_hardware()
        )
    return False


def _execution_prefix(target_id: str) -> tuple[str, ...]:
    if target_id == "macos-arm64" and platform.machine().lower() not in {
        "arm64",
        "aarch64",
    }:
        return ("/usr/bin/arch", "-arm64")
    return ()


def _execution_architecture(target_id: str) -> str:
    """Report the architecture used by the candidate process."""
    prefix = _execution_prefix(target_id)
    try:
        result = subprocess.run(
            [*prefix, "/usr/bin/uname", "-m"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unavailable"


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _environment(root: Path, config: Path) -> dict[str, str]:
    """Create an allowlisted child environment rooted in the temp tree."""
    excluded = {
        "SERVER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "EGGPOOL_E2E_OPENCODE_GO_API_KEY",
    }
    values = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EGGPOOL_") and key not in excluded
    }
    values.update(
        {
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "config-home"),
            "XDG_DATA_HOME": str(root / "data-home"),
            "XDG_STATE_HOME": str(root / "state-home"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "XDG_BACKUP_HOME": str(root / "backups"),
            "EGGPOOL_CONFIG": str(config),
            "EGGPOOL_RUNTIME_DIR": str(root / "runtime"),
            "EGGPOOL_PID_FILE": str(root / "runtime" / "eggpool.pid"),
            "EGGPOOL_LOG_FILE": str(root / "state-home" / "eggpool.log"),
            "TZ": "UTC",
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONHASHSEED": "0",
            "RUST_BACKTRACE": "0",
        }
    )
    return values


class _LoopbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    owner: ClassVar[LoopbackProvider]

    def log_message(self, format: str, *_args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/models":
            self._respond(
                200,
                b'{"object":"list","data":[{"id":"q005-fixture-model"}]}',
                "application/json",
            )
            return
        self._respond(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        self.owner.requests += 1
        try:
            payload_value: Any = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload_value = {}
        payload: dict[str, Any] = (
            cast("dict[str, Any]", payload_value)
            if isinstance(payload_value, dict)
            else {}
        )
        if payload.get("stream") is True:
            chunks = (
                b'data: {"id":"q005-stream","choices":[{"delta":'
                b'{"content":"ok"}}]}\n\n',
                b"data: [DONE]\n\n",
            )
            value = b"".join(chunks)
            self._respond(200, value, "text/event-stream")
            return
        value = json.dumps(
            {
                "id": "q005-finite",
                "object": "chat.completion",
                "model": "q005-fixture-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()
        self._respond(200, value, "application/json")

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(body)


class LoopbackProvider:
    """Small deterministic local provider used only by the runner."""

    def __init__(self) -> None:
        self.requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        _LoopbackHandler.owner = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> LoopbackProvider:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _run(
    command_id: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    input_text: str | None = None,
) -> CommandResult:
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(argv),
            cwd=cwd,
            env=dict(env),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as error:
        return CommandResult(
            command_id,
            tuple(str(value) for value in argv),
            None,
            True,
            round((time.monotonic() - started) * 1000),
            f"command timed out: {error}",
        )
    except OSError as error:
        return CommandResult(
            command_id,
            tuple(str(value) for value in argv),
            None,
            False,
            round((time.monotonic() - started) * 1000),
            f"command could not start: {type(error).__name__}",
        )
    diagnostic = result.stderr or result.stdout
    return CommandResult(
        command_id,
        tuple(str(value) for value in argv),
        result.returncode,
        False,
        round((time.monotonic() - started) * 1000),
        _bounded_text(diagnostic),
    )


def _http(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers or {}),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_METADATA_BYTES)
    except urllib.error.HTTPError as error:
        return error.code, error.read(MAX_METADATA_BYTES)


def _wait_http(
    process: subprocess.Popen[bytes], url: str, *, timeout: float
) -> tuple[int, bytes]:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise QualificationError(
                f"candidate exited during startup with code {code}"
            )
        try:
            return _http(url, timeout=1.0)
        except (OSError, urllib.error.URLError) as error:
            last_error = type(error).__name__
            time.sleep(0.1)
    raise QualificationError(f"candidate readiness timed out: {last_error}")


def _stop_process(process: subprocess.Popen[bytes], timeout: float) -> int | None:
    if process.poll() is not None:
        return process.returncode
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                return None
    return process.returncode


def _render_fixture(
    fixture: Path,
    destination: Path,
    *,
    port: int,
    upstream: str,
    database: Path,
    backup_dir: Path,
) -> str:
    content = fixture.read_text(encoding="utf-8")
    replacements = {
        "__Q005_PORT__": str(port),
        "__Q005_UPSTREAM__": upstream,
        "__Q005_DATABASE__": str(database),
        "__Q005_BACKUP_DIR__": str(backup_dir),
    }
    for marker, value in replacements.items():
        content = content.replace(marker, value)
    if "__Q005_" in content:
        raise QualificationError("Q005 config fixture has unresolved placeholders")
    destination.write_text(content, encoding="utf-8")
    return content


def _rehome_archive(
    archive: Path,
    destination: Path,
    *,
    source_config: Path,
    source_database: Path,
    target_config: Path,
    target_database: Path,
    source_backup: Path,
    target_backup: Path,
) -> None:
    """Copy an archive while changing only its private absolute restore roots."""
    with zipfile.ZipFile(archive) as source:
        members = {name: source.read(name) for name in source.namelist()}
    metadata = members["META"].decode("utf-8")
    metadata = re.sub(
        r"(?m)^config_path = .*?$",
        f"config_path = {json.dumps(str(target_config))}",
        metadata,
    )
    metadata = re.sub(
        r"(?m)^db_path = .*?$",
        f"db_path = {json.dumps(str(target_database))}",
        metadata,
    )
    config = members["config.toml"].decode("utf-8")
    config = config.replace(str(source_database), str(target_database))
    config = config.replace(str(source_backup), str(target_backup))
    members["META"] = metadata.encode("utf-8")
    members["config.toml"] = config.encode("utf-8")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as target:
        for name, data in members.items():
            target.writestr(name, data)


def _binary_metadata(binary: Path) -> dict[str, Any]:
    size = binary.stat().st_size
    tool = "otool" if platform.system() == "Darwin" else "ldd"
    try:
        result = subprocess.run(
            [tool, "-L", str(binary)] if tool == "otool" else [tool, str(binary)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return {
            "size_bytes": size,
            "dependency_tool": tool,
            "dependency_status": "pass" if result.returncode == 0 else "fail",
            "dependency_count": len(lines),
            "unresolved_count": sum("not found" in line for line in lines),
        }
    except (OSError, subprocess.TimeoutExpired):
        return {
            "size_bytes": size,
            "dependency_tool": tool,
            "dependency_status": "unavailable",
            "dependency_count": None,
            "unresolved_count": None,
        }


def _command(
    command_id: str,
    binary: Path,
    prefix: Sequence[str],
    args: Sequence[str],
    *,
    config: Path,
    root: Path,
    env: Mapping[str, str],
    timeout: float,
    input_text: str | None = None,
) -> CommandResult:
    argv = (*prefix, str(binary), "--config", str(config), *args)
    return _run(
        command_id,
        argv,
        cwd=ROOT,
        env=env,
        timeout=timeout,
        input_text=input_text,
    )


def run_qualification(
    *,
    binary: Path,
    config_fixture: Path = DEFAULT_FIXTURE,
    target_id: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Run Q005 and return bounded machine-readable evidence."""
    resolved_target = target_id or target_for_platform()
    if resolved_target not in TARGETS:
        raise QualificationError(f"unknown Q001 target: {resolved_target}")
    target = TARGETS[resolved_target]
    environment: dict[str, Any] = {
        "target_id": resolved_target,
        "classification": target["classification"],
        "environment_class": target["environment_class"],
        "os": platform.platform(aliased=True),
        "system": platform.system(),
        "architecture": platform.machine(),
        "execution_architecture": _execution_architecture(resolved_target),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "rust": _bounded_text(
            subprocess.run(
                ["rustc", "--version"], capture_output=True, text=True, check=False
            ).stdout
        ),
        "candidate_sha256": _sha256(binary) if binary.is_file() else None,
        "qualification_manifest": Q005_MANIFEST_VERSION,
    }
    report: dict[str, Any] = {
        "schema_version": "m10-q005.v1",
        "plan": "Q005",
        "status": "not-applicable",
        "target": environment,
        "commands": [],
        "runtime_steps": [],
        "artifacts": {},
    }
    if target["classification"] in {"unsupported", "not-qualified"}:
        report["reason"] = "explicit Q001 target classification; no runtime claim"
        return report
    if not binary.is_file():
        report["status"] = "fail"
        report["reason"] = "candidate binary does not exist"
        return report
    if not _target_can_execute(resolved_target):
        report["status"] = "blocked"
        report["reason"] = "host cannot execute the requested target natively"
        return report

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="eggpool-q005-") as temporary:
        root = Path(temporary)
        for name in (
            "home",
            "config-home",
            "data-home",
            "state-home",
            "runtime",
            "backups",
            "recovery",
        ):
            (root / name).mkdir()
        database = root / "data-home" / "usage.sqlite3"
        backup_dir = root / "backups"
        config = root / "config.toml"
        port = _port()
        with LoopbackProvider() as provider:
            content = _render_fixture(
                config_fixture,
                config,
                port=port,
                upstream=provider.base_url,
                database=database,
                backup_dir=backup_dir,
            )
            env = _environment(root, config)
            prefix = _execution_prefix(resolved_target)
            command_results: list[CommandResult] = []
            for command_id, args in (
                ("version", ("version",)),
                ("root-help", ("--help",)),
                ("check-config", ("check-config",)),
            ):
                result = _command(
                    command_id,
                    binary,
                    prefix,
                    args,
                    config=config,
                    root=root,
                    env=env,
                    timeout=timeout,
                )
                command_results.append(result)
                if result.status != "pass":
                    raise QualificationError(
                        f"{command_id}: {result.status} ({result.returncode}): "
                        f"{result.reason}"
                    )

            stdout_log = (root / "serve.stdout").open("wb")
            stderr_log = (root / "serve.stderr").open("wb")
            process = subprocess.Popen(
                [*prefix, str(binary), "--config", str(config), "serve", "--verbose"],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_log,
                stderr=stderr_log,
                start_new_session=True,
            )
            try:
                health_url = f"http://127.0.0.1:{port}/v1/healthz"
                status, _ = _wait_http(process, health_url, timeout=timeout)
                if status != 200:
                    raise QualificationError(f"health returned HTTP {status}")
                report["runtime_steps"].append({"id": "health", "status": "pass"})
                for step_id, path in (
                    ("readiness", "/v1/readyz"),
                    ("models", "/v1/models"),
                ):
                    status, body = _http(
                        f"http://127.0.0.1:{port}{path}",
                        headers={"Authorization": "Bearer q005-server-key"},
                    )
                    if status != 200:
                        raise QualificationError(f"{step_id} returned HTTP {status}")
                    if step_id == "models" and b"q005-fixture-model" not in body:
                        try:
                            model_payload_value = json.loads(body.decode("utf-8"))
                            model_payload = (
                                cast("dict[str, Any]", model_payload_value)
                                if isinstance(model_payload_value, dict)
                                else {}
                            )
                            model_items = model_payload.get("data", [])
                            model_ids = [
                                cast("dict[str, Any]", item).get("id")
                                for item in model_items
                                if isinstance(item, dict)
                            ]
                        except (
                            UnicodeDecodeError,
                            json.JSONDecodeError,
                            AttributeError,
                        ):
                            model_ids = []
                        raise QualificationError(
                            "models response omitted fixture model; "
                            f"body_bytes={len(body)}; ids={model_ids}"
                        )
                    report["runtime_steps"].append({"id": step_id, "status": "pass"})

                request = json.dumps(
                    {
                        "model": "q005-fixture-model",
                        "messages": [{"role": "user", "content": "ping"}],
                    }
                ).encode()
                status, body = _http(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    method="POST",
                    body=request,
                    headers={
                        "Authorization": "Bearer q005-server-key",
                        "Content-Type": "application/json",
                    },
                )
                if status != 200 or b'"choices"' not in body:
                    raise QualificationError(
                        f"finite inference returned HTTP {status}; "
                        f"body_bytes={len(body)}; "
                        f"upstream_requests={provider.requests}; "
                        "provider response did not contain a finite choices payload"
                    )
                report["runtime_steps"].append({"id": "finite-chat", "status": "pass"})

                stream_request = json.dumps(
                    {
                        "model": "q005-fixture-model",
                        "messages": [{"role": "user", "content": "ping"}],
                        "stream": True,
                    }
                ).encode()
                status, body = _http(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    method="POST",
                    body=stream_request,
                    headers={
                        "Authorization": "Bearer q005-server-key",
                        "Content-Type": "application/json",
                    },
                )
                if status != 200 or b"[DONE]" not in body:
                    raise QualificationError(
                        f"streaming inference returned HTTP {status}; "
                        f"body_bytes={len(body)}; upstream_requests={provider.requests}"
                    )
                report["runtime_steps"].append({"id": "stream-chat", "status": "pass"})

                for command_id, args in (
                    ("runtime-status", ("runtime-status", "--json")),
                ):
                    result = _command(
                        command_id,
                        binary,
                        prefix,
                        args,
                        config=config,
                        root=root,
                        env=env,
                        timeout=timeout,
                    )
                    command_results.append(result)
                    if result.status != "pass":
                        raise QualificationError(f"{command_id}: {result.reason}")
                report["runtime_steps"].append(
                    {"id": "runtime-status", "status": "pass"}
                )

                config.write_text(
                    content.replace(
                        "max_request_body_bytes = 1048576",
                        "max_request_body_bytes = 1048577",
                    ),
                    encoding="utf-8",
                )
                result = _command(
                    "rehash",
                    binary,
                    prefix,
                    ("rehash", "--json"),
                    config=config,
                    root=root,
                    env=env,
                    timeout=timeout,
                )
                command_results.append(result)
                if result.status != "pass":
                    raise QualificationError(f"rehash: {result.reason}")
                report["runtime_steps"].append({"id": "rehash", "status": "pass"})

                result = _command(
                    "backup",
                    binary,
                    prefix,
                    ("backup", "--output-dir", str(backup_dir)),
                    config=config,
                    root=root,
                    env=env,
                    timeout=timeout,
                )
                command_results.append(result)
                if result.status != "pass":
                    raise QualificationError(f"backup: {result.reason}")
                archives = sorted(backup_dir.glob("eggpool-backup-*.zip"))
                if len(archives) != 1:
                    raise QualificationError(
                        "backup did not publish exactly one archive"
                    )
                archive = archives[0]
                report["artifacts"]["backup"] = {
                    "member_count": len(zipfile.ZipFile(archive).namelist()),
                    "size_bytes": archive.stat().st_size,
                    "sha256": _sha256(archive),
                }

                recovery_root = root / "recovery"
                recovery_runtime = recovery_root / "runtime"
                recovery_runtime.mkdir()
                recovery_config = recovery_root / "config.toml"
                recovery_database = recovery_root / "usage.sqlite3"
                recovery_backup = recovery_root / "backups"
                relocated = root / "relocated-backup.zip"
                _rehome_archive(
                    archive,
                    relocated,
                    source_config=config,
                    source_database=database,
                    target_config=recovery_config,
                    target_database=recovery_database,
                    source_backup=backup_dir,
                    target_backup=recovery_backup,
                )
                recovery_env = dict(env)
                recovery_env["EGGPOOL_CONFIG"] = str(recovery_config)
                recovery_env["EGGPOOL_RUNTIME_DIR"] = str(recovery_runtime)
                recovery_env["EGGPOOL_PID_FILE"] = str(recovery_runtime / "eggpool.pid")
                recovery_env["EGGPOOL_LOG_FILE"] = str(
                    root / "recovery" / "state" / "eggpool.log"
                )
                result = _command(
                    "recover",
                    binary,
                    prefix,
                    ("recover", str(relocated)),
                    config=recovery_config,
                    root=root,
                    env=recovery_env,
                    timeout=timeout,
                    input_text="y\n",
                )
                command_results.append(result)
                if (
                    result.status != "pass"
                    or not recovery_config.is_file()
                    or not recovery_database.is_file()
                ):
                    raise QualificationError(f"recover: {result.reason}")
                report["runtime_steps"].append(
                    {"id": "backup-recover", "status": "pass"}
                )

                result = _command(
                    "stop",
                    binary,
                    prefix,
                    ("stop", "--timeout", "30"),
                    config=config,
                    root=root,
                    env=env,
                    timeout=timeout,
                )
                command_results.append(result)
                if result.status != "pass" or process.poll() is None:
                    raise QualificationError(f"stop: {result.reason}")
                report["runtime_steps"].append(
                    {"id": "graceful-stop", "status": "pass"}
                )

                process = subprocess.Popen(
                    [
                        *prefix,
                        str(binary),
                        "--config",
                        str(config),
                        "serve",
                        "--verbose",
                    ],
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_log,
                    stderr=stderr_log,
                    start_new_session=True,
                )
                status, _ = _wait_http(process, health_url, timeout=timeout)
                if status != 200:
                    raise QualificationError("restart health did not return HTTP 200")
                report["runtime_steps"].append(
                    {"id": "restart-reconcile", "status": "pass"}
                )
            except QualificationError as error:
                stderr_log.flush()
                try:
                    server_diagnostic = " ".join(
                        Path(path).read_text(encoding="utf-8", errors="replace")
                        for path in (stderr_log.name, stdout_log.name)
                    )
                except OSError:
                    server_diagnostic = ""
                raise QualificationError(
                    f"{error}; candidate_poll={process.poll()}; "
                    f"server={_bounded_text(server_diagnostic)}"
                ) from error
            finally:
                _stop_process(process, timeout=min(timeout, 5.0))
                stdout_log.close()
                stderr_log.close()
            environment["upstream_requests"] = provider.requests
            environment["duration_ms"] = round((time.monotonic() - started) * 1000)
            environment["binary"] = _binary_metadata(binary)
            environment["temp_root_private"] = True
            report["commands"] = [result.to_dict(root) for result in command_results]
            report["status"] = "pass"
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--config-fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--target-id", choices=tuple(TARGETS), default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_qualification(
            binary=args.binary,
            config_fixture=args.config_fixture,
            target_id=args.target_id,
            timeout=args.timeout,
        )
    except (OSError, QualificationError) as error:
        report = {
            "schema_version": "m10-q005.v1",
            "plan": "Q005",
            "status": "fail",
            "reason": _bounded_text(str(error)),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report.get("status"), "output": str(args.output)}))
    return 0 if report.get("status") in {"pass", "not-applicable"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
