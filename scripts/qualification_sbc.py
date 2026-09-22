"""Run bounded SBC qualification on a physical Linux aarch64 SBC.

The runner uses a private temporary root and a loopback-only provider. It
records only bounded, scalar evidence; process output, request bodies,
credentials, hostnames, addresses, and full environment values are excluded.

Usage::

    uv run python scripts/qualification_sbc.py \
        --binary rust/target/release/eggpool \
        --output artifacts/qualification/008-run.json

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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/tooling/fixtures/qualification/sbc.toml"
BENCHMARK_FIXTURE = ROOT / "tests/tooling/fixtures/qualification/sbc-benchmark.toml"
DEFAULT_OUTPUT = ROOT / "artifacts/qualification/008-run.json"
SCHEMA_V1 = "runtime-q008.v1"
SCHEMA_V2 = "runtime-q008.v2"
SCHEMA_VERSION = SCHEMA_V1
MANIFEST_VERSION = "runtime-q001.v1"
MAX_REASON_BYTES = 768
MAX_HTTP_BODY_BYTES = 128 * 1024
COMMAND_TIMEOUT = 45.0
SAMPLE_SECONDS = 0.20
BENCHMARK_MAX_SAMPLES = 100
BENCHMARK_WARMUPS = 5
CONCURRENCY_BATCH_SIZE = 32
CONCURRENCY_WORKERS = 4
STABILIZATION_SECONDS = 3.0
DIAGNOSTIC_MIN_SAMPLES = 10
DIAGNOSTIC_MAX_SAMPLES = 200
PUBLICATION_STORAGE_MIN_SAMPLES = 20
PUBLICATION_STORAGE_MAX_SAMPLES = 200
PUBLICATION_PHASE_SAMPLES = 60
DIAGNOSTIC_WARMUPS = 5
DIRECT_CONTROL_WARMUPS = 5
DIRECT_CONTROL_SAMPLES = 30
# The phase corpus must remain exactly 60 successful requests even when the
# effective SQLite autocheckpoint produces a multi-second commit tail.
DIAGNOSTIC_TIMEOUT = 30.0
DIAGNOSTIC_SLOWEST_RETAINED = 5
DIAGNOSTIC_FIXTURE_PATH_SUFFIX = "/responses"
WAL_HEADER_BYTES = 32
DIAGNOSTIC_QUIESCENCE_TIMEOUT = 15.0
DIAGNOSTIC_TASK_NAMES = (
    "checkpoint",
    "metrics_flush",
    "catalog_refresh",
    "retention_cleanup",
    "automatic_backup",
)
MODELS = {
    "chat_completions": "q008-chat",
    "responses": "q008-responses",
    "messages": "q008-messages",
}


@dataclass(frozen=True)
class BenchmarkCase:
    """One typed benchmark case: client surface plus terminal expectations.

    The translated case keeps the Responses client surface while requiring
    proof that the loopback fixture observed the Anthropic Messages upstream
    path, so an accidental native Responses route cannot pass as translated.
    """

    key: str
    client_surface: str
    model: str
    streaming: bool
    terminal_marker: bytes | None
    expected_upstream_path: str | None


NATIVE_FINITE_CASE = BenchmarkCase(
    key="native_responses_finite",
    client_surface="responses",
    model=MODELS["responses"],
    streaming=False,
    terminal_marker=None,
    expected_upstream_path=None,
)
NATIVE_STREAMING_CASE = BenchmarkCase(
    key="native_responses_streaming",
    client_surface="responses",
    model=MODELS["responses"],
    streaming=True,
    terminal_marker=b"response.completed",
    expected_upstream_path=None,
)
TRANSLATED_STREAMING_CASE = BenchmarkCase(
    key="translated_responses_to_messages_streaming",
    client_surface="responses",
    model=MODELS["messages"],
    streaming=True,
    terminal_marker=b"response.completed",
    expected_upstream_path="/messages",
)


@dataclass(frozen=True)
class DiagnosticSample:
    """One bounded scalar-only finite-tail diagnostic observation.

    Only sequence numbers and millisecond phase durations are retained.
    Prompts, bodies, headers, credentials, paths, and URLs are never stored.
    """

    sequence: int
    pre_provider_ms: int | None
    provider_service_ms: int | None
    post_provider_ttft_ms: int | None
    client_body_ms: int | None
    total_ms: int
    timed_out: bool
    http_status: int | None
    last_phase: str | None
    wal_bytes_before: int | None = None
    wal_bytes_after: int | None = None
    wal_bytes_delta: int | None = None
    wal_checkpoint_sequence_before: int | None = None
    wal_checkpoint_sequence_after: int | None = None
    wal_checkpoint_sequence_changed: bool | None = None


class QualificationError(RuntimeError):
    """A mandatory SBC qualification observation failed."""


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


def _repository_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    value = result.stdout.strip()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else "unavailable"


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
        return None, "SBC qualification requires Linux on a physical aarch64 SBC"
    architecture = platform.machine().lower()
    if architecture not in {"aarch64", "arm64"}:
        return (
            None,
            "SBC qualification requires aarch64; hosted or translated execution "
            "is refused",
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
        diagnostic = self.owner.diagnostic_begin()
        try:
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(min(length, MAX_HTTP_BODY_BYTES))
            self.owner.record(self.path)
            streaming = b'"stream":true' in body.replace(b" ", b"")
            if self.path.endswith("/chat/completions"):
                response = (
                    b'data: {"id":"q008-stream","choices":'
                    b'[{"delta":{"content":"ok"}}]}\n\n'
                    b"data: [DONE]\n\n"
                    if streaming
                    else b'{"id":"q008-chat","object":"chat.completion",'
                    b'"model":"q008-chat",'
                    b'"choices":[{"index":0,"message":{"role":"assistant",'
                    b'"content":"ok"},'
                    b'"finish_reason":"stop"}],"usage":{"prompt_tokens":1,'
                    b'"completion_tokens":1,"total_tokens":2}}'
                )
                self._respond(
                    200,
                    response,
                    "text/event-stream" if streaming else "application/json",
                )
                return
            if self.path.endswith("/responses"):
                response = (
                    b"event: response.output_text.delta\n"
                    b'data: {"type":"response.output_text.delta",'
                    b'"delta":"ok"}\n\n'
                    b"event: response.completed\n"
                    b'data: {"type":"response.completed","response":'
                    b'{"id":"q008-stream",'
                    b'"status":"completed","usage":{"input_tokens":1,'
                    b'"output_tokens":1,'
                    b'"total_tokens":2}}}\n\n'
                    if streaming
                    else b'{"id":"q008-responses","object":"response",'
                    b'"status":"completed",'
                    b'"error":null,'
                    b'"model":"q008-responses","output":[{"type":"message",'
                    b'"id":"msg",'
                    b'"status":"completed","role":"assistant","content":'
                    b'[{"type":"output_text",'
                    b'"text":"ok","annotations":[]}]}],"usage":'
                    b'{"input_tokens":1,"output_tokens":1,"total_tokens":2}}'
                )
                self._respond(
                    200,
                    response,
                    "text/event-stream" if streaming else "application/json",
                )
                return
            if self.path.endswith("/messages"):
                response = (
                    b"event: message_start\n"
                    b'data: {"type":"message_start","message":'
                    b'{"id":"q008-stream"}}\n\n'
                    b"event: content_block_delta\n"
                    b'data: {"type":"content_block_delta","index":0,'
                    b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
                    b"event: message_delta\n"
                    b'data: {"type":"message_delta","delta":'
                    b'{"stop_reason":"end_turn"},'
                    b'"usage":{"input_tokens":1,"output_tokens":1}}\n\n'
                    b"event: message_stop\n"
                    b'data: {"type":"message_stop"}\n\n'
                    if streaming
                    else b'{"id":"q008-messages","type":"message",'
                    b'"role":"assistant",'
                    b'"model":"q008-messages","content":[{"type":"text",'
                    b'"text":"ok"}],'
                    b'"stop_reason":"end_turn","stop_sequence":null,'
                    b'"usage":{"input_tokens":1,"output_tokens":1}}'
                )
                self._respond(
                    200,
                    response,
                    "text/event-stream" if streaming else "application/json",
                )
                return
            self._respond(404, b"not found", "text/plain")
        finally:
            if diagnostic is not None:
                self.owner.diagnostic_end(
                    diagnostic[0], diagnostic[1], time.monotonic_ns()
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
    """Threaded loopback-only provider with fixed-bucket request counters."""

    def __init__(self) -> None:
        self.requests = 0
        self._lock = threading.Lock()
        self._path_counts = {
            "/chat/completions": 0,
            "/responses": 0,
            "/messages": 0,
        }
        self._diagnostic_enabled = False
        self._diagnostic_sequence = 0
        self._diagnostic_timings: list[tuple[int, int, int]] = []
        self._diagnostic_capacity = DIAGNOSTIC_MAX_SAMPLES
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
        _LoopbackHandler.owner = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def record(self, path: str) -> None:
        """Count one loopback request in a fixed bucket; no URLs retained."""
        with self._lock:
            self.requests += 1
            for bucket in self._path_counts:
                if path.endswith(bucket):
                    self._path_counts[bucket] += 1
                    break

    def path_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._path_counts)

    def upstream_count(self, suffix: str) -> int:
        with self._lock:
            return int(self._path_counts.get(suffix, 0))

    def start_diagnostic(self, capacity: int = DIAGNOSTIC_MAX_SAMPLES) -> None:
        """Enable bounded provider-boundary timing for one diagnostic batch.

        Only integer sequence numbers and monotonic-nanosecond timestamps are
        retained. Bodies, paths, headers, credentials, and URLs are never
        stored.
        """
        with self._lock:
            self._diagnostic_enabled = True
            self._diagnostic_sequence = 0
            self._diagnostic_timings = []
            self._diagnostic_capacity = max(1, min(capacity, DIAGNOSTIC_MAX_SAMPLES))

    def stop_diagnostic(self) -> None:
        with self._lock:
            self._diagnostic_enabled = False

    def diagnostic_begin(self) -> tuple[int, int] | None:
        """Assign a diagnostic sequence number and receive timestamp, if armed."""
        with self._lock:
            if not self._diagnostic_enabled:
                return None
            self._diagnostic_sequence += 1
            return (self._diagnostic_sequence, time.monotonic_ns())

    def diagnostic_end(self, sequence: int, received_ns: int, finished_ns: int) -> None:
        """Retain one bounded timing tuple of integers only."""
        with self._lock:
            if not self._diagnostic_enabled:
                return
            self._diagnostic_timings.append((sequence, received_ns, finished_ns))
            while len(self._diagnostic_timings) > self._diagnostic_capacity:
                self._diagnostic_timings.pop(0)

    def diagnostic_timings(self) -> list[tuple[int, int, int]]:
        with self._lock:
            return list(self._diagnostic_timings)

    def diagnostic_timing_count(self) -> int:
        with self._lock:
            return len(self._diagnostic_timings)

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


def _runtime_task_snapshot(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return only the fixed, database-relevant task scalars."""
    manager = value.get("runtime_manager")
    manager_map: dict[str, Any] = (
        cast("dict[str, Any]", manager) if isinstance(manager, dict) else {}
    )
    tasks = manager_map.get("tasks") or value.get("background_tasks")
    task_list: list[Any] = cast("list[Any]", tasks) if isinstance(tasks, list) else []
    allowed = set(DIAGNOSTIC_TASK_NAMES)
    snapshot: dict[str, dict[str, Any]] = {}
    for raw_task in task_list:
        if not isinstance(raw_task, dict):
            continue
        task = cast("dict[str, Any]", raw_task)
        name = task.get("name")
        if name in allowed:
            snapshot[str(name)] = {
                "tick_count": task.get("tick_count")
                if isinstance(task.get("tick_count"), int)
                else None,
                "in_tick": task.get("in_tick")
                if isinstance(task.get("in_tick"), bool)
                else None,
            }
    return snapshot


