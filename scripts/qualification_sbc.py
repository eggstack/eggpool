"""Run the bounded M10 Q008 qualification on a physical Linux aarch64 SBC.

The runner uses a private temporary root and a loopback-only provider. It
records only bounded, scalar evidence; process output, request bodies,
credentials, hostnames, addresses, and full environment values are excluded.

Usage::

    uv run python scripts/qualification_sbc.py \
        --binary rust/target/release/eggpool \
        --output migration-rs/closure/qualification/008-run.json

The command is intentionally refused unless Linux/aarch64 and a device-tree
board model are visible. This prevents a hosted ARM VM from being reported as
the mandatory physical SBC evidence.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import signal
import socket
import sqlite3
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
DEFAULT_FIXTURE = ROOT / "tests/migration_rs/fixtures/config/q008-sbc.toml"
DEFAULT_OUTPUT = ROOT / "migration-rs/closure/qualification/008-run.json"
SCHEMA_VERSION = "m10-q008.v1"
MANIFEST_VERSION = "m10-q001.v1"
MAX_REASON_BYTES = 768
MAX_HTTP_BODY_BYTES = 128 * 1024
COMMAND_TIMEOUT = 45.0
SAMPLE_SECONDS = 0.20
MODELS = {
    "chat_completions": "q008-chat",
    "responses": "q008-responses",
    "messages": "q008-messages",
}


class QualificationError(RuntimeError):
    """A mandatory Q008 observation failed."""


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

    def as_dict(self, root: Path, binary: Path) -> dict[str, Any]:
        command: list[str] = []
        for value in self.argv:
            text = str(value).replace(str(root), "<TEMP_ROOT>")
            text = text.replace(str(binary), "<CANDIDATE>")
            command.append(text)
        return {
            "id": self.command_id,
            "command": command,
            "status": self.status,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "reason": bounded(self.reason),
        }


def bounded(value: str) -> str:
    """Keep diagnostics small and remove common secret-shaped values."""
    text = " ".join(value.replace("\x00", " ").split())
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer <redacted>", text)
    text = re.sub(
        r"(?i)(api[_-]?key|token|password|secret)=\S+", r"\1=<redacted>", text
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "<redacted>", text)
    text = re.sub(r"https?://[^\s/@:]+:[^\s/@]+@", "<url-credentials>@", text)
    return text.encode("utf-8", "replace")[:MAX_REASON_BYTES].decode("utf-8", "replace")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip("\x00\n ")
    except OSError:
        return None


def _os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    text = _read_text(Path("/etc/os-release")) or ""
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"NAME", "VERSION_ID", "PRETTY_NAME"}:
            values[key] = value.strip('"')[:128]
    return values


def _root_mount_source() -> str | None:
    """Return the sanitized source device for the root filesystem."""
    mounts = _read_text(Path("/proc/mounts")) or ""
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "/":
            return fields[0]
    return None


def _root_block_device(source: str | None) -> str | None:
    """Resolve a partition source to its parent block-device name."""
    if not source or not source.startswith("/dev/"):
        return None
    name = Path(source).name
    for pattern in (r"^(mmcblk\d+)p\d+$", r"^(nvme\d+n\d+)p\d+$"):
        match = re.match(pattern, name)
        if match:
            return match.group(1)
    match = re.match(r"^([a-z]+)\d+$", name)
    return match.group(1) if match else name


def _storage_device_class(device: str | None) -> str | None:
    """Return a non-identifying class for a root block device."""
    if not device:
        return None
    if device.startswith("mmcblk"):
        return "mmc"
    if device.startswith("nvme"):
        return "nvme"
    if device.startswith("sd"):
        return "scsi-disk"
    return "block-device"


def board_metadata() -> tuple[dict[str, Any] | None, str | None]:
    """Return board facts, or why physical SBC evidence is unavailable."""
    if platform.system().lower() != "linux":
        return None, "Q008 requires Linux on a physical aarch64 SBC"
    architecture = platform.machine().lower()
    if architecture not in {"aarch64", "arm64"}:
        return (
            None,
            "Q008 requires aarch64; hosted or translated execution is refused",
        )
    model = next(
        (
            value
            for value in (
                _read_text(Path("/sys/firmware/devicetree/base/model")),
                _read_text(Path("/proc/device-tree/model")),
            )
            if value
        ),
        None,
    )
    if not model:
        return (
            None,
            "device-tree board model is unavailable; physical SBC evidence "
            "cannot be established",
        )
    mem_total = _read_text(Path("/proc/meminfo")) or ""
    memory_kib = next(
        (
            int(match.group(1))
            for match in re.finditer(r"(?m)^MemTotal:\s+(\d+)\s+kB", mem_total)
        ),
        None,
    )
    frequency_policy = next(
        (
            value
            for value in (
                _read_text(
                    Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
                ),
                _read_text(
                    Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
                ),
            )
            if value
        ),
        None,
    )
    governors = _read_text(
        Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    )
    thermal = _read_text(Path("/sys/class/thermal/thermal_zone0/temp"))
    thermal_celsius = None
    if thermal and thermal.isdigit():
        thermal_celsius = round(int(thermal) / 1000, 1)
    root_source = _root_mount_source()
    root_device = _root_block_device(root_source)
    rotational = (
        _read_text(Path(f"/sys/class/block/{root_device}/queue/rotational"))
        if root_device
        else None
    )
    storage = (
        "rotational"
        if rotational == "1"
        else "non-rotational"
        if rotational == "0"
        else "unavailable"
    )
    filesystem = "unavailable"
    mounts = _read_text(Path("/proc/mounts")) or ""
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "/":
            filesystem = fields[2][:64]
            break
    release = _os_release()
    return {
        "board_model": model[:160],
        "soc_cpu_core_count": os.cpu_count(),
        "cpu_frequency_policy": frequency_policy,
        "cpu_governor": governors,
        "ram_bytes": memory_kib * 1024 if memory_kib is not None else None,
        "storage_medium_class": storage,
        "root_storage_device_class": _storage_device_class(root_device),
        "filesystem": filesystem,
        "os": release.get("PRETTY_NAME") or release.get("NAME"),
        "os_version_id": release.get("VERSION_ID"),
        "kernel": platform.release(),
        "architecture": architecture,
        "power_thermal_mode_celsius": thermal_celsius,
        "attestation": "Linux aarch64 plus device-tree board model",
    }, None


class _LoopbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    owner: ClassVar[LoopbackProvider]

    def log_message(self, format: str, *_args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/models":
            self._respond(
                200,
                json.dumps(
                    {
                        "object": "list",
                        "data": [{"id": model} for model in MODELS.values()],
                    }
                ).encode(),
            )
            return
        self._respond(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(min(length, MAX_HTTP_BODY_BYTES))
        self.owner.requests += 1
        streaming = b'"stream":true' in body.replace(b" ", b"")
        if self.path.endswith("/chat/completions"):
            response = (
                b'data: {"id":"q008-stream","choices":[{"delta":{"content":"ok"}}]}\n\n'
                b"data: [DONE]\n\n"
                if streaming
                else b'{"id":"q008-chat","object":"chat.completion",'
                b'"model":"q008-chat",'
                b'"choices":[{"index":0,"message":{"role":"assistant","content":"ok"},'
                b'"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
            )
            self._respond(
                200, response, "text/event-stream" if streaming else "application/json"
            )
            return
        if self.path.endswith("/responses"):
            response = (
                b"event: response.output_text.delta\n"
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                b"event: response.completed\n"
                b'data: {"type":"response.completed","response":{"id":"q008-stream",'
                b'"status":"completed","usage":{"input_tokens":1,"output_tokens":1,'
                b'"total_tokens":2}}}\n\n'
                if streaming
                else b'{"id":"q008-responses","object":"response","status":"completed",'
                b'"error":null,'
                b'"model":"q008-responses","output":[{"type":"message","id":"msg",'
                b'"status":"completed","role":"assistant","content":[{"type":"output_text",'
                b'"text":"ok","annotations":[]}]}],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}'
            )
            self._respond(
                200, response, "text/event-stream" if streaming else "application/json"
            )
            return
        if self.path.endswith("/messages"):
            response = (
                b"event: message_start\n"
                b'data: {"type":"message_start","message":{"id":"q008-stream"}}\n\n'
                b"event: content_block_delta\n"
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
                b"event: message_delta\n"
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                b'"usage":{"input_tokens":1,"output_tokens":1}}\n\n'
                b"event: message_stop\n"
                b'data: {"type":"message_stop"}\n\n'
                if streaming
                else b'{"id":"q008-messages","type":"message","role":"assistant",'
                b'"model":"q008-messages","content":[{"type":"text","text":"ok"}],'
                b'"stop_reason":"end_turn","stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":1}}'
            )
            self._respond(
                200, response, "text/event-stream" if streaming else "application/json"
            )
            return
        self._respond(404, b"not found", "text/plain")

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
    """Threaded loopback-only provider with structural request counters."""

    def __init__(self) -> None:
        self.requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        _LoopbackHandler.owner = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> LoopbackProvider:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _http(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_HTTP_BODY_BYTES)
    except urllib.error.HTTPError as error:
        return error.code, error.read(MAX_HTTP_BODY_BYTES)


def _timed_http(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, bytes, int, int | None]:
    """Return a bounded response plus total and first-byte timings."""
    request = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            first = response.read(1)
            ttft_ms = round((time.monotonic() - started) * 1000) if first else None
            body_value = first + response.read(MAX_HTTP_BODY_BYTES - len(first))
            return (
                response.status,
                body_value,
                round((time.monotonic() - started) * 1000),
                ttft_ms,
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            error.read(MAX_HTTP_BODY_BYTES),
            round((time.monotonic() - started) * 1000),
            None,
        )


def _run(
    command_id: str,
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    input_text: str | None = None,
) -> CommandResult:
    started = time.monotonic()
    try:
        result = subprocess.run(
            list(argv),
            cwd=ROOT,
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
            tuple(str(item) for item in argv),
            None,
            True,
            round((time.monotonic() - started) * 1000),
            str(error),
        )
    except OSError as error:
        return CommandResult(
            command_id,
            tuple(str(item) for item in argv),
            None,
            False,
            round((time.monotonic() - started) * 1000),
            f"command could not start: {type(error).__name__}",
        )
    return CommandResult(
        command_id,
        tuple(str(item) for item in argv),
        result.returncode,
        False,
        round((time.monotonic() - started) * 1000),
        result.stderr or result.stdout,
    )


def _command(
    command_id: str,
    binary: Path,
    config: Path,
    env: Mapping[str, str],
    args: Sequence[str],
    timeout: float,
    input_text: str | None = None,
) -> CommandResult:
    return _run(
        command_id,
        (str(binary), "--config", str(config), *args),
        env=env,
        timeout=timeout,
        input_text=input_text,
    )


def _wait_ready(process: subprocess.Popen[str], url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise QualificationError(
                f"candidate exited during startup with code {process.returncode}"
            )
        try:
            status, _ = _http(url, timeout=1)
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.1)
    raise QualificationError("candidate readiness timed out")


def _stop(process: subprocess.Popen[str] | None, timeout: float) -> int | None:
    if process is None or process.poll() is not None:
        return None if process is None else process.returncode
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


def _proc_value(pid: int, key: str) -> int | None:
    text = _read_text(Path(f"/proc/{pid}/status")) or ""
    match = re.search(rf"(?m)^{re.escape(key)}:\s+(\d+)\s+kB", text)
    return int(match.group(1)) * 1024 if match else None


def _proc_count(pid: int, key: str) -> int | None:
    text = _read_text(Path(f"/proc/{pid}/status")) or ""
    match = re.search(rf"(?m)^{re.escape(key)}:\s+(\d+)", text)
    return int(match.group(1)) if match else None


def _proc_cpu(pid: int) -> tuple[int, int] | None:
    text = _read_text(Path(f"/proc/{pid}/stat"))
    if not text:
        return None
    fields = text.rsplit(")", 1)[-1].split()
    if len(fields) < 13:
        return None
    return int(fields[11]) + int(fields[12]), int(
        os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    )


def _db_counts(database: Path) -> dict[str, Any]:
    counts: dict[str, Any] = {
        "requests": None,
        "attempts": None,
        "reservations": None,
        "pending_requests": None,
        "active_reservations": None,
        "request_statuses": {},
    }
    if not database.is_file():
        return counts
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
        try:
            for table in ("requests", "request_attempts", "reservations"):
                counts[table] = int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
            counts["attempts"] = counts.pop("request_attempts")
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM requests GROUP BY status"
            ).fetchall()
            counts["request_statuses"] = {
                str(status): int(number) for status, number in rows
            }
            counts["pending_requests"] = counts["request_statuses"].get("pending", 0)
            counts["active_reservations"] = int(
                connection.execute(
                    "SELECT COUNT(*) FROM reservations WHERE status = 'active'"
                ).fetchone()[0]
            )
        finally:
            connection.close()
    except sqlite3.Error:
        counts["read_error"] = True
    return counts


def _runtime_observations(value: Mapping[str, Any]) -> dict[str, Any]:
    manager = value.get("runtime_manager")
    manager_map: dict[str, Any] = (
        cast("dict[str, Any]", manager) if isinstance(manager, dict) else {}
    )
    active = manager_map.get("active_generation")
    active_map: dict[str, Any] = (
        cast("dict[str, Any]", active) if isinstance(active, dict) else {}
    )
    retiring = manager_map.get("retiring_generations")
    retiring_list: list[Any] = (
        cast("list[Any]", retiring) if isinstance(retiring, list) else []
    )
    tasks = manager_map.get("tasks") or value.get("background_tasks")
    task_list: list[Any] = cast("list[Any]", tasks) if isinstance(tasks, list) else []
    task_maps = [
        cast("dict[str, Any]", task) for task in task_list if isinstance(task, dict)
    ]

    def retiring_sum(key: str) -> int:
        total = 0
        for raw_item in retiring_list:
            if not isinstance(raw_item, dict):
                continue
            item = cast("dict[str, Any]", raw_item)
            number = item.get(key)
            if isinstance(number, int):
                total += number
        return total

    return {
        "runtime_tasks": len(task_list),
        "running_tasks": sum(1 for task in task_maps if task.get("running")),
        "generation_count": 1 + len(retiring_list) if active_map else None,
        "active_generation_id": active_map.get("generation_id"),
        "active_leases": active_map.get("active_leases"),
        "retiring_generations": len(retiring_list) if active_map else None,
        "retiring_leases": retiring_sum("active_leases"),
        "terminal_references": retiring_sum("terminal_references"),
        "finalization_jobs": active_map.get("finalization_active_jobs"),
        "finalization_capacity": active_map.get("finalization_capacity"),
        "wire_flights": manager_map.get("wire_flights"),
    }


def resource_sample(
    label: str,
    process: subprocess.Popen[str],
    database: Path,
    runtime_url: str,
    *,
    duration: float = SAMPLE_SECONDS,
    server_api_key: str = "q008-server-key",
) -> dict[str, Any]:
    """Capture a bounded procfs/runtime/database snapshot."""
    pid = process.pid
    first = _proc_cpu(pid)
    started = time.monotonic()
    time.sleep(duration)
    second = _proc_cpu(pid)
    elapsed = max(time.monotonic() - started, 0.001)
    cpu_percent = None
    if first and second and first[1] == second[1]:
        cpu_percent = round(max(second[0] - first[0], 0) / first[1] / elapsed * 100, 2)
    runtime_read_error = False
    try:
        _, body = _http(
            runtime_url, headers={"Authorization": f"Bearer {server_api_key}"}
        )
        runtime_value = json.loads(body.decode("utf-8"))
        runtime = (
            cast("dict[str, Any]", runtime_value)
            if isinstance(runtime_value, dict)
            else {}
        )
    except (OSError, ValueError):
        runtime_read_error = True
        runtime = {}
    db = _db_counts(database)
    fd_count = None
    with contextlib.suppress(OSError):
        fd_count = len(list(Path(f"/proc/{pid}/fd").iterdir()))
    sample = {
        "label": label,
        "sample_window_ms": round(duration * 1000),
        "rss_bytes": _proc_value(pid, "VmRSS"),
        "virtual_bytes": _proc_value(pid, "VmSize"),
        "cpu_percent": cpu_percent,
        "open_fd_count": fd_count,
        "thread_count": _proc_count(pid, "Threads"),
        "db_bytes": database.stat().st_size if database.exists() else 0,
        "wal_bytes": database.with_name(database.name + "-wal").stat().st_size
        if database.with_name(database.name + "-wal").exists()
        else 0,
        "runtime_read_error": runtime_read_error,
        **_runtime_observations(runtime),
        **db,
    }
    return sample


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
        "__Q008_PORT__": str(port),
        "__Q008_UPSTREAM__": upstream,
        "__Q008_DATABASE__": str(database),
        "__Q008_BACKUP_DIR__": str(backup_dir),
    }
    for marker, replacement in replacements.items():
        content = content.replace(marker, replacement)
    if "__Q008_" in content:
        raise QualificationError("Q008 config fixture has unresolved placeholders")
    destination.write_text(content, encoding="utf-8")
    return content


def _rehome_archive(
    archive: Path,
    destination: Path,
    *,
    source_database: Path,
    target_database: Path,
    source_backup: Path,
    target_backup: Path,
    target_config: Path,
) -> None:
    with zipfile.ZipFile(archive) as source:
        members = {name: source.read(name) for name in source.namelist()}
    if "config.toml" not in members or "META" not in members:
        raise QualificationError("backup archive omitted required config/META members")
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
    config = config.replace(str(source_database), str(target_database)).replace(
        str(source_backup), str(target_backup)
    )
    members["META"] = metadata.encode()
    members["config.toml"] = config.encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as target:
        for name, data in members.items():
            target.writestr(name, data)


def _environment(root: Path, config: Path) -> dict[str, str]:
    values = {
        "PATH": os.environ.get("PATH", ""),
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
    return values


def _request_timed(
    port: int, surface: str, model: str, streaming: bool
) -> tuple[int, bytes, int, int | None]:
    paths = {
        "chat_completions": "/v1/chat/completions",
        "responses": "/v1/responses",
        "messages": "/v1/messages",
    }
    if surface == "messages":
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "ping"}],
        }
    elif surface == "responses":
        payload = {"model": model, "input": "ping", "store": False}
    else:
        payload = {"model": model, "messages": [{"role": "user", "content": "ping"}]}
    if streaming:
        payload["stream"] = True
    return _timed_http(
        f"http://127.0.0.1:{port}{paths[surface]}",
        method="POST",
        body=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer q008-server-key",
            "Content-Type": "application/json",
        },
    )


def run_qualification(
    *,
    binary: Path,
    config_fixture: Path = DEFAULT_FIXTURE,
    timeout: float = COMMAND_TIMEOUT,
    expected_sha256: str | None = None,
    candidate_origin: str = "supplied-candidate",
    build_elapsed_ms: int | None = None,
) -> dict[str, Any]:
    """Run Q008 and return a bounded machine-readable report."""
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "plan": "Q008",
        "manifest": MANIFEST_VERSION,
        "status": "blocked",
        "functional": [],
        "resource_samples": [],
        "commands": [],
        "findings": [],
    }
    board, block_reason = board_metadata()
    if block_reason:
        report["reason"] = block_reason
        report["environment"] = {
            "system": platform.system(),
            "architecture": platform.machine(),
        }
        return report
    environment = cast("dict[str, Any]", board)
    report["environment"] = environment
    if not binary.is_file():
        report["status"] = "fail"
        report["reason"] = "candidate binary does not exist"
        return report
    candidate_hash = sha256(binary)
    report["candidate"] = {
        "sha256": candidate_hash,
        "build_profile": "release",
        "build_mode": candidate_origin,
        "binary_size_bytes": binary.stat().st_size,
    }
    if build_elapsed_ms is not None:
        report["candidate"]["build_elapsed_ms"] = build_elapsed_ms
    if expected_sha256 and candidate_hash != expected_sha256.lower():
        report["status"] = "fail"
        report["reason"] = "candidate SHA-256 does not match --expected-sha256"
        return report
    try:
        rust = subprocess.run(
            ["rustc", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        report["candidate"]["rust_toolchain"] = bounded(rust.stdout)
    except (OSError, subprocess.TimeoutExpired):
        report["candidate"]["rust_toolchain"] = "unavailable"

    process: subprocess.Popen[str] | None = None
    with tempfile.TemporaryDirectory(prefix="eggpool-q008-") as temporary:
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
        recovery_root = root / "recovery"
        recovery_root.mkdir(exist_ok=True)
        port = free_port()
        env = _environment(root, config)
        commands: list[CommandResult] = []
        samples: list[dict[str, Any]] = []
        workload_timings: dict[str, list[int]] = {
            "finite_elapsed_ms": [],
            "finite_ttft_ms": [],
            "streaming_elapsed_ms": [],
            "streaming_ttft_ms": [],
            "second_workload_elapsed_ms": [],
            "second_workload_ttft_ms": [],
        }
        with LoopbackProvider() as provider:
            content = _render_fixture(
                config_fixture,
                config,
                port=port,
                upstream=provider.base_url,
                database=database,
                backup_dir=backup_dir,
            )
            for command_id, args in (
                ("version", ("version",)),
                ("help", ("--help",)),
                ("check-config", ("check-config",)),
                ("migrate", ("migrate",)),
            ):
                result = _command(command_id, binary, config, env, args, timeout)
                commands.append(result)
                if result.status != "pass":
                    raise QualificationError(f"{command_id}: {result.reason}")
            log_out = (root / "stdout.log").open("w", encoding="utf-8")
            log_err = (root / "stderr.log").open("w", encoding="utf-8")
            try:
                started = time.monotonic()
                process = subprocess.Popen(
                    [str(binary), "--config", str(config), "serve", "--verbose"],
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_out,
                    stderr=log_err,
                    start_new_session=True,
                    text=True,
                )
                _wait_ready(process, f"http://127.0.0.1:{port}/v1/healthz", timeout)
                report["startup_to_ready_ms"] = round(
                    (time.monotonic() - started) * 1000
                )
                for step_id, path in (
                    ("migration-startup", "/v1/readyz"),
                    ("model-listing", "/v1/models"),
                ):
                    status, body = _http(
                        f"http://127.0.0.1:{port}{path}",
                        headers={"Authorization": "Bearer q008-server-key"},
                    )
                    passed = status == 200 and (
                        step_id != "model-listing"
                        or all(model.encode() in body for model in MODELS.values())
                    )
                    report["functional"].append(
                        {
                            "id": step_id,
                            "status": "pass" if passed else "fail",
                            "http_status": status,
                            "body_bytes": len(body),
                        }
                    )
                    if not passed:
                        raise QualificationError(f"{step_id} returned HTTP {status}")
                samples.append(
                    resource_sample(
                        "before-warmup",
                        process,
                        database,
                        f"http://127.0.0.1:{port}/api/stats/runtime",
                    )
                )
                time.sleep(2.25)
                samples.append(
                    resource_sample(
                        "after-warmup",
                        process,
                        database,
                        f"http://127.0.0.1:{port}/api/stats/runtime",
                    )
                )
                for surface, model in MODELS.items():
                    status, body, elapsed_ms, ttft_ms = _request_timed(
                        port, surface, model, False
                    )
                    passed = status == 200 and len(body) > 0
                    report["functional"].append(
                        {
                            "id": f"{surface}-finite",
                            "status": "pass" if passed else "fail",
                            "http_status": status,
                            "body_bytes": len(body),
                            "elapsed_ms": elapsed_ms,
                            "ttft_ms": ttft_ms,
                        }
                    )
                    workload_timings["finite_elapsed_ms"].append(elapsed_ms)
                    if ttft_ms is not None:
                        workload_timings["finite_ttft_ms"].append(ttft_ms)
                    if not passed:
                        raise QualificationError(
                            f"{surface} finite request returned HTTP {status}"
                        )
                for surface, model in MODELS.items():
                    status, body, elapsed_ms, ttft_ms = _request_timed(
                        port, surface, model, True
                    )
                    markers = {
                        "chat_completions": b"[DONE]",
                        "responses": b"response.completed",
                        "messages": b"message_stop",
                    }
                    passed = status == 200 and markers[surface] in body
                    report["functional"].append(
                        {
                            "id": f"{surface}-stream",
                            "status": "pass" if passed else "fail",
                            "http_status": status,
                            "body_bytes": len(body),
                            "terminal_evidence": markers[surface] in body,
                            "elapsed_ms": elapsed_ms,
                            "ttft_ms": ttft_ms,
                        }
                    )
                    workload_timings["streaming_elapsed_ms"].append(elapsed_ms)
                    if ttft_ms is not None:
                        workload_timings["streaming_ttft_ms"].append(ttft_ms)
                    if not passed:
                        raise QualificationError(
                            f"{surface} stream lacked terminal evidence"
                        )
                for command_id, args in (
                    ("accounts-list", ("accounts", "list")),
                    ("modelinfo-list", ("modelinfo", "list")),
                    ("operator-stats", ("stats", "explain-dashboard", "--json")),
                    ("transcoding-stats", ("stats", "transcoding", "--json")),
                    ("runtime-status", ("runtime-status", "--json")),
                ):
                    result = _command(command_id, binary, config, env, args, timeout)
                    commands.append(result)
                    if result.status != "pass":
                        raise QualificationError(f"{command_id}: {result.reason}")
                    report["functional"].append({"id": command_id, "status": "pass"})
                for path in (
                    "/",
                    "/static/dashboard.css",
                    "/static/dashboard.js",
                    "/static/chart.js",
                    "/static/favicon.svg",
                    "/static/theme.css?theme=Nord",
                ):
                    status, body = _http(
                        f"http://127.0.0.1:{port}{path}",
                        headers={"Authorization": "Bearer q008-server-key"},
                    )
                    passed = status == 200 and len(body) > 0
                    dashboard_id = path.strip("/").replace("/", "-") or "page"
                    report["functional"].append(
                        {
                            "id": f"dashboard-{dashboard_id}",
                            "status": "pass" if passed else "fail",
                            "http_status": status,
                            "body_bytes": len(body),
                        }
                    )
                    if not passed:
                        raise QualificationError(
                            f"dashboard path {path} returned HTTP {status}"
                        )
                samples.append(
                    resource_sample(
                        "after-first-workload",
                        process,
                        database,
                        f"http://127.0.0.1:{port}/api/stats/runtime",
                    )
                )
                config.write_text(
                    content.replace("flush_interval_s = 2", "flush_interval_s = 3"),
                    encoding="utf-8",
                )
                result = _command(
                    "rehash", binary, config, env, ("rehash", "--json"), timeout
                )
                commands.append(result)
                if result.status != "pass":
                    raise QualificationError(f"rehash: {result.reason}")
                report["functional"].append(
                    {"id": "rehash-runtime-status", "status": "pass"}
                )
                result = _command(
                    "backup",
                    binary,
                    config,
                    env,
                    ("backup", "--output-dir", str(backup_dir)),
                    timeout,
                )
                commands.append(result)
                if result.status != "pass":
                    raise QualificationError(f"backup: {result.reason}")
                archives = sorted(backup_dir.glob("eggpool-backup-*.zip"))
                if not archives:
                    raise QualificationError("backup did not publish an archive")
                archive = archives[-1]
                with zipfile.ZipFile(archive) as archive_reader:
                    member_count = len(archive_reader.namelist())
                report["backup"] = {
                    "elapsed_ms": commands[-1].duration_ms,
                    "archive_size_bytes": archive.stat().st_size,
                    "archive_sha256": sha256(archive),
                    "member_count": member_count,
                }
                samples.append(
                    resource_sample(
                        "after-rehash-backup",
                        process,
                        database,
                        f"http://127.0.0.1:{port}/api/stats/runtime",
                    )
                )
                _stop(process, min(timeout, 10))
                if process.poll() is None:
                    raise QualificationError("candidate did not stop before recovery")
                process = None
                recovery_config = recovery_root / "config.toml"
                recovery_database = recovery_root / "usage.sqlite3"
                recovery_backup = recovery_root / "backups"
                recovery_backup.mkdir()
                recovery_archive = recovery_root / "recovered.zip"
                _rehome_archive(
                    archive,
                    recovery_archive,
                    source_database=database,
                    target_database=recovery_database,
                    source_backup=backup_dir,
                    target_backup=recovery_backup,
                    target_config=recovery_config,
                )
                recovery_config.write_text(
                    content.replace(str(database), str(recovery_database)).replace(
                        str(backup_dir), str(recovery_backup)
                    ),
                    encoding="utf-8",
                )
                recovery_env = _environment(recovery_root, recovery_config)
                result = _command(
                    "recover",
                    binary,
                    recovery_config,
                    recovery_env,
                    ("recover", str(recovery_archive)),
                    timeout,
                    input_text="y\n",
                )
                commands.append(result)
                if result.status != "pass" or not recovery_database.is_file():
                    raise QualificationError(f"recover: {result.reason}")
                report["functional"].append(
                    {
                        "id": "backup-recover-isolated",
                        "status": "pass",
                        "recovered_database": True,
                    }
                )
                result = _command(
                    "vacuum", binary, config, env, ("db", "vacuum"), timeout
                )
                commands.append(result)
                if result.status != "pass":
                    raise QualificationError(f"vacuum: {result.reason}")
                report["functional"].append(
                    {"id": "bounded-maintenance", "status": "pass"}
                )
                process = subprocess.Popen(
                    [str(binary), "--config", str(config), "serve", "--verbose"],
                    cwd=ROOT,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_out,
                    stderr=log_err,
                    start_new_session=True,
                    text=True,
                )
                _wait_ready(process, f"http://127.0.0.1:{port}/v1/healthz", timeout)
                report["functional"].append(
                    {"id": "restart-reconcile", "status": "pass"}
                )
                for surface, model in list(MODELS.items())[:2]:
                    status, body, elapsed_ms, ttft_ms = _request_timed(
                        port, surface, model, False
                    )
                    if status != 200 or not body:
                        raise QualificationError(
                            f"second workload {surface} failed with HTTP {status}"
                        )
                    workload_timings["second_workload_elapsed_ms"].append(elapsed_ms)
                    if ttft_ms is not None:
                        workload_timings["second_workload_ttft_ms"].append(ttft_ms)
                samples.append(
                    resource_sample(
                        "after-second-workload",
                        process,
                        database,
                        f"http://127.0.0.1:{port}/api/stats/runtime",
                    )
                )
                result = _command(
                    "stop", binary, config, env, ("stop", "--timeout", "30"), timeout
                )
                commands.append(result)
                if result.status != "pass":
                    raise QualificationError(f"stop: {result.reason}")
                if process.poll() is None:
                    raise QualificationError(
                        "stop reported success but candidate remained running"
                    )
                report["functional"].append(
                    {"id": "graceful-shutdown", "status": "pass"}
                )
                counts = _db_counts(database)
                if counts.get("pending_requests") not in {0, None} or counts.get(
                    "active_reservations"
                ) not in {0, None}:
                    raise QualificationError(
                        "request/reservation state did not converge"
                    )
                report["durable"] = counts
                report["status"] = "pass"
                report["python_reference"] = {
                    "status": "not-run",
                    "reason": "optional same-board comparison was not supplied",
                }
                report["repeated_run_stability"] = {
                    "status": "pass",
                    "samples": len(samples),
                    "logical_leaks": False,
                    "rss_bytes": [sample["rss_bytes"] for sample in samples],
                    "fd_counts": [sample["open_fd_count"] for sample in samples],
                    "thread_counts": [sample["thread_count"] for sample in samples],
                    "interpretation": (
                        "bounded samples for M11 characterization; no fixed "
                        "performance threshold applied"
                    ),
                }
                report["request_workload_timings"] = {
                    "sample_count": sum(
                        len(values) for values in workload_timings.values()
                    ),
                    "elapsed_ms": workload_timings,
                    "interpretation": (
                        "bounded client-observed timings; first-byte values are "
                        "diagnostic characterization, not an SLA"
                    ),
                }
            finally:
                _stop(process, min(timeout, 5))
                log_out.close()
                log_err.close()
            environment["loopback_provider_requests"] = provider.requests
        report["resource_samples"] = samples
        report["commands"] = [item.as_dict(root, binary) for item in commands]
        report["isolated_temporary_root"] = True
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--config-fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--expected-sha256")
    parser.add_argument(
        "--build-elapsed-ms",
        type=int,
        help="Elapsed time for an on-device release build, if measured.",
    )
    parser.add_argument(
        "--candidate-origin",
        choices=("on-device-release-build", "q005-qualified-aarch64-copy"),
        default="q005-qualified-aarch64-copy",
        help="How the release candidate was produced on or for the SBC.",
    )
    parser.add_argument("--timeout", type=float, default=COMMAND_TIMEOUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_qualification(
            binary=args.binary,
            config_fixture=args.config_fixture,
            timeout=args.timeout,
            expected_sha256=args.expected_sha256,
            candidate_origin=args.candidate_origin,
            build_elapsed_ms=args.build_elapsed_ms,
        )
    except (OSError, QualificationError, ValueError, sqlite3.Error) as error:
        report = {
            "schema_version": SCHEMA_VERSION,
            "plan": "Q008",
            "manifest": MANIFEST_VERSION,
            "status": "fail",
            "reason": bounded(str(error)),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": report.get("status"), "output": str(args.output)}))
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