def _runtime_json(runtime_url: str, server_api_key: str) -> dict[str, Any] | None:
    try:
        status, body = _http(
            runtime_url, headers={"Authorization": f"Bearer {server_api_key}"}
        )
        if status != 200:
            return None
        value = json.loads(body.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _wait_for_diagnostic_quiescence(
    runtime_url: str,
    *,
    timeout: float = DIAGNOSTIC_QUIESCENCE_TIMEOUT,
    server_api_key: str = "q008-server-key",
) -> dict[str, dict[str, Any]]:
    """Wait for startup checkpoint/metrics work and a quiescent task window."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = _runtime_json(runtime_url, server_api_key)
        tasks = _runtime_task_snapshot(value or {})
        checkpoint = tasks.get("checkpoint")
        metrics_flush = tasks.get("metrics_flush")
        if (
            checkpoint is not None
            and metrics_flush is not None
            and isinstance(checkpoint.get("tick_count"), int)
            and checkpoint["tick_count"] >= 1
            and isinstance(metrics_flush.get("tick_count"), int)
            and metrics_flush["tick_count"] >= 1
            and all(task.get("in_tick") is False for task in tasks.values())
        ):
            return tasks
        time.sleep(0.1)
    raise QualificationError("database task quiescence was not reached")


def _task_tick_deltas(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in DIAGNOSTIC_TASK_NAMES:
        initial = before.get(name)
        final = after.get(name)
        if initial is None and final is None:
            continue
        initial_count = initial.get("tick_count") if initial else None
        final_count = final.get("tick_count") if final else None
        delta = (
            final_count - initial_count
            if isinstance(initial_count, int) and isinstance(final_count, int)
            else None
        )
        result[name] = {
            "baseline_tick_count": initial_count,
            "final_tick_count": final_count,
            "tick_count_delta": delta,
            "final_in_tick": final.get("in_tick") if final else None,
        }
    return result


def _wal_snapshot(database: Path) -> dict[str, Any]:
    """Read bounded WAL/file metadata without opening SQLite or checkpointing."""
    wal = database.with_name(database.name + "-wal")
    shm = database.with_name(database.name + "-shm")
    snapshot: dict[str, Any] = {
        "database_present": database.is_file(),
        "database_bytes": database.stat().st_size if database.is_file() else None,
        "wal_present": wal.is_file(),
        "wal_bytes": wal.stat().st_size if wal.is_file() else None,
        "shm_present": shm.is_file(),
        "shm_bytes": shm.stat().st_size if shm.is_file() else None,
        "wal_page_size": None,
        "wal_checkpoint_sequence": None,
    }
    if not wal.is_file():
        return snapshot
    try:
        with wal.open("rb") as stream:
            header = stream.read(WAL_HEADER_BYTES)
    except OSError:
        return snapshot
    if len(header) < 16 or header[:4] not in {b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"}:
        return snapshot
    page_size = int.from_bytes(header[8:12], "big")
    if page_size == 1:
        page_size = 65_536
    if page_size < 512 or page_size > 65_536 or page_size & (page_size - 1):
        return snapshot
    snapshot["wal_page_size"] = page_size
    snapshot["wal_checkpoint_sequence"] = int.from_bytes(header[12:16], "big")
    return snapshot


def resource_sample(
    label: str,
    process: subprocess.Popen[str],
    database: Path,
    runtime_url: str,
    *,
    duration: float = SAMPLE_SECONDS,
    server_api_key: str = "q008-server-key",
    include_peak: bool = False,
) -> dict[str, Any]:
    """Capture a bounded procfs/runtime/database snapshot.

    Ordinary Q008 qualification keeps the pre-benchmark shape without
    ``peak_rss_bytes``. Benchmark mode sets ``include_peak`` to capture
    ``VmHWM`` alongside the existing RSS snapshot.
    """
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
    if include_peak:
        sample["peak_rss_bytes"] = _proc_value(pid, "VmHWM")
    return sample


def _cadence_scalar(content: str, section: str, key: str) -> str | None:
    """Extract one bounded scalar from a rendered TOML section, if present."""
    section_match = re.search(
        rf"(?ms)^\[{re.escape(section)}\]\s*(.*?)(?=^\[|\Z)", content
    )
    if not section_match:
        return None
    match = re.search(rf"(?m)^{re.escape(key)}\s*=\s*(.+?)\s*$", section_match.group(1))
    if not match:
        return None
    return match.group(1).strip()[:64]


def benchmark_cadence_facts(content: str) -> dict[str, Any]:
    """Record background cadence/enablement scalars for benchmark reports."""
    return {
        "server_access_log": _cadence_scalar(content, "server", "access_log"),
        "server_threads": _cadence_scalar(content, "server", "threads"),
        "database_busy_timeout_ms": _cadence_scalar(
            content, "database", "busy_timeout_ms"
        ),
        "database_wal": _cadence_scalar(content, "database", "wal"),
        "database_synchronous": _cadence_scalar(content, "database", "synchronous"),
        "database_worker_threads": _cadence_scalar(
            content, "database", "worker_threads"
        ),
        "models_refresh_interval_s": _cadence_scalar(
            content, "models", "refresh_interval_s"
        ),
        "models_startup_refresh": _cadence_scalar(content, "models", "startup_refresh"),
        "model_info_enabled": _cadence_scalar(content, "model_info", "enabled"),
        "model_info_startup_refresh": _cadence_scalar(
            content, "model_info", "startup_refresh"
        ),
        "metrics_write_mode": _cadence_scalar(content, "metrics", "write_mode"),
        "metrics_flush_interval_s": _cadence_scalar(
            content, "metrics", "flush_interval_s"
        ),
        "metrics_aggregate_only": _cadence_scalar(content, "metrics", "aggregate_only"),
        "metrics_max_buffered_events": _cadence_scalar(
            content, "metrics", "max_buffered_events"
        ),
        "metrics_event_loop_lag_enabled": _cadence_scalar(
            content, "metrics", "event_loop_lag_enabled"
        ),
        "metrics_cleanup_interval_s": _cadence_scalar(
            content, "metrics", "cleanup_interval_s"
        ),
        "routing_trace_mode": _cadence_scalar(content, "routing.trace", "mode"),
        "routing_trace_sample_rate": _cadence_scalar(
            content, "routing.trace", "sample_rate"
        ),
        "backup_enabled": _cadence_scalar(content, "backup", "enabled"),
        "backup_interval_s": _cadence_scalar(content, "backup", "interval_s"),
    }


def _percentile(values: Sequence[int], percentile: int) -> int | None:
    """Return a nearest-rank percentile without retaining benchmark samples."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, (len(ordered) * percentile + 99) // 100)
    return ordered[min(rank - 1, len(ordered) - 1)]


def _timing_summary(
    elapsed_ms: Sequence[int], ttft_ms: Sequence[int]
) -> dict[str, Any]:
    """Return aggregate timing scalars and discard per-request observations."""
    summary: dict[str, Any] = {
        "sample_count": len(elapsed_ms),
        "p50_elapsed_ms": _percentile(elapsed_ms, 50),
        "p95_elapsed_ms": _percentile(elapsed_ms, 95),
        "minimum_elapsed_ms": min(elapsed_ms) if elapsed_ms else None,
        "maximum_elapsed_ms": max(elapsed_ms) if elapsed_ms else None,
    }
    if ttft_ms:
        summary.update(
            {
                "p50_ttft_ms": _percentile(ttft_ms, 50),
                "p95_ttft_ms": _percentile(ttft_ms, 95),
            }
        )
    return summary


def _cpu_batch_summary(
    first: tuple[int, int] | None,
    second: tuple[int, int] | None,
    elapsed_seconds: float,
) -> dict[str, Any]:
    """Summarize process CPU ticks observed over one request batch."""
    if not first or not second or first[1] != second[1]:
        return {
            "cpu_ticks": None,
            "cpu_time_ms": None,
            "cpu_percent": None,
        }
    ticks = max(second[0] - first[0], 0)
    clock_hz = first[1]
    return {
        "cpu_ticks": ticks,
        "cpu_time_ms": round(ticks / clock_hz * 1000),
        "cpu_percent": round(ticks / clock_hz / max(elapsed_seconds, 0.001) * 100, 2),
    }


def _diagnose_sample_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "diagnostic samples must be an integer"
        ) from error
    if not DIAGNOSTIC_MIN_SAMPLES <= count <= DIAGNOSTIC_MAX_SAMPLES:
        raise argparse.ArgumentTypeError(
            "diagnostic samples must be between "
            f"{DIAGNOSTIC_MIN_SAMPLES} and {DIAGNOSTIC_MAX_SAMPLES}"
        )
    return count


def _publication_storage_sample_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "publication-storage diagnostic samples must be an integer"
        ) from error
    if not PUBLICATION_STORAGE_MIN_SAMPLES <= count <= PUBLICATION_STORAGE_MAX_SAMPLES:
        raise argparse.ArgumentTypeError(
            "publication-storage diagnostic samples must be between "
            f"{PUBLICATION_STORAGE_MIN_SAMPLES} and {PUBLICATION_STORAGE_MAX_SAMPLES}"
        )
    return count


def _qualification_wal_autocheckpoint_pages(value: str) -> int:
    try:
        pages = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "qualification wal_autocheckpoint must be an integer in 0..100000"
        ) from error
    if not 0 <= pages <= 100_000:
        raise argparse.ArgumentTypeError(
            "qualification wal_autocheckpoint must be an integer in 0..100000"
        )
    return pages


def _ns_to_ms(earlier_ns: int | None, later_ns: int | None) -> int | None:
    if earlier_ns is None or later_ns is None:
        return None
    return max(0, round((later_ns - earlier_ns) / 1_000_000))


def _phase_values(samples: Sequence[DiagnosticSample], field: str) -> list[int]:
    values: list[int] = []
    for sample in samples:
        value = getattr(sample, field)
        if isinstance(value, int):
            values.append(value)
    return values


def _diagnostic_phase_summary(
    samples: Sequence[DiagnosticSample], *, include_wal: bool = False
) -> dict[str, Any]:
    """Aggregate bounded scalar phase evidence; discard per-request detail.

    Retains sample counts, p50/p95/max per phase, and the five slowest
    scalar-only records. Never emits p99 and never retains bodies, headers,
    paths, credentials, or URLs.
    """
    ordered = sorted(samples, key=lambda item: item.total_ms, reverse=True)
    slowest: list[dict[str, Any]] = []
    for item in ordered[:DIAGNOSTIC_SLOWEST_RETAINED]:
        slowest.append(
            {
                "sequence": item.sequence,
                "pre_provider_ms": item.pre_provider_ms,
                "provider_service_ms": item.provider_service_ms,
                "post_provider_ttft_ms": item.post_provider_ttft_ms,
                "client_body_ms": item.client_body_ms,
                "total_ms": item.total_ms,
                "timed_out": item.timed_out,
                "http_status": item.http_status,
                "last_phase": item.last_phase,
            }
        )
        if include_wal:
            slowest[-1].update(
                {
                    "wal_bytes_before": item.wal_bytes_before,
                    "wal_bytes_after": item.wal_bytes_after,
                    "wal_bytes_delta": item.wal_bytes_delta,
                    "wal_checkpoint_sequence_before": (
                        item.wal_checkpoint_sequence_before
                    ),
                    "wal_checkpoint_sequence_after": item.wal_checkpoint_sequence_after,
                    "wal_checkpoint_sequence_changed": (
                        item.wal_checkpoint_sequence_changed
                    ),
                }
            )
    total_values = [item.total_ms for item in samples]
    pre_values = _phase_values(samples, "pre_provider_ms")
    service_values = _phase_values(samples, "provider_service_ms")
    post_values = _phase_values(samples, "post_provider_ttft_ms")
    body_values = _phase_values(samples, "client_body_ms")
    timeout_count = sum(1 for item in samples if item.timed_out)
    completed_count = sum(
        1 for item in samples if not item.timed_out and item.http_status == 200
    )
    result: dict[str, Any] = {
        "sample_count": len(samples),
        "completed_count": completed_count,
        "timeout_count": timeout_count,
        "failed_count": len(samples) - completed_count - timeout_count,
        "p50_total_ms": _percentile(total_values, 50),
        "p95_total_ms": _percentile(total_values, 95),
        "maximum_total_ms": max(total_values) if total_values else None,
        "p50_pre_provider_ms": _percentile(pre_values, 50),
        "p95_pre_provider_ms": _percentile(pre_values, 95),
        "maximum_pre_provider_ms": max(pre_values) if pre_values else None,
        "p50_provider_service_ms": _percentile(service_values, 50),
        "p95_provider_service_ms": _percentile(service_values, 95),
        "maximum_provider_service_ms": (
            max(service_values) if service_values else None
        ),
        "p50_post_provider_ttft_ms": _percentile(post_values, 50),
        "p95_post_provider_ttft_ms": _percentile(post_values, 95),
        "maximum_post_provider_ttft_ms": max(post_values) if post_values else None,
        "p50_client_body_ms": _percentile(body_values, 50),
        "p95_client_body_ms": _percentile(body_values, 95),
        "maximum_client_body_ms": max(body_values) if body_values else None,
        "slowest_five": slowest,
    }
    if include_wal:
        checkpoint_changed = [
            item.total_ms
            for item in samples
            if item.wal_checkpoint_sequence_changed is True
        ]
        checkpoint_unchanged = [
            item.total_ms
            for item in samples
            if item.wal_checkpoint_sequence_changed is False
        ]
        result.update(
            {
                "wal_checkpoint_sequence_change_count": len(checkpoint_changed),
                "wal_checkpoint_sequence_unchanged_count": len(checkpoint_unchanged),
                "maximum_total_ms_with_checkpoint_sequence_change": max(
                    checkpoint_changed, default=None
                ),
                "maximum_total_ms_without_checkpoint_sequence_change": max(
                    checkpoint_unchanged, default=None
                ),
            }
        )
    return result


def _qualification_database_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and project the feature-only runtime database snapshot."""
    snapshot_value = value.get("database_qualification")
    snapshot = (
        cast("dict[str, Any]", snapshot_value)
        if isinstance(snapshot_value, dict)
        else None
    )
    if not isinstance(snapshot, dict):
        raise QualificationError(
            "Plan 239 requires a qualification-db-diagnostics candidate build"
        )
    effective_value = snapshot.get("effective")
    effective = (
        cast("dict[str, Any]", effective_value)
        if isinstance(effective_value, dict)
        else None
    )
    if not isinstance(effective, dict):
        raise QualificationError("qualification database pragma snapshot is missing")
    required_effective: dict[str, Any] = {
        "journal_mode": effective.get("journal_mode"),
        "synchronous": effective.get("synchronous"),
        "page_size": effective.get("page_size"),
        "wal_autocheckpoint_pages": effective.get("wal_autocheckpoint_pages"),
    }
    if not isinstance(required_effective["journal_mode"], str) or not isinstance(
        required_effective["synchronous"], str
    ):
        raise QualificationError("qualification pragma names are not scalar")
    page_size = required_effective["page_size"]
    autocheckpoint_pages = required_effective["wal_autocheckpoint_pages"]
    if not (
        isinstance(page_size, int)
        and page_size >= 0
        and isinstance(autocheckpoint_pages, int)
        and autocheckpoint_pages >= 0
    ):
        raise QualificationError("qualification pragma values are not scalar")
    latest = snapshot.get("latest_record_seq")
    capacity = snapshot.get("collector_capacity")
    if (
        not isinstance(latest, int)
        or latest < 0
        or not isinstance(capacity, int)
        or not 1 <= capacity <= 256
    ):
        raise QualificationError("qualification collector metadata is invalid")
    return {
        "schema_version": snapshot.get("schema_version"),
        "collector_capacity": capacity,
        "effective": required_effective,
        "latest_record_seq": latest,
    }


def _qualification_records_after(
    value: Mapping[str, Any], baseline_sequence: int
) -> list[dict[str, Any]]:
    """Return only bounded, fixed-shape records newer than the baseline."""
    snapshot_value = value.get("database_qualification")
    snapshot = (
        cast("dict[str, Any]", snapshot_value)
        if isinstance(snapshot_value, dict)
        else None
    )
    if snapshot is None or not isinstance(snapshot.get("records"), list):
        raise QualificationError("qualification database records are missing")
    records: list[dict[str, Any]] = []
    previous_sequence = baseline_sequence
    raw_records = cast("list[Any]", snapshot["records"])
    for raw_value in raw_records:
        if not isinstance(raw_value, dict):
            raise QualificationError("qualification database record is not an object")
        raw = cast("dict[str, Any]", raw_value)
        record = {
            "record_seq": raw.get("record_seq"),
            "kind": raw.get("kind"),
            "gate_wait_us": raw.get("gate_wait_us"),
            "worker_queue_us": raw.get("worker_queue_us"),
            "begin_us": raw.get("begin_us"),
            "body_us": raw.get("body_us"),
            "commit_us": raw.get("commit_us"),
            "worker_return_us": raw.get("worker_return_us"),
            "total_us": raw.get("total_us"),
            "success": raw.get("success"),
        }
        sequence = record["record_seq"]
        if not isinstance(sequence, int) or sequence <= baseline_sequence:
            continue
        if sequence <= previous_sequence or record["kind"] not in {
            "publication",
            "finalization",
            "other",
        }:
            raise QualificationError("qualification database records are not ordered")
        phase_values = [
            record[name]
            for name in (
                "gate_wait_us",
                "worker_queue_us",
                "begin_us",
                "body_us",
                "worker_return_us",
                "total_us",
            )
        ]
        if not all(
            isinstance(phase_value, int) and phase_value >= 0
            for phase_value in phase_values
        ):
            raise QualificationError("qualification database phase is not scalar")
        if record["commit_us"] is not None and (
            not isinstance(record["commit_us"], int) or record["commit_us"] < 0
        ):
            raise QualificationError("qualification database commit phase is invalid")
        if not isinstance(record["success"], bool):
            raise QualificationError("qualification database success flag is invalid")
        records.append(record)
        previous_sequence = sequence
    return records


def _transaction_phase_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"sample_count": len(records)}
    for field in (
        "gate_wait_us",
        "worker_queue_us",
        "begin_us",
        "body_us",
        "commit_us",
        "worker_return_us",
        "total_us",
    ):
        values = [value[field] for value in records if isinstance(value[field], int)]
        result[f"p50_{field}"] = _percentile(values, 50)
        result[f"p95_{field}"] = _percentile(values, 95)
        result[f"maximum_{field}"] = max(values) if values else None
    return result


def _correlate_transaction_phases(
    run: Mapping[str, Any], records: Sequence[Mapping[str, Any]], sample_count: int
) -> dict[str, Any]:
    foreground: list[dict[str, Any]] = [
        dict(record)
        for record in records
        if record["kind"] in {"publication", "finalization"}
    ]
    if len(foreground) != sample_count * 2:
        raise QualificationError(
            "Plan 239 expected exactly one publication and finalization "
            "record per request"
        )
    if any(not record["success"] for record in foreground):
        raise QualificationError("Plan 239 foreground transaction did not succeed")
    publication: list[dict[str, Any]] = [
        record for record in foreground if record["kind"] == "publication"
    ]
    finalization: list[dict[str, Any]] = [
        record for record in foreground if record["kind"] == "finalization"
    ]
    if len(publication) != sample_count or len(finalization) != sample_count:
        raise QualificationError(
            "Plan 239 foreground records are missing or duplicated"
        )

    slowest: list[dict[str, Any]] = []
    slowest_value = run.get("slowest_five", [])
    if not isinstance(slowest_value, list):
        raise QualificationError("Plan 239 slowest request evidence is invalid")
    for item_value in cast("list[Any]", slowest_value):
        if not isinstance(item_value, dict):
            raise QualificationError("Plan 239 slowest request evidence is invalid")
        item = cast("dict[str, Any]", item_value)
        if not isinstance(item.get("sequence"), int):
            raise QualificationError("Plan 239 slowest request evidence is invalid")
        request_sequence = item["sequence"]
        if not 1 <= request_sequence <= sample_count:
            raise QualificationError("Plan 239 request sequence is out of bounds")
        publication_record = cast("dict[str, Any]", publication[request_sequence - 1])
        finalization_record = cast("dict[str, Any]", finalization[request_sequence - 1])
        pre_provider = item.get("pre_provider_ms")
        post_provider = item.get("post_provider_ttft_ms")
        slowest.append(
            {
                "request_sequence": request_sequence,
                "pre_provider_ms": pre_provider,
                "provider_service_ms": item.get("provider_service_ms"),
                "post_provider_ttft_ms": post_provider,
                "total_ms": item.get("total_ms"),
                "wal_checkpoint_sequence_changed": item.get(
                    "wal_checkpoint_sequence_changed"
                ),
                "publication": dict(publication_record),
                "finalization": dict(finalization_record),
                "pre_provider_unattributed_us": (
                    max(pre_provider * 1000 - publication_record["total_us"], 0)
                    if isinstance(pre_provider, int)
                    else None
                ),
                "post_provider_unattributed_us": (
                    max(post_provider * 1000 - finalization_record["total_us"], 0)
                    if isinstance(post_provider, int)
                    else None
                ),
            }
        )
    publication_records = publication
    finalization_records = finalization
    return {
        "foreground_record_count": len(foreground),
        "publication_phase_summary": _transaction_phase_summary(publication_records),
        "finalization_phase_summary": _transaction_phase_summary(finalization_records),
        "slowest_five_correlated": slowest,
    }


def _diagnostic_timed_post(
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[int | None, bytes, int, int | None, int, bool, str | None]:
    """POST with monotonic T0/T3/T4 capture using the shared urllib client.

    Returns ``(status, body, t0_ns, t3_ns, t4_ns, timed_out, last_phase)``.
    Timeouts preserve whatever phase timestamps exist; they are never
    silently discarded.
    """
    payload = urllib.request.Request(
        url, data=body, headers=dict(headers), method="POST"
    )
    started_ns = time.monotonic_ns()
    try:
        with urllib.request.urlopen(payload, timeout=timeout) as response:
            first = response.read(1)
            first_ns: int | None = time.monotonic_ns() if first else None
            rest = response.read(MAX_HTTP_BODY_BYTES - len(first))
            finished_ns = time.monotonic_ns()
            last_phase: str | None = "completed" if first else "provider_finished"
            return (
                response.status,
                first + rest,
                started_ns,
                first_ns,
                finished_ns,
                False,
                last_phase,
            )
    except urllib.error.HTTPError as error:
        finished_ns = time.monotonic_ns()
        try:
            error_body = error.read(MAX_HTTP_BODY_BYTES)
        except OSError:
            error_body = b""
        return (
            error.code,
            error_body,
            started_ns,
            None,
            finished_ns,
            False,
            "http_error",
        )
    except (OSError, TimeoutError) as error:
        finished_ns = time.monotonic_ns()
        timed_out = isinstance(error, TimeoutError) or "timed out" in str(error).lower()
        return (
            None,
            b"",
            started_ns,
            None,
            finished_ns,
            timed_out,
            "timeout" if timed_out else "client_start",
        )


def _finite_diagnostic_payload() -> bytes:
    return json.dumps(
        {"model": MODELS["responses"], "input": "ping", "store": False}
    ).encode()


def _diagnostic_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer q008-server-key",
        "Content-Type": "application/json",
    }


def _combine_diagnostic_sample(
    sequence: int,
    t0_ns: int,
    provider_timing: tuple[int, int, int] | None,
    t3_ns: int | None,
    t4_ns: int,
    status: int | None,
    timed_out: bool,
    last_phase: str | None,
    *,
    wal_before: Mapping[str, Any] | None = None,
    wal_after: Mapping[str, Any] | None = None,
) -> DiagnosticSample:
    t1_ns = provider_timing[1] if provider_timing is not None else None
    t2_ns = provider_timing[2] if provider_timing is not None else None
    observed_phase = last_phase
    if provider_timing is None and not timed_out and observed_phase == "completed":
        observed_phase = "completed"
    elif provider_timing is None and timed_out:
        observed_phase = "timeout"
    return DiagnosticSample(
        sequence=sequence,
        pre_provider_ms=_ns_to_ms(t0_ns, t1_ns),
        provider_service_ms=_ns_to_ms(t1_ns, t2_ns),
        post_provider_ttft_ms=_ns_to_ms(t2_ns, t3_ns),
        client_body_ms=_ns_to_ms(t3_ns, t4_ns),
        total_ms=max(0, round((t4_ns - t0_ns) / 1_000_000)),
        timed_out=timed_out,
        http_status=status,
        last_phase=observed_phase,
        wal_bytes_before=(
            wal_before.get("wal_bytes") if wal_before is not None else None
        ),
        wal_bytes_after=(wal_after.get("wal_bytes") if wal_after is not None else None),
        wal_bytes_delta=(
            wal_after.get("wal_bytes", 0) - wal_before.get("wal_bytes", 0)
            if wal_before is not None
            and wal_after is not None
            and isinstance(wal_before.get("wal_bytes"), int)
            and isinstance(wal_after.get("wal_bytes"), int)
            else None
        ),
        wal_checkpoint_sequence_before=(
            wal_before.get("wal_checkpoint_sequence")
            if wal_before is not None
            else None
        ),
        wal_checkpoint_sequence_after=(
            wal_after.get("wal_checkpoint_sequence") if wal_after is not None else None
        ),
        wal_checkpoint_sequence_changed=(
            wal_before.get("wal_checkpoint_sequence")
            != wal_after.get("wal_checkpoint_sequence")
            if wal_before is not None
            and wal_after is not None
            and isinstance(wal_before.get("wal_checkpoint_sequence"), int)
            and isinstance(wal_after.get("wal_checkpoint_sequence"), int)
            else None
        ),
    )


def _drain_new_provider_timings(
    provider: LoopbackProvider, before: int
) -> list[tuple[int, int, int]]:
    return provider.diagnostic_timings()[before:]


def _direct_provider_control(
    provider: LoopbackProvider,
    *,
    timeout: float = DIAGNOSTIC_TIMEOUT,
) -> dict[str, Any]:
    """Issue a matched finite corpus directly to the fixture provider.

    Uses the same Python HTTP client and the same loopback provider as the
    EggPool path, with fixed fixture paths only. Five warm-ups are
    unrecorded; 30 requests are measured. This is a fixture/host control,
    never a benchmark score comparison.
    """
    target = provider.base_url + DIAGNOSTIC_FIXTURE_PATH_SUFFIX
    payload = _finite_diagnostic_payload()
    headers = _diagnostic_headers()
    for _ in range(DIRECT_CONTROL_WARMUPS):
        with contextlib.suppress(OSError):
            _diagnostic_timed_post(target, payload, headers, timeout)
    provider.start_diagnostic(capacity=DIRECT_CONTROL_SAMPLES)
    samples: list[DiagnosticSample] = []
    try:
        for sequence in range(1, DIRECT_CONTROL_SAMPLES + 1):
            before = provider.diagnostic_timing_count()
            status, _, t0_ns, t3_ns, t4_ns, timed_out, last_phase = (
                _diagnostic_timed_post(target, payload, headers, timeout)
            )
            new_timings = _drain_new_provider_timings(provider, before)
            provider_timing = new_timings[-1] if new_timings else None
            samples.append(
                _combine_diagnostic_sample(
                    sequence,
                    t0_ns,
                    provider_timing,
                    t3_ns,
                    t4_ns,
                    status,
                    timed_out,
                    last_phase,
                )
            )
    finally:
        provider.stop_diagnostic()
    summary = _diagnostic_phase_summary(samples)
    return {
        "status": "measured",
        "request_target": "fixture-provider-direct",
        "fixture_path_suffix": DIAGNOSTIC_FIXTURE_PATH_SUFFIX,
        "warmup_count": DIRECT_CONTROL_WARMUPS,
        "interpretation": (
            "fixture/host control only; do not compare absolute latency "
            "to EggPool as a benchmark score"
        ),
        **summary,
    }


def _diagnostic_finite_warmup(port: int) -> None:
    for _ in range(DIAGNOSTIC_WARMUPS):
        status, body, _, _ = _benchmark_request(
            port, MODELS["responses"], False, "responses"
        )
        if status != 200 or not body:
            raise QualificationError(
                f"diagnostic warm-up returned HTTP {status} without a body"
            )


def _diagnostic_finite_batch(
    port: int,
    provider: LoopbackProvider,
    samples: int,
    process: subprocess.Popen[str] | None = None,
    database: Path | None = None,
    *,
    timeout: float = DIAGNOSTIC_TIMEOUT,
    warmup: bool = True,
) -> dict[str, Any]:
    """Run a sequential native-finite diagnostic batch with phase attribution.

    Five warm-ups are unrecorded. The measured batch contains only sequential
    native Responses finite requests so provider-boundary correlation does
    not require propagating an ID through production headers.
    """
    if not DIAGNOSTIC_MIN_SAMPLES <= samples <= DIAGNOSTIC_MAX_SAMPLES:
        raise ValueError(
            "diagnostic sample count must be "
            f"{DIAGNOSTIC_MIN_SAMPLES}..{DIAGNOSTIC_MAX_SAMPLES}"
        )
    url = f"http://127.0.0.1:{port}/v1/responses"
    payload = _finite_diagnostic_payload()
    headers = _diagnostic_headers()
    if warmup:
        _diagnostic_finite_warmup(port)
    provider.start_diagnostic(capacity=samples)
    observations: list[DiagnosticSample] = []
    wal_before = _wal_snapshot(database) if database is not None else None
    first_cpu = _proc_cpu(process.pid) if process is not None else None
    started = time.monotonic()
    try:
        for sequence in range(1, samples + 1):
            before = provider.diagnostic_timing_count()
            status, _, t0_ns, t3_ns, t4_ns, timed_out, last_phase = (
                _diagnostic_timed_post(url, payload, headers, timeout)
            )
            new_timings = _drain_new_provider_timings(provider, before)
            provider_timing = new_timings[-1] if new_timings else None
            wal_after = _wal_snapshot(database) if database is not None else None
            observations.append(
                _combine_diagnostic_sample(
                    sequence,
                    t0_ns,
                    provider_timing,
                    t3_ns,
                    t4_ns,
                    status,
                    timed_out,
                    last_phase,
                    wal_before=wal_before,
                    wal_after=wal_after,
                )
            )
            wal_before = wal_after
    finally:
        provider.stop_diagnostic()
    elapsed_seconds = max(time.monotonic() - started, 0.001)
    second_cpu = _proc_cpu(process.pid) if process is not None else None
    summary = _diagnostic_phase_summary(observations, include_wal=database is not None)
    return {
        "status": "measured",
        "model": MODELS["responses"],
        "client_surface": "responses",
        "streaming": False,
        "warmup_count": DIAGNOSTIC_WARMUPS,
        "sequential": True,
        "cpu": _cpu_batch_summary(first_cpu, second_cpu, elapsed_seconds),
        "batch_elapsed_ms": round(elapsed_seconds * 1000),
        "interpretation": (
            "bounded provider-boundary phase attribution; T0 client start, "
            "T1 provider receive, T2 provider finish, T3 first byte, T4 done"
        ),
        **summary,
    }


def _benchmark_request(
    port: int,
    model: str,
    streaming: bool,
    client_surface: str = "responses",
) -> tuple[int, bytes, int, int | None]:
    return _request_timed(port, client_surface, model, streaming)


def _sequential_benchmark(
    process: subprocess.Popen[str],
    port: int,
    case: BenchmarkCase,
    samples: int,
    provider: LoopbackProvider | None = None,
    *,
    # Backward-compatible keyword spellings retained for tooling callers.
    model: str | None = None,
    streaming: bool | None = None,
    terminal_marker: bytes | None = None,
) -> tuple[dict[str, Any], bool]:
    """Run warm-ups and a sequential batch, retaining only aggregate scalars.

    The typed ``case`` carries the client surface, model, stream flag, expected
    client-side terminal marker, and expected fixture upstream path. Legacy
    ``model``/``streaming``/``terminal_marker`` keywords override the case when
    supplied so older ad-hoc invocations keep working.
    """
    active_model = model if model is not None else case.model
    active_streaming = streaming if streaming is not None else case.streaming
    legacy_override = (
        model is not None or streaming is not None or terminal_marker is not None
    )
    active_marker = terminal_marker if legacy_override else case.terminal_marker
    expected_upstream = None if legacy_override else case.expected_upstream_path
    upstream_before = (
        provider.upstream_count(expected_upstream)
        if provider is not None and expected_upstream is not None
        else None
    )
    for _ in range(BENCHMARK_WARMUPS):
        status, body, _, _ = _benchmark_request(
            port, active_model, active_streaming, case.client_surface
        )
        if (
            status != 200
            or not body
            or (active_marker is not None and active_marker not in body)
        ):
            return (
                {
                    "status": "not-measured",
                    "reason": (
                        "fixture route was not accepted during warm-up"
                        if active_marker is None
                        else "fixture warm-up lacked expected client terminal evidence"
                    ),
                    "warmup_http_status": status,
                    "model": active_model,
                    "streaming": active_streaming,
                },
                False,
            )
    first_cpu = _proc_cpu(process.pid)
    started = time.monotonic()
    elapsed_ms: list[int] = []
    ttft_ms: list[int] = []
    for _ in range(samples):
        status, body, elapsed, ttft = _benchmark_request(
            port, active_model, active_streaming, case.client_surface
        )
        elapsed_ms.append(elapsed)
        if ttft is not None:
            ttft_ms.append(ttft)
        if (
            status != 200
            or not body
            or (active_marker is not None and active_marker not in body)
        ):
            raise QualificationError(
                f"benchmark request returned HTTP {status} or lacked terminal evidence"
            )
    second_cpu = _proc_cpu(process.pid)
    elapsed_seconds = max(time.monotonic() - started, 0.001)
    result: dict[str, Any] = {
        "status": "measured",
        "model": active_model,
        "streaming": active_streaming,
        "warmup_count": BENCHMARK_WARMUPS,
        "cpu": _cpu_batch_summary(first_cpu, second_cpu, elapsed_seconds),
        **_timing_summary(elapsed_ms, ttft_ms),
    }
    if expected_upstream is not None:
        if provider is None or upstream_before is None:
            raise QualificationError(
                "translated benchmark requires a loopback upstream proof"
            )
        upstream_after = provider.upstream_count(expected_upstream)
        upstream_delta = upstream_after - upstream_before
        expected_minimum = BENCHMARK_WARMUPS + samples
        result["expected_upstream_path"] = expected_upstream
        result["upstream_request_delta"] = upstream_delta
        if upstream_delta < expected_minimum:
            return (
                {
                    "status": "not-measured",
                    "reason": (
                        "translated samples did not reach the fixture "
                        f"{expected_upstream} path"
                    ),
                    "model": active_model,
                    "streaming": active_streaming,
                    "expected_upstream_path": expected_upstream,
                    "upstream_request_delta": upstream_delta,
                },
                False,
            )
        result["upstream_proof"] = "fixture Messages path observed"
    return (result, True)


def _concurrent_finite_benchmark(
    process: subprocess.Popen[str], port: int
) -> dict[str, Any]:
    """Run the fixed contention observation at client concurrency four."""
    first_cpu = _proc_cpu(process.pid)
    started = time.monotonic()

    def request_once(_item: int) -> tuple[int, bytes, int, int | None]:
        return _benchmark_request(port, MODELS["responses"], False)

    with ThreadPoolExecutor(max_workers=CONCURRENCY_WORKERS) as executor:
        results = list(executor.map(request_once, range(CONCURRENCY_BATCH_SIZE)))
    elapsed_seconds = max(time.monotonic() - started, 0.001)
    second_cpu = _proc_cpu(process.pid)
    elapsed_ms = [result[2] for result in results]
    ttft_ms = [result[3] for result in results if result[3] is not None]
    completed = sum(1 for status, body, _, _ in results if status == 200 and body)
    return {
        "status": "measured" if completed == CONCURRENCY_BATCH_SIZE else "fail",
        "request_count": CONCURRENCY_BATCH_SIZE,
        "client_concurrency": CONCURRENCY_WORKERS,
        "completed_count": completed,
        "failed_count": CONCURRENCY_BATCH_SIZE - completed,
        "batch_elapsed_ms": round(elapsed_seconds * 1000),
        "requests_per_second": round(CONCURRENCY_BATCH_SIZE / elapsed_seconds, 3),
        "cpu": _cpu_batch_summary(first_cpu, second_cpu, elapsed_seconds),
        "timings": _timing_summary(elapsed_ms, ttft_ms),
    }


def _benchmark_sample_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "benchmark samples must be an integer"
        ) from error
    if not 1 <= count <= BENCHMARK_MAX_SAMPLES:
        raise argparse.ArgumentTypeError(
            f"benchmark samples must be between 1 and {BENCHMARK_MAX_SAMPLES}"
        )
    return count


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
        "__SBC_PORT__": str(port),
        "__SBC_UPSTREAM__": upstream,
        "__SBC_DATABASE__": str(database),
        "__SBC_BACKUP_DIR__": str(backup_dir),
    }
    for marker, replacement in replacements.items():
        content = content.replace(marker, replacement)
    if "__SBC_" in content:
        raise QualificationError(
            "SBC qualification config fixture has unresolved placeholders"
        )
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
    benchmark_samples: int = 0,
    diagnose_finite_tail: int | None = None,
    diagnose_publication_storage: int | None = None,
    diagnose_publication_phases: bool = False,
    qualification_wal_autocheckpoint_pages: int | None = None,
    diagnostic_database_dir: Path | None = None,
) -> dict[str, Any]:
    """Run SBC qualification and return a bounded machine-readable report."""
    if benchmark_samples < 0 or benchmark_samples > BENCHMARK_MAX_SAMPLES:
        raise ValueError(
            f"benchmark sample count must be 0 or 1..{BENCHMARK_MAX_SAMPLES}"
        )
    if diagnose_finite_tail is not None and not (
        DIAGNOSTIC_MIN_SAMPLES <= diagnose_finite_tail <= DIAGNOSTIC_MAX_SAMPLES
    ):
        raise ValueError(
            "diagnostic sample count must be "
            f"{DIAGNOSTIC_MIN_SAMPLES}..{DIAGNOSTIC_MAX_SAMPLES}"
        )
    if diagnose_finite_tail is not None and benchmark_samples <= 0:
        raise ValueError("diagnostic finite-tail mode requires benchmark mode")
    if diagnose_publication_storage is not None and not (
        PUBLICATION_STORAGE_MIN_SAMPLES
        <= diagnose_publication_storage
        <= PUBLICATION_STORAGE_MAX_SAMPLES
    ):
        raise ValueError(
            "publication-storage diagnostic sample count must be "
            f"{PUBLICATION_STORAGE_MIN_SAMPLES}..{PUBLICATION_STORAGE_MAX_SAMPLES}"
        )
    if diagnose_publication_storage is not None and diagnose_finite_tail is not None:
        raise ValueError(
            "publication-storage and finite-tail diagnostics are exclusive"
        )
    if diagnose_publication_phases and (
        diagnose_finite_tail is not None or diagnose_publication_storage is not None
    ):
        raise ValueError(
            "Plan 239 phase diagnostics are exclusive with other diagnostics"
        )
    if diagnose_publication_phases and benchmark_samples > 0:
        raise ValueError("Plan 239 phase diagnostics do not run the benchmark corpus")
    if (
        diagnose_publication_phases
        and config_fixture.resolve() != BENCHMARK_FIXTURE.resolve()
    ):
        raise ValueError("Plan 239 phase diagnostics require the benchmark fixture")
    if (
        qualification_wal_autocheckpoint_pages is not None
        and not diagnose_publication_phases
    ):
        raise ValueError(
            "qualification wal_autocheckpoint requires Plan 239 phase diagnostics"
        )
    if diagnose_publication_storage is not None and benchmark_samples > 0:
        raise ValueError(
            "publication-storage diagnostic mode does not run the benchmark corpus"
        )
    if diagnose_publication_storage is not None:
        if config_fixture.resolve() != BENCHMARK_FIXTURE.resolve():
            raise ValueError(
                "publication-storage diagnostic mode requires the benchmark fixture"
            )
    elif diagnostic_database_dir is not None and not diagnose_publication_phases:
        raise ValueError(
            "diagnostic database directory requires publication-phase or "
            "publication-storage mode"
        )
    benchmark_mode = (
        benchmark_samples > 0
        or diagnose_publication_storage is not None
        or diagnose_publication_phases
    )
    report: dict[str, Any] = {
        "schema_version": SCHEMA_V2 if benchmark_mode else SCHEMA_V1,
        "plan": "SBC qualification",
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
    if benchmark_mode:
        report["candidate"]["repository_commit_sha"] = _repository_commit()
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
        if qualification_wal_autocheckpoint_pages is not None:
            env["EGGPOOL_QUALIFICATION_WAL_AUTOCHECKPOINT_PAGES"] = str(
                qualification_wal_autocheckpoint_pages
            )
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
        with contextlib.ExitStack() as resources, LoopbackProvider() as provider:
            if (
                diagnose_publication_storage is not None or diagnose_publication_phases
            ) and diagnostic_database_dir is not None:
                try:
                    isolated_database_root = Path(
                        resources.enter_context(
                            tempfile.TemporaryDirectory(
                                dir=str(diagnostic_database_dir),
                                prefix="eggpool-q008-db-",
                            )
                        )
                    )
                except (OSError, TypeError) as error:
                    raise QualificationError(
                        "diagnostic database filesystem is unavailable"
                    ) from error
                database = isolated_database_root / "usage.sqlite3"
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
                    ("startup-readyz", "/v1/readyz"),
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
                        include_peak=benchmark_mode,
                    )
                )
                time.sleep(2.25)
                samples.append(
                    resource_sample(
                        "after-warmup",
                        process,
                        database,
                        f"http://127.0.0.1:{port}/api/stats/runtime",
                        include_peak=benchmark_mode,
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
                        include_peak=benchmark_mode,
                    )
                )
                if diagnose_publication_phases:
                    benchmark_process = process
                    benchmark: dict[str, Any] = {
                        "sample_count": PUBLICATION_PHASE_SAMPLES,
                        "config_fixture": Path(config_fixture).name,
                        "diagnostic_mode": "publication_commit_checkpoint_phase",
                        "wal_autocheckpoint_override_pages": (
                            qualification_wal_autocheckpoint_pages
                        ),
                        "cadence": benchmark_cadence_facts(content),
                        "runs": {},
                    }
                    diagnostic_runtime_url = (
                        f"http://127.0.0.1:{port}/api/stats/runtime"
                    )
                    samples.append(
                        resource_sample(
                            "publication-phases-before-quiescence",
                            benchmark_process,
                            database,
                            diagnostic_runtime_url,
                            include_peak=True,
                        )
                    )
                    benchmark["runs"]["direct_provider_control"] = (
                        _direct_provider_control(provider)
                    )
                    _diagnostic_finite_warmup(port)
                    baseline_tasks = _wait_for_diagnostic_quiescence(
                        diagnostic_runtime_url
                    )
                    baseline_runtime = _runtime_json(
                        diagnostic_runtime_url, "q008-server-key"
                    )
                    if baseline_runtime is None:
                        raise QualificationError(
                            "runtime snapshot was unavailable before Plan 239 batch"
                        )
                    baseline_database = _qualification_database_snapshot(
                        baseline_runtime
                    )
                    baseline_sequence = baseline_database["latest_record_seq"]
                    phase_run = _diagnostic_finite_batch(
                        port,
                        provider,
                        PUBLICATION_PHASE_SAMPLES,
                        benchmark_process,
                        database,
                        warmup=False,
                    )
                    if (
                        phase_run["completed_count"] != PUBLICATION_PHASE_SAMPLES
                        or phase_run["timeout_count"] != 0
                        or phase_run["failed_count"] != 0
                    ):
                        raise QualificationError(
                            "Plan 239 measured batch did not contain 60 "
                            "successful requests: "
                            f"completed={phase_run['completed_count']}, "
                            f"timeouts={phase_run['timeout_count']}, "
                            f"failed={phase_run['failed_count']}"
                        )
                    final_runtime = _runtime_json(
                        diagnostic_runtime_url, "q008-server-key"
                    )
                    if final_runtime is None:
                        raise QualificationError(
                            "runtime snapshot was unavailable after Plan 239 batch"
                        )
                    final_database = _qualification_database_snapshot(final_runtime)
                    records = _qualification_records_after(
                        final_runtime, baseline_sequence
                    )
                    correlation = _correlate_transaction_phases(
                        phase_run, records, PUBLICATION_PHASE_SAMPLES
                    )
                    benchmark["effective"] = final_database["effective"]
                    benchmark["collector"] = {
                        "schema_version": final_database["schema_version"],
                        "capacity": final_database["collector_capacity"],
                        "baseline_record_seq": baseline_sequence,
                        "final_record_seq": final_database["latest_record_seq"],
                        "records_after_baseline": len(records),
                    }
                    benchmark["runs"]["publication_phase_diagnostic"] = {
                        **phase_run,
                        **correlation,
                    }
                    final_tasks = _runtime_task_snapshot(final_runtime)
                    task_deltas = _task_tick_deltas(baseline_tasks, final_tasks)
                    benchmark["task_quiescence"] = {
                        "wait_timeout_s": DIAGNOSTIC_QUIESCENCE_TIMEOUT,
                        "fixed_task_names": list(DIAGNOSTIC_TASK_NAMES),
                        "baseline": baseline_tasks,
                        "final": final_tasks,
                        "deltas": task_deltas,
                        "background_db_activity": any(
                            value.get("tick_count_delta", 0) > 0
                            for value in task_deltas.values()
                            if isinstance(value.get("tick_count_delta"), int)
                        ),
                    }
                    if benchmark["task_quiescence"]["background_db_activity"]:
                        raise QualificationError(
                            "Plan 239 measured batch was contaminated by a "
                            "background task"
                        )
                    samples.append(
                        resource_sample(
                            "after-publication-phases-diagnostic",
                            benchmark_process,
                            database,
                            diagnostic_runtime_url,
                            include_peak=True,
                        )
                    )
                    stabilized = resource_sample(
                        "after-publication-phases-stabilization",
                        benchmark_process,
                        database,
                        diagnostic_runtime_url,
                        duration=STABILIZATION_SECONDS,
                        include_peak=True,
                    )
                    samples.append(stabilized)
                    if stabilized["pending_requests"] not in {0, None} or stabilized[
                        "active_reservations"
                    ] not in {0, None}:
                        raise QualificationError(
                            "Plan 239 request/reservation state did not converge"
                        )
                    benchmark["resource_summary"] = {
                        "final_pending_requests": stabilized["pending_requests"],
                        "final_active_reservations": stabilized["active_reservations"],
                        "final_finalization_jobs": stabilized["finalization_jobs"],
                        "final_rss_bytes": stabilized["rss_bytes"],
                        "final_peak_rss_bytes": stabilized.get("peak_rss_bytes"),
                    }
                    benchmark["interpretation"] = (
                        "qualification-only transaction phase correlation; no "
                        "production SQLite policy or performance threshold applied"
                    )
                    report["benchmark"] = benchmark
                elif diagnose_publication_storage is not None:
                    benchmark_process = process
                    benchmark: dict[str, Any] = {
                        "sample_count": diagnose_publication_storage,
                        "config_fixture": Path(config_fixture).name,
                        "diagnostic_mode": "publication_storage",
                        "database_storage": (
                            "isolated-filesystem"
                            if diagnostic_database_dir is not None
                            else "qualification-root"
                        ),
                        "cadence": benchmark_cadence_facts(content),
                        "runs": {},
                    }
                    diagnostic_runtime_url = (
                        f"http://127.0.0.1:{port}/api/stats/runtime"
                    )
                    samples.append(
                        resource_sample(
                            "publication-storage-before-quiescence",
                            benchmark_process,
                            database,
                            diagnostic_runtime_url,
                            include_peak=True,
                        )
                    )
                    direct_control = _direct_provider_control(provider)
                    benchmark["runs"]["direct_provider_control"] = direct_control
                    _diagnostic_finite_warmup(port)
                    baseline_tasks = _wait_for_diagnostic_quiescence(
                        diagnostic_runtime_url
                    )
                    publication_storage = _diagnostic_finite_batch(
                        port,
                        provider,
                        diagnose_publication_storage,
                        benchmark_process,
                        database,
                        warmup=False,
                    )
                    benchmark["runs"]["publication_storage_diagnostic"] = (
                        publication_storage
                    )
                    final_runtime = _runtime_json(
                        diagnostic_runtime_url, "q008-server-key"
                    )
                    final_tasks = _runtime_task_snapshot(final_runtime or {})
                    if not final_tasks:
                        raise QualificationError(
                            "runtime task snapshot was unavailable after diagnostic"
                        )
                    task_deltas = _task_tick_deltas(baseline_tasks, final_tasks)
                    benchmark["task_quiescence"] = {
                        "wait_timeout_s": DIAGNOSTIC_QUIESCENCE_TIMEOUT,
                        "fixed_task_names": list(DIAGNOSTIC_TASK_NAMES),
                        "baseline": baseline_tasks,
                        "final": final_tasks,
                        "deltas": task_deltas,
                        "background_db_activity": any(
                            value.get("tick_count_delta", 0) > 0
                            for value in task_deltas.values()
                            if isinstance(value.get("tick_count_delta"), int)
                        ),
                    }
                    benchmark["interpretation"] = (
                        "diagnostic-only publication/storage localization; no "
                        "runtime change or performance threshold applied"
                    )
                    samples.append(
                        resource_sample(
                            "after-publication-storage-diagnostic",
                            benchmark_process,
                            database,
                            diagnostic_runtime_url,
                            include_peak=True,
                        )
                    )
                    stabilized = resource_sample(
                        "after-publication-storage-stabilization",
                        benchmark_process,
                        database,
                        diagnostic_runtime_url,
                        duration=STABILIZATION_SECONDS,
                        include_peak=True,
                    )
                    samples.append(stabilized)
                    if stabilized["pending_requests"] not in {0, None} or stabilized[
                        "active_reservations"
                    ] not in {0, None}:
                        raise QualificationError(
                            "publication/storage diagnostic state did not converge"
                        )
                    benchmark["resource_summary"] = {
                        "final_pending_requests": stabilized["pending_requests"],
                        "final_active_reservations": stabilized["active_reservations"],
                        "final_finalization_jobs": stabilized["finalization_jobs"],
                        "final_rss_bytes": stabilized["rss_bytes"],
                        "final_peak_rss_bytes": stabilized.get("peak_rss_bytes"),
                    }
                    report["benchmark"] = benchmark
                elif benchmark_samples:
                    benchmark_process = process
                    benchmark: dict[str, Any] = {
                        "sample_count": benchmark_samples,
                        "concurrency_batch_size": CONCURRENCY_BATCH_SIZE,
                        "client_concurrency": CONCURRENCY_WORKERS,
                        "config_fixture": Path(config_fixture).name,
                        "cadence": benchmark_cadence_facts(content),
                        "runs": {},
                    }
                    benchmark_snapshots: list[dict[str, Any]] = []

                    def benchmark_sample(
                        label: str, duration: float = SAMPLE_SECONDS
                    ) -> None:
                        benchmark_snapshots.append(
                            resource_sample(
                                label,
                                benchmark_process,
                                database,
                                f"http://127.0.0.1:{port}/api/stats/runtime",
                                duration=duration,
                                include_peak=True,
                            )
                        )
                        samples.append(benchmark_snapshots[-1])

                    benchmark_sample("benchmark-baseline")
                    finite, finite_measured = _sequential_benchmark(
                        benchmark_process,
                        port,
                        NATIVE_FINITE_CASE,
                        benchmark_samples,
                        provider,
                    )
                    if not finite_measured:
                        raise QualificationError(
                            "native finite benchmark warm-up failed"
                        )
                    benchmark["runs"]["native_responses_finite"] = finite
                    benchmark_sample("after-native-responses-finite")
                    streaming, streaming_measured = _sequential_benchmark(
                        benchmark_process,
                        port,
                        NATIVE_STREAMING_CASE,
                        benchmark_samples,
                        provider,
                    )
                    if not streaming_measured:
                        raise QualificationError(
                            "native streaming benchmark warm-up failed"
                        )
                    benchmark["runs"]["native_responses_streaming"] = streaming
                    benchmark_sample("after-native-responses-streaming")
                    translated, translated_measured = _sequential_benchmark(
                        benchmark_process,
                        port,
                        TRANSLATED_STREAMING_CASE,
                        benchmark_samples,
                        provider,
                    )
                    benchmark["runs"]["translated_responses_to_messages_streaming"] = (
                        translated
                    )
                    if translated_measured:
                        benchmark_sample("after-translated-streaming")
                    concurrent = _concurrent_finite_benchmark(benchmark_process, port)
                    if concurrent["status"] != "measured":
                        raise QualificationError(
                            "concurrency-4 benchmark did not complete all requests"
                        )
                    benchmark["runs"]["concurrent_native_responses_finite"] = concurrent
                    if diagnose_finite_tail is not None:
                        benchmark_sample("before-finite-tail-diagnostic")
                        direct_control = _direct_provider_control(provider)
                        benchmark["runs"]["direct_provider_control"] = direct_control
                        finite_tail = _diagnostic_finite_batch(
                            port,
                            provider,
                            diagnose_finite_tail,
                            benchmark_process,
                        )
                        benchmark["runs"]["finite_tail_diagnostic"] = finite_tail
                        benchmark["finite_tail_diagnostic"] = {
                            "diagnostic_sample_count": diagnose_finite_tail,
                            "config_fixture": Path(config_fixture).name,
                            "direct_control_sample_count": DIRECT_CONTROL_SAMPLES,
                            "direct_control_warmups": DIRECT_CONTROL_WARMUPS,
                        }
                        benchmark_sample("after-finite-tail-diagnostic")
                    benchmark_sample("after-concurrency-4")
                    benchmark_sample(
                        "after-benchmark-stabilization", STABILIZATION_SECONDS
                    )
                    stabilized = benchmark_snapshots[-1]
                    if stabilized["pending_requests"] not in {0, None} or stabilized[
                        "active_reservations"
                    ] not in {0, None}:
                        raise QualificationError(
                            "benchmark state did not converge after stabilization"
                        )
                    peak_values = [
                        int(value)
                        for value in (
                            sample.get("peak_rss_bytes")
                            for sample in benchmark_snapshots
                        )
                        if isinstance(value, int)
                    ]
                    benchmark["resource_summary"] = {
                        "baseline_rss_bytes": benchmark_snapshots[0]["rss_bytes"],
                        "final_rss_bytes": stabilized["rss_bytes"],
                        "baseline_peak_rss_bytes": benchmark_snapshots[0].get(
                            "peak_rss_bytes"
                        ),
                        "peak_rss_bytes": max(peak_values, default=None),
                        "final_pending_requests": stabilized["pending_requests"],
                        "final_active_reservations": stabilized["active_reservations"],
                        "final_finalization_jobs": stabilized["finalization_jobs"],
                    }
                    benchmark["interpretation"] = (
                        "descriptive loopback characterization; no performance "
                        "threshold applied"
                    )
                    report["benchmark"] = benchmark
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
                        include_peak=benchmark_mode,
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
                        include_peak=benchmark_mode,
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
                        "bounded samples for release characterization; no fixed "
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
            if benchmark_mode:
                environment["loopback_provider_path_counts"] = provider.path_counts()
        report["resource_samples"] = samples
        report["commands"] = [item.as_dict(root, binary) for item in commands]
        report["isolated_temporary_root"] = True
        if benchmark_mode:
            end_board, _ = board_metadata()
            if end_board:
                report["environment"]["power_thermal_mode_celsius_end"] = end_board.get(
                    "power_thermal_mode_celsius"
                )
                report["environment"]["cpu_frequency_policy_end"] = end_board.get(
                    "cpu_frequency_policy"
                )
                report["environment"]["cpu_governor_end"] = end_board.get(
                    "cpu_governor"
                )
                if report["environment"].get("cpu_governor") != end_board.get(
                    "cpu_governor"
                ):
                    report["findings"].append(
                        "CPU frequency governor changed during run"
                    )
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
    parser.add_argument(
        "--benchmark-samples",
        type=_benchmark_sample_count,
        default=0,
        metavar="N",
        help="Run the optional physical-SBC benchmark with 1..100 samples.",
    )
    parser.add_argument(
        "--diagnose-finite-tail",
        type=_diagnose_sample_count,
        default=None,
        metavar="N",
        help=(
            "Run the diagnostic-only sequential finite-tail batch with 10..200 "
            "samples; requires --benchmark-samples and the benchmark fixture."
        ),
    )
    parser.add_argument(
        "--diagnose-publication-storage",
        type=_publication_storage_sample_count,
        default=None,
        metavar="N",
        help=(
            "Run the Plan 238 publication/storage diagnostic with 20..200 "
            "samples; requires the benchmark fixture and physical SBC gate."
        ),
    )
    parser.add_argument(
        "--diagnose-publication-phases",
        action="store_true",
        help=(
            "Run the Plan 239 qualification-only 60-request publication/"
            "finalization phase diagnostic; requires the benchmark fixture."
        ),
    )
    parser.add_argument(
        "--qualification-wal-autocheckpoint-pages",
        type=_qualification_wal_autocheckpoint_pages,
        default=None,
        metavar="N",
        help=(
            "Set the feature-only startup wal_autocheckpoint override for "
            "Plan 239 phase diagnostics (0..100000)."
        ),
    )
    parser.add_argument(
        "--diagnostic-database-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Create only the diagnostic SQLite files in a temporary child of "
            "DIR; requires --diagnose-publication-phases or "
            "--diagnose-publication-storage."
        ),
    )
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
            benchmark_samples=args.benchmark_samples,
            diagnose_finite_tail=args.diagnose_finite_tail,
            diagnose_publication_storage=args.diagnose_publication_storage,
            diagnose_publication_phases=args.diagnose_publication_phases,
            qualification_wal_autocheckpoint_pages=(
                args.qualification_wal_autocheckpoint_pages
            ),
            diagnostic_database_dir=args.diagnostic_database_dir,
        )
    except (OSError, QualificationError, ValueError, sqlite3.Error) as error:
        requested_benchmark = (
            args.diagnose_publication_storage is not None
            or args.diagnose_publication_phases
        )
        with contextlib.suppress(TypeError, ValueError):
            requested_benchmark = (
                requested_benchmark or int(str(args.benchmark_samples)) > 0
            )
        report = {
            "schema_version": SCHEMA_V2 if requested_benchmark else SCHEMA_V1,
            "plan": "SBC qualification",
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
