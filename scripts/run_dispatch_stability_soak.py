"""Dispatch stability runtime-validation runner.

Runs a process-level, file-backed SQLite soak test of EggPool's dispatch
stability.  Starts EggPool as a real ``eggpool serve --verbose`` subprocess
with a deterministic local mock upstream, exercises dashboard/runtime metrics
polling, and produces one concise JSON summary.

Usage::

    uv run python scripts/run_dispatch_stability_soak.py \
        --profile sbc-reference \
        --duration-seconds 300 \
        --output /tmp/eggpool-runtime-validation.json

The runner never persists request content, provider secrets, or credential
values.  All environment snapshots and config dumps are redacted before
writing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import platform
import random
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("dispatch_soak")

# ---------------------------------------------------------------------------
# Version / schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2
SCRIPT_VERSION = "2.1.0"


def _validate_duration(value: str) -> int:
    try:
        ivalue = int(value)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from err
    if ivalue < 30:
        raise argparse.ArgumentTypeError(f"must be at least 30, got {ivalue}")
    return ivalue


# ---------------------------------------------------------------------------
# Utility: free port, fingerprint, redaction
# ---------------------------------------------------------------------------

_CREDENTIAL_REDACT_PATTERNS: tuple[str, ...] = (
    "sk-",
    "Bearer ",
    "key-",
    "token-",
    "password=",
    "secret=",
    "api_key",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _redact(value: str) -> str:
    lower = value.lower()
    for pat in _CREDENTIAL_REDACT_PATTERNS:
        if pat.lower() in lower:
            return _fingerprint(value)
    return value


def _redact_dict(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = _redact(v)
        elif isinstance(v, dict):
            out[k] = _redact_dict(v)  # pyright: ignore[reportUnknownArgumentType]
        else:
            out[k] = v
    return out


def _git_sha() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _memory_total_bytes() -> int | None:
    """Return total system memory in bytes, or None on failure."""
    if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            page_count = os.sysconf("SC_PHYS_PAGES")
            if page_size > 0 and page_count > 0:
                return int(page_size * page_count)
        except (TypeError, ValueError, OSError):
            pass
    if sys.platform == "darwin":
        try:
            import ctypes

            buf = ctypes.create_string_buffer(8)
            ctypes.CDLL("libc.dylib").sysctlbyname(
                b"hw.memsize",
                buf,
                ctypes.pointer(ctypes.c_size_t(8)),
                None,
                0,
            )
            return struct.unpack("Q", buf)[0]
        except Exception:
            return None
    return None


def _collect_environment() -> dict[str, Any]:
    return {
        "git_sha": _git_sha(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "system": platform.system(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": _memory_total_bytes(),
    }


# ---------------------------------------------------------------------------
# Process RSS measurement (child PID)
# ---------------------------------------------------------------------------


def read_process_rss_bytes(pid: int) -> int | None:
    """Return current RSS for the requested process in bytes.

    Linux: parses VmRSS from /proc/<pid>/status (KiB, multiplied by 1024).
    macOS/BSD: runs ``ps -o rss= -p <pid>`` (KiB, multiplied by 1024).
    Returns None for missing process, malformed values, or unsupported OS.
    Never returns 0 on failure.
    """
    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        parts = line.split()
                        if len(parts) >= 3 and parts[2].lower() in ("kb", "kib"):
                            value = int(parts[1])
                            if value > 0:
                                return value * 1024
                        return None
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            return None
        return None

    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            value = int(result.stdout.strip())
            if value > 0:
                return value * 1024
    except (subprocess.SubprocessError, ValueError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Duration planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DurationPlan:
    """Derived phase durations from a single total_s input."""

    total_s: float
    warmup_s: float
    early_window_s: float
    drain_s: float
    late_window_s: float
    poll_interval_s: float

    dispatch_p95_ratio_limit: float
    dispatch_p99_ratio_limit: float
    throughput_decline_limit: float
    max_pending_requests: int
    max_active_reservations: int


def build_duration_plan(total_s: float) -> DurationPlan:
    """Derive bounded phases from total duration.

    warm-up:   min(60s, max(5s, total * 10%))
    drain:     min(30s, max(2s, total * 5%))
    remaining: split equally between early and late windows
    """
    warmup_s = min(60.0, max(5.0, total_s * 0.10))
    drain_s = min(30.0, max(2.0, total_s * 0.05))
    remaining = total_s - warmup_s - drain_s
    half_window = remaining / 2.0 if remaining > 0 else 1.0
    early_window_s = max(half_window, 1.0)
    late_window_s = max(half_window, 1.0)

    if total_s <= 120:
        poll_interval = 2.0
    elif total_s <= 600:
        poll_interval = 5.0
    else:
        poll_interval = 10.0

    return DurationPlan(
        total_s=total_s,
        warmup_s=warmup_s,
        early_window_s=early_window_s,
        drain_s=drain_s,
        late_window_s=late_window_s,
        poll_interval_s=poll_interval,
        dispatch_p95_ratio_limit=1.50 if total_s <= 300 else 1.30,
        dispatch_p99_ratio_limit=2.00 if total_s <= 300 else 1.80,
        throughput_decline_limit=0.20 if total_s <= 300 else 0.15,
        max_pending_requests=0,
        max_active_reservations=0,
    )


# ---------------------------------------------------------------------------
# Mock upstream server (stdlib threading)
# ---------------------------------------------------------------------------


class MockUpstreamState:
    """Mutable state shared between mock servers and the soak runner."""

    def __init__(
        self,
        *,
        chunks_per_stream: int = 3,
        chunk_delay_s: float = 0.01,
        error_rate: float = 0.0,
    ) -> None:
        self.request_count: int = 0
        self.error_count: int = 0
        self.latencies_ms: list[float] = []
        self.chunks_per_stream = chunks_per_stream
        self.chunk_delay_s = chunk_delay_s
        self.error_rate = error_rate
        self.lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "request_count": self.request_count,
                "error_count": self.error_count,
                "avg_latency_ms": (
                    sum(self.latencies_ms) / len(self.latencies_ms)
                    if self.latencies_ms
                    else 0.0
                ),
                "latency_p95_ms": (
                    sorted(self.latencies_ms)[int(len(self.latencies_ms) * 0.95)]
                    if self.latencies_ms
                    else 0.0
                ),
            }


class MockUpstreamHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible upstream handler."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._send_models()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") in (
            "/v1/chat/completions",
            "/chat/completions",
        ):
            self._handle_chat()
        else:
            self.send_error(404)

    def _send_models(self) -> None:
        body = json.dumps(
            {
                "object": "list",
                "data": [{"id": "gpt-4", "object": "model", "owned_by": "soak-mock"}],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_chat(self) -> None:
        state: MockUpstreamState = self.server.mock_state  # type: ignore[attr-defined]
        t0 = time.monotonic()

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw)
        stream = body.get("stream", False)
        model = body.get("model", "gpt-4")

        with state.lock:  # pyright: ignore[reportUnknownMemberType]
            state.request_count += 1  # pyright: ignore[reportUnknownMemberType]
            if random.random() < state.error_rate:  # pyright: ignore[reportUnknownMemberType]
                state.error_count += 1  # pyright: ignore[reportUnknownMemberType]
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                err = json.dumps({"error": {"message": "rate limited"}}).encode()
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                elapsed = (time.monotonic() - t0) * 1000
                state.latencies_ms.append(elapsed)  # pyright: ignore[reportUnknownMemberType]
                return

        if not stream:
            resp = {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": model,
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
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            elapsed = (time.monotonic() - t0) * 1000
            with state.lock:  # pyright: ignore[reportUnknownMemberType]
                state.latencies_ms.append(elapsed)  # pyright: ignore[reportUnknownMemberType]
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        n_chunks: int = 0
        delay: float = 0.0
        mock_state: MockUpstreamState = self.server.mock_state  # type: ignore[attr-defined]
        with mock_state.lock:  # pyright: ignore[reportUnknownMemberType]
            n_chunks = int(mock_state.chunks_per_stream)  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]
            delay = float(mock_state.chunk_delay_s)  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType]

        for i in range(n_chunks):
            chunk = {
                "id": "cmpl-mock",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": f"t{i}"},
                        "finish_reason": None,
                    }
                ],
            }
            line = f"data: {json.dumps(chunk)}\n\n"
            self.wfile.write(line.encode())
            self.wfile.flush()
            if delay > 0:
                time.sleep(delay)  # pyright: ignore[reportUnknownArgumentType]

        done_line = "data: [DONE]\n\n"
        self.wfile.write(done_line.encode())
        self.wfile.flush()
        self.close_connection = True

        elapsed = (time.monotonic() - t0) * 1000
        with state.lock:  # pyright: ignore[reportUnknownMemberType]
            state.latencies_ms.append(elapsed)  # pyright: ignore[reportUnknownMemberType]


def _start_mock_upstream(state: MockUpstreamState) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), MockUpstreamHandler)
    server.mock_state = state  # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ---------------------------------------------------------------------------
# Config writer
# ---------------------------------------------------------------------------


def _write_soak_config(
    path: Path,
    *,
    server_port: int,
    upstream_port: int,
    db_path: str,
    api_key: str,
    server_threads: int = 4,
    db_worker_threads: int = 2,
    dispatch_batch_size: int = 8,
    dispatch_batch_wait_ms: int = 50,
    maintenance_batch_size: int = 200,
    maintenance_budget_ms: int = 500,
    routing_trace_mode: str = "sampled",
    routing_trace_sample_rate: float = 0.05,
    metrics_write_mode: str = "balanced",
    metrics_flush_interval_s: float = 30.0,
    transcoder_enabled: bool = True,
    compression_enabled: bool = False,
) -> None:
    config = f"""\
[server]
api_key = "{api_key}"
host = "127.0.0.1"
port = {server_port}
threads = {server_threads}

[database]
path = "{db_path}"
worker_threads = {db_worker_threads}

[database.dispatch_writer]
max_batch_size = {dispatch_batch_size}
max_batch_wait_ms = {dispatch_batch_wait_ms}

[maintenance]
max_rows_per_batch = {maintenance_batch_size}
max_tick_duration_ms = {maintenance_budget_ms}

[routing.trace]
mode = "{routing_trace_mode}"
sample_rate = {routing_trace_sample_rate}

[metrics]
write_mode = "{metrics_write_mode}"
flush_interval_s = {metrics_flush_interval_s}

[transcoder]
enabled = {str(transcoder_enabled).lower()}

[compression]
enabled = {str(compression_enabled).lower()}

[models]
startup_refresh = true
refresh_interval_s = 0
allow_stale_catalog = true

[upstream]
base_url = "http://127.0.0.1:{upstream_port}/v1"

[providers.mock-provider]
id = "mock-provider"
base_url = "http://127.0.0.1:{upstream_port}/v1"
protocols = ["openai"]

[providers.mock-provider.models_endpoint]
method = "GET"
path = "/models"

[[providers.mock-provider.static_models]]
id = "gpt-4"
protocol = "openai"

[[providers.mock-provider.accounts]]
name = "soak-acct"
api_key = "{api_key}"
enabled = true
weight = 1.0

[model_info]
enabled = false

[dashboard]
enabled = true
"""
    path.write_text(config, encoding="utf-8")


# ---------------------------------------------------------------------------
# Profile definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SoakProfile:
    """Configuration for a soak test profile."""

    name: str
    description: str
    concurrency: int
    requests_per_burst: int
    burst_interval_s: float
    chunks_per_stream: int
    chunk_delay_s: float
    error_rate: float
    streaming_ratio: float
    cancel_rate: float
    server_threads: int
    db_worker_threads: int
    dispatch_batch_size: int
    dispatch_batch_wait_ms: int
    maintenance_batch_size: int
    maintenance_budget_ms: int
    metrics_write_mode: str
    metrics_flush_interval_s: float
    routing_trace_mode: str
    routing_trace_sample_rate: float
    rss_required: bool = False


PROFILES: dict[str, SoakProfile] = {
    "balanced-file-backed": SoakProfile(
        name="balanced-file-backed",
        description="Representative sustained mixed traffic",
        concurrency=5,
        requests_per_burst=20,
        burst_interval_s=2.0,
        chunks_per_stream=4,
        chunk_delay_s=0.005,
        error_rate=0.0,
        streaming_ratio=0.6,
        cancel_rate=0.0,
        server_threads=4,
        db_worker_threads=2,
        dispatch_batch_size=8,
        dispatch_batch_wait_ms=50,
        maintenance_batch_size=200,
        maintenance_budget_ms=500,
        metrics_write_mode="balanced",
        metrics_flush_interval_s=30.0,
        routing_trace_mode="sampled",
        routing_trace_sample_rate=0.05,
    ),
    "burst-recovery": SoakProfile(
        name="burst-recovery",
        description="Repeated high-concurrency bursts and drain periods",
        concurrency=20,
        requests_per_burst=50,
        burst_interval_s=5.0,
        chunks_per_stream=6,
        chunk_delay_s=0.01,
        error_rate=0.05,
        streaming_ratio=1.0,
        cancel_rate=0.0,
        server_threads=4,
        db_worker_threads=2,
        dispatch_batch_size=16,
        dispatch_batch_wait_ms=100,
        maintenance_batch_size=200,
        maintenance_budget_ms=500,
        metrics_write_mode="balanced",
        metrics_flush_interval_s=30.0,
        routing_trace_mode="sampled",
        routing_trace_sample_rate=0.05,
    ),
    "cancellation-maintenance": SoakProfile(
        name="cancellation-maintenance",
        description="Cancellations while retention/reconciliation runs",
        concurrency=10,
        requests_per_burst=30,
        burst_interval_s=3.0,
        chunks_per_stream=8,
        chunk_delay_s=0.02,
        error_rate=0.0,
        streaming_ratio=1.0,
        cancel_rate=0.3,
        server_threads=4,
        db_worker_threads=2,
        dispatch_batch_size=8,
        dispatch_batch_wait_ms=50,
        maintenance_batch_size=100,
        maintenance_budget_ms=250,
        metrics_write_mode="balanced",
        metrics_flush_interval_s=15.0,
        routing_trace_mode="sampled",
        routing_trace_sample_rate=0.1,
    ),
    "rehash-churn": SoakProfile(
        name="rehash-churn",
        description="Active streams across repeated valid and rejected rehash attempts",
        concurrency=3,
        requests_per_burst=15,
        burst_interval_s=1.5,
        chunks_per_stream=4,
        chunk_delay_s=0.01,
        error_rate=0.0,
        streaming_ratio=0.8,
        cancel_rate=0.0,
        server_threads=4,
        db_worker_threads=2,
        dispatch_batch_size=8,
        dispatch_batch_wait_ms=50,
        maintenance_batch_size=200,
        maintenance_budget_ms=500,
        metrics_write_mode="immediate",
        metrics_flush_interval_s=10.0,
        routing_trace_mode="all",
        routing_trace_sample_rate=1.0,
    ),
    "slow-storage": SoakProfile(
        name="slow-storage",
        description="Constrained SQLite write latency (SBC-like)",
        concurrency=2,
        requests_per_burst=10,
        burst_interval_s=3.0,
        chunks_per_stream=3,
        chunk_delay_s=0.02,
        error_rate=0.0,
        streaming_ratio=0.5,
        cancel_rate=0.0,
        server_threads=1,
        db_worker_threads=1,
        dispatch_batch_size=4,
        dispatch_batch_wait_ms=100,
        maintenance_batch_size=100,
        maintenance_budget_ms=1000,
        metrics_write_mode="low_wear",
        metrics_flush_interval_s=120.0,
        routing_trace_mode="off",
        routing_trace_sample_rate=0.0,
    ),
    "sbc-reference": SoakProfile(
        name="sbc-reference",
        description="Conservative one-thread, one-DB-worker profile",
        concurrency=1,
        requests_per_burst=5,
        burst_interval_s=2.0,
        chunks_per_stream=3,
        chunk_delay_s=0.01,
        error_rate=0.0,
        streaming_ratio=0.5,
        cancel_rate=0.0,
        server_threads=1,
        db_worker_threads=1,
        dispatch_batch_size=4,
        dispatch_batch_wait_ms=100,
        maintenance_batch_size=100,
        maintenance_budget_ms=1000,
        metrics_write_mode="low_wear",
        metrics_flush_interval_s=120.0,
        routing_trace_mode="off",
        routing_trace_sample_rate=0.0,
        rss_required=True,
    ),
}


# ---------------------------------------------------------------------------
# Metrics collection
# ---------------------------------------------------------------------------


@dataclass
class WindowMetrics:
    """Metrics collected during a measurement window."""

    name: str
    start_time: float
    end_time: float = 0.0
    request_count: int = 0
    success_count: int = 0
    stream_success_count: int = 0
    nonstream_success_count: int = 0
    error_count: int = 0
    dispatch_latencies_ms: list[float] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list
    )
    pending_at_end: int | None = None
    active_reservations_at_end: int | None = None
    upstream_requests: int = 0
    upstream_errors: int = 0
    db_size_bytes: int = 0
    rss_bytes: int | None = None

    @property
    def duration_s(self) -> float:
        return self.end_time - self.start_time

    @property
    def throughput_rps(self) -> float:
        return self.request_count / self.duration_s if self.duration_s > 0 else 0.0

    @property
    def observed_error_rate(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self.error_count / self.request_count

    def percentile(self, p: float) -> float:
        if not self.dispatch_latencies_ms:
            return 0.0
        s = sorted(self.dispatch_latencies_ms)
        idx = int(len(s) * p)
        return s[min(idx, len(s) - 1)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_s": round(self.duration_s, 3),
            "request_count": self.request_count,
            "success_count": self.success_count,
            "stream_success_count": self.stream_success_count,
            "nonstream_success_count": self.nonstream_success_count,
            "error_count": self.error_count,
            "observed_error_rate": round(self.observed_error_rate, 4),
            "throughput_rps": round(self.throughput_rps, 3),
            "dispatch_p50_ms": round(self.percentile(0.50), 2),
            "dispatch_p95_ms": round(self.percentile(0.95), 2),
            "dispatch_p99_ms": round(self.percentile(0.99), 2),
            "pending_at_end": self.pending_at_end,
            "active_reservations_at_end": self.active_reservations_at_end,
            "upstream_requests": self.upstream_requests,
            "upstream_errors": self.upstream_errors,
            "db_size_bytes": self.db_size_bytes,
            "rss_bytes": self.rss_bytes,
        }


@dataclass
class MetricsSnapshot:
    """A single point-in-time metrics sample."""

    timestamp: float
    elapsed_s: float
    upstream_requests: int
    upstream_errors: int
    upstream_avg_latency_ms: float
    pending_requests: int | None = None
    active_reservations: int | None = None
    db_size_bytes: int = 0
    rss_bytes: int | None = None
    db_lock_wait_p95_ms: float | None = None
    db_lock_wait_max_ms: float | None = None
    db_lock_wait_sample_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "timestamp": self.timestamp,
            "elapsed_s": round(self.elapsed_s, 2),
            "upstream_requests": self.upstream_requests,
            "upstream_errors": self.upstream_errors,
            "upstream_avg_latency_ms": round(self.upstream_avg_latency_ms, 2),
            "pending_requests": self.pending_requests,
            "active_reservations": self.active_reservations,
            "db_size_bytes": self.db_size_bytes,
            "rss_bytes": self.rss_bytes,
        }
        if self.db_lock_wait_p95_ms is not None:
            d["db_lock_wait_p95_ms"] = round(self.db_lock_wait_p95_ms, 2)
        if self.db_lock_wait_max_ms is not None:
            d["db_lock_wait_max_ms"] = round(self.db_lock_wait_max_ms, 2)
        if self.db_lock_wait_sample_count is not None:
            d["db_lock_wait_sample_count"] = self.db_lock_wait_sample_count
        return d


@dataclass
class PollingStats:
    """Bounded diagnostics for dashboard/runtime polling."""

    summary_successes: int = 0
    summary_failures: int = 0
    runtime_successes: int = 0
    runtime_failures: int = 0
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary_successes": self.summary_successes,
            "summary_failures": self.summary_failures,
            "runtime_successes": self.runtime_successes,
            "runtime_failures": self.runtime_failures,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# Pure gate evaluators
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuiescenceResult:
    """Outcome of a bounded post-load quiescence poll."""

    snapshot: MetricsSnapshot | None
    drained: bool
    attempts: int
    elapsed_s: float
    failure_reason: str | None
    pending_requests: int | None = None
    active_reservations: int | None = None


@dataclass(frozen=True, slots=True)
class RatioGateResult:
    """Result of evaluating a single latency ratio cap."""

    passed: bool
    early_ms: float | None
    late_ms: float | None
    ratio: float | None
    limit: float
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class WorkloadGateResult:
    """Result of evaluating the per-window useful-work gate."""

    passed: bool
    failure_reasons: tuple[str, ...]
    early_attempts: int
    late_attempts: int
    early_successes: int
    late_successes: int
    early_errors: int
    late_errors: int
    early_error_rate: float
    late_error_rate: float
    expected_error_rate: float
    allowed_error_fraction: float


def evaluate_drain_gate(
    final_snapshot: MetricsSnapshot | None,
    max_pending: int,
    max_active: int,
) -> tuple[bool, str | None]:
    """Evaluate the drain gate against a final post-load snapshot.

    Fails closed on a missing snapshot, unavailable fields, or pending /
    active-reservation counts above the configured limits.
    """
    if final_snapshot is None:
        return False, "no final runtime snapshot"
    if (
        final_snapshot.pending_requests is None
        or final_snapshot.active_reservations is None
    ):
        return False, "drain metrics unavailable"
    if (
        final_snapshot.pending_requests <= max_pending
        and final_snapshot.active_reservations <= max_active
    ):
        return True, None
    return (
        False,
        f"drain: pending={final_snapshot.pending_requests} "
        f"reservations={final_snapshot.active_reservations}",
    )


def evaluate_ratio_gate(
    early_value: float | None,
    late_value: float | None,
    *,
    limit: float,
    label: str,
) -> RatioGateResult:
    """Apply a direct late/early ratio cap.

    Fails closed when either value is unavailable, when the early baseline
    is non-positive, or when the ratio exceeds the supplied direct cap.
    The ratio cap is applied directly — it is **not** an additive increase.
    """
    if early_value is None or late_value is None:
        return RatioGateResult(
            passed=False,
            early_ms=early_value,
            late_ms=late_value,
            ratio=None,
            limit=limit,
            failure_reason=f"{label}: latency samples unavailable",
        )
    if early_value <= 0:
        return RatioGateResult(
            passed=False,
            early_ms=early_value,
            late_ms=late_value,
            ratio=None,
            limit=limit,
            failure_reason=f"{label}: early baseline non-positive ({early_value:.2f})",
        )
    ratio = late_value / early_value
    if ratio <= limit:
        return RatioGateResult(
            passed=True,
            early_ms=early_value,
            late_ms=late_value,
            ratio=ratio,
            limit=limit,
            failure_reason=None,
        )
    return RatioGateResult(
        passed=False,
        early_ms=early_value,
        late_ms=late_value,
        ratio=ratio,
        limit=limit,
        failure_reason=f"{label}: ratio={ratio:.4f} exceeds limit={limit:.4f}",
    )


def evaluate_workload_gate(
    early: WindowMetrics,
    late: WindowMetrics,
    *,
    expected_error_rate: float,
    require_stream_and_nonstream: bool = False,
) -> WorkloadGateResult:
    """Require useful per-window work and bounded error rates.

    Each window must have at least one completed attempt and one
    successful request. Zero-error profiles (expected_error_rate == 0)
    reject any unexpected error. Configured-error profiles tolerate up
    to ``min(0.25, expected_error_rate + 0.10)`` errors per request.
    """
    reasons: list[str] = []

    if early.request_count == 0:
        reasons.append("early window: zero attempts")
    if late.request_count == 0:
        reasons.append("late window: zero attempts")
    if early.success_count == 0:
        reasons.append("early window: zero successes")
    if late.success_count == 0:
        reasons.append("late window: zero successes")

    allowed_error_fraction = min(0.25, expected_error_rate + 0.10)

    if expected_error_rate <= 0.0:
        if early.error_count > 0:
            reasons.append(f"early window: {early.error_count} unexpected errors")
        if late.error_count > 0:
            reasons.append(f"late window: {late.error_count} unexpected errors")
    else:
        if early.observed_error_rate > allowed_error_fraction:
            reasons.append(
                f"early window: error rate {early.observed_error_rate:.4f} "
                f"exceeds allowed fraction {allowed_error_fraction:.4f}"
            )
        if late.observed_error_rate > allowed_error_fraction:
            reasons.append(
                f"late window: error rate {late.observed_error_rate:.4f} "
                f"exceeds allowed fraction {allowed_error_fraction:.4f}"
            )

    if require_stream_and_nonstream:
        if early.stream_success_count + late.stream_success_count == 0:
            reasons.append("no streaming successes across windows")
        if early.nonstream_success_count + late.nonstream_success_count == 0:
            reasons.append("no non-streaming successes across windows")

    return WorkloadGateResult(
        passed=not reasons,
        failure_reasons=tuple(reasons),
        early_attempts=early.request_count,
        late_attempts=late.request_count,
        early_successes=early.success_count,
        late_successes=late.success_count,
        early_errors=early.error_count,
        late_errors=late.error_count,
        early_error_rate=early.observed_error_rate,
        late_error_rate=late.observed_error_rate,
        expected_error_rate=expected_error_rate,
        allowed_error_fraction=allowed_error_fraction,
    )


def _write_atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _gate_passed(value: Any) -> bool:
    """Return ``True`` only if ``value`` represents a passing gate."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        gate_dict: dict[str, Any] = value  # type: ignore[redundant-cast]
        return gate_dict.get("passed") is True
    return False


# ---------------------------------------------------------------------------
# Load generator
# ---------------------------------------------------------------------------

_thread_lock = threading.Lock()

# Exceptions that the load generator treats as expected stream-consumption
# errors and accounts as request errors rather than re-raising.
_EXPECTED_STREAM_EXC: tuple[type[BaseException], ...] = (
    asyncio.CancelledError,
    ConnectionError,
    TimeoutError,
    OSError,
)


async def _generate_load(
    *,
    base_url: str,
    api_key: str,
    profile: SoakProfile,
    deadline: float,
    rng: random.Random,
    metrics: deque[MetricsSnapshot],
    window: WindowMetrics,
    cancelled_flag: asyncio.Event,
    generation_counter: list[int],
    request_shapes: Iterable[str] | None = None,
) -> None:
    """Generate load against the running EggPool server until deadline."""
    import httpx

    sem = asyncio.Semaphore(profile.concurrency)
    chunk_counts: list[int] = []
    shape_iterator: Iterable[str] | None = (
        iter(request_shapes) if request_shapes is not None else None
    )

    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{base_url}",
        timeout=httpx.Timeout(120.0, connect=10.0, read=120.0, write=30.0, pool=30.0),
    ) as client:

        def _next_shape() -> str:
            if shape_iterator is None:
                return (
                    "stream" if rng.random() < profile.streaming_ratio else "nonstream"
                )
            try:
                return next(shape_iterator)
            except StopIteration:
                return (
                    "stream" if rng.random() < profile.streaming_ratio else "nonstream"
                )

        async def _dispatch_one(idx: int) -> None:
            shape = _next_shape()
            streaming = shape == "stream"
            body: dict[str, Any] = {
                "model": "gpt-4",
                "messages": [
                    {"role": "user", "content": f"soak-{generation_counter[0]}-{idx}"}
                ],
                "stream": streaming,
            }
            t0 = time.monotonic()
            try:
                resp = await client.post(
                    "/v1/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                elapsed_ms = (time.monotonic() - t0) * 1000
                http_ok = resp.status_code < 400
                stream_consumed_ok = False
                if streaming and resp.status_code == 200:
                    chunks = 0
                    try:
                        async for _ in resp.aiter_bytes():
                            chunks += 1
                        stream_consumed_ok = True
                    except _EXPECTED_STREAM_EXC:
                        stream_consumed_ok = False
                    except Exception:
                        stream_consumed_ok = False
                    chunk_counts.append(chunks)

                success = http_ok and (not streaming or stream_consumed_ok)
                with _thread_lock:
                    window.request_count += 1
                    window.dispatch_latencies_ms.append(elapsed_ms)
                    if success:
                        window.success_count += 1
                        if streaming:
                            window.stream_success_count += 1
                        else:
                            window.nonstream_success_count += 1
                    else:
                        window.error_count += 1
            except _EXPECTED_STREAM_EXC:
                with _thread_lock:
                    window.request_count += 1
                    window.error_count += 1
            except Exception:
                with _thread_lock:
                    window.request_count += 1
                    window.error_count += 1
            finally:
                sem.release()

        while time.monotonic() < deadline and not cancelled_flag.is_set():
            tasks: list[asyncio.Task[None]] = []
            burst_count = min(
                profile.requests_per_burst,
                max(1, int(rng.expovariate(1.0 / profile.requests_per_burst))),
            )
            for i in range(burst_count):
                if time.monotonic() >= deadline or cancelled_flag.is_set():
                    break
                await sem.acquire()
                tasks.append(asyncio.create_task(_dispatch_one(i)))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            wait = profile.burst_interval_s * rng.uniform(0.8, 1.2)
            remaining = deadline - time.monotonic()
            if remaining > 0 and not cancelled_flag.is_set():
                await asyncio.sleep(min(wait, remaining))

    if chunk_counts:
        logger.info(
            "Load generation complete: %d chunks across %d streams",
            sum(chunk_counts),
            len(chunk_counts),
        )


# ---------------------------------------------------------------------------
# Dashboard / runtime metrics poller
# ---------------------------------------------------------------------------


async def collect_runtime_snapshot(
    *,
    client: Any,
    api_key: str,
    upstream_state: MockUpstreamState,
    db_path: str,
    eggpool_pid: int,
    start_time: float,
    polling_stats: PollingStats,
    include_summary: bool = True,
) -> MetricsSnapshot:
    """Collect one runtime snapshot from EggPool.

    Used by both the periodic poller and the bounded quiescence poll.
    Nullable fields are reported as ``None`` when the runtime endpoint
    returns no value or the call fails. This helper never synthesises
    zero on parse or fetch failure.
    """
    us = upstream_state.snapshot()

    if include_summary:
        try:
            r = await client.get(
                "/api/stats/summary",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if r.status_code == 200:
                polling_stats.summary_successes += 1
            else:
                polling_stats.summary_failures += 1
                polling_stats.last_error = f"summary HTTP {r.status_code}"
        except Exception as exc:
            polling_stats.summary_failures += 1
            polling_stats.last_error = str(exc)[:200]

    pending: int | None = None
    active_resv: int | None = None
    db_lock_p95: float | None = None
    db_lock_max: float | None = None
    db_lock_count: int | None = None

    try:
        r = await client.get(
            "/api/stats/runtime",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if r.status_code == 200:
            rt = r.json()
            contention = rt.get("contention", {})
            if contention:
                db_lock_p95 = contention.get("lock_wait_p95_ms")
                db_lock_max = contention.get("lock_wait_max_ms")
                db_lock_count = contention.get("lock_wait_sample_count")
            routing_runtime = rt.get("routing_runtime", {})
            pending = routing_runtime.get("pending_count")
            active_resv = routing_runtime.get("active_reservations_count")
            polling_stats.runtime_successes += 1
        else:
            polling_stats.runtime_failures += 1
            polling_stats.last_error = f"runtime HTTP {r.status_code}"
    except Exception as exc:
        polling_stats.runtime_failures += 1
        polling_stats.last_error = str(exc)[:200]

    db_size = 0
    if os.path.exists(db_path):
        db_size = os.path.getsize(db_path)

    rss = read_process_rss_bytes(eggpool_pid)

    return MetricsSnapshot(
        timestamp=time.time(),
        elapsed_s=time.monotonic() - start_time,
        upstream_requests=us["request_count"],
        upstream_errors=us["error_count"],
        upstream_avg_latency_ms=us["avg_latency_ms"],
        pending_requests=pending,
        active_reservations=active_resv,
        db_size_bytes=db_size,
        rss_bytes=rss,
        db_lock_wait_p95_ms=db_lock_p95,
        db_lock_wait_max_ms=db_lock_max,
        db_lock_wait_sample_count=db_lock_count,
    )


async def _poll_dashboard(
    *,
    base_url: str,
    api_key: str,
    poll_interval_s: float,
    deadline: float,
    upstream_state: MockUpstreamState,
    db_path: str,
    eggpool_pid: int,
    metrics: deque[MetricsSnapshot],
    cancelled_flag: asyncio.Event,
    start_time: float,
    polling_stats: PollingStats,
) -> None:
    """Poll dashboard endpoints and collect metrics at a fixed cadence."""
    import httpx

    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{base_url}",
        timeout=httpx.Timeout(30.0),
    ) as client:
        while time.monotonic() < deadline and not cancelled_flag.is_set():
            snap = await collect_runtime_snapshot(
                client=client,
                api_key=api_key,
                upstream_state=upstream_state,
                db_path=db_path,
                eggpool_pid=eggpool_pid,
                start_time=start_time,
                polling_stats=polling_stats,
                include_summary=True,
            )
            metrics.append(snap)

            remaining = deadline - time.monotonic()
            if remaining > 0 and not cancelled_flag.is_set():
                await asyncio.sleep(min(poll_interval_s, remaining))


async def wait_for_runtime_quiescence(
    *,
    base_url: str,
    api_key: str,
    upstream_state: MockUpstreamState,
    db_path: str,
    eggpool_pid: int,
    start_time: float,
    polling_stats: PollingStats,
    timeout_s: float,
    poll_interval_s: float,
    max_pending: int,
    max_active: int,
) -> QuiescenceResult:
    """Poll runtime state after late load stops, until drained or timeout.

    Returns a :class:`QuiescenceResult` describing the post-load
    observation. Missing or unavailable runtime data fails closed — the
    returned ``drained`` value is ``False`` and ``failure_reason``
    captures the cause.
    """
    import httpx

    deadline = time.monotonic() + timeout_s
    attempts = 0
    first_attempt = True
    failure_reason: str | None = None
    final_snap: MetricsSnapshot | None = None
    pending_observed: int | None = None
    active_observed: int | None = None
    started = time.monotonic()

    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{base_url}",
        timeout=httpx.Timeout(30.0),
    ) as client:
        while True:
            attempts += 1
            snap = await collect_runtime_snapshot(
                client=client,
                api_key=api_key,
                upstream_state=upstream_state,
                db_path=db_path,
                eggpool_pid=eggpool_pid,
                start_time=start_time,
                polling_stats=polling_stats,
                include_summary=first_attempt,
            )
            first_attempt = False
            final_snap = snap

            if snap.pending_requests is None or snap.active_reservations is None:
                failure_reason = "drain metrics unavailable"
            else:
                pending_observed = snap.pending_requests
                active_observed = snap.active_reservations
                if (
                    snap.pending_requests <= max_pending
                    and snap.active_reservations <= max_active
                ):
                    return QuiescenceResult(
                        snapshot=snap,
                        drained=True,
                        attempts=attempts,
                        elapsed_s=time.monotonic() - started,
                        failure_reason=None,
                        pending_requests=snap.pending_requests,
                        active_reservations=snap.active_reservations,
                    )

            if time.monotonic() >= deadline:
                if failure_reason is None:
                    failure_reason = (
                        f"drain timeout after {attempts} attempts: "
                        f"pending={pending_observed} active={active_observed}"
                    )
                break

            remaining = deadline - time.monotonic()
            await asyncio.sleep(min(poll_interval_s, remaining))

    return QuiescenceResult(
        snapshot=final_snap,
        drained=False,
        attempts=attempts,
        elapsed_s=time.monotonic() - started,
        failure_reason=failure_reason,
        pending_requests=pending_observed,
        active_reservations=active_observed,
    )


# ---------------------------------------------------------------------------
# EggPool process management
# ---------------------------------------------------------------------------


async def _start_eggpool(
    config_path: str,
    log_path: str,
    env: dict[str, str],
) -> subprocess.Popen[str]:  # type: ignore[type-arg]  # pyright: ignore[reportReturnType]
    """Start EggPool as a subprocess."""
    argv = [
        sys.executable,
        "-m",
        "eggpool",
        "--config",
        config_path,
        "serve",
        "--verbose",
    ]
    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    try:
        proc = subprocess.Popen(  # noqa: S602,S603
            argv,
            stdout=log_file,
            stderr=log_file,
            env=env,
            start_new_session=True,
            text=True,
        )
    finally:
        log_file.close()
    return proc


async def _wait_healthy(port: int, *, timeout: float = 30.0) -> bool:
    import httpx

    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(f"http://127.0.0.1:{port}/v1/healthz", timeout=2.0)
                if r.status_code == 200:
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            await asyncio.sleep(0.3)
    return False


def _terminate_eggpool(proc: subprocess.Popen[str], *, timeout: float = 10.0) -> None:  # type: ignore[type-arg]
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5.0)


# ---------------------------------------------------------------------------
# SQLite offline audit
# ---------------------------------------------------------------------------


def _sqlite_offline_audit(db_path: str) -> dict[str, Any]:
    """Read-only audit of the SQLite file for lifecycle invariants."""
    result: dict[str, Any] = {"passed": True, "violations": []}
    if not os.path.exists(db_path):
        result["passed"] = False
        result["violations"].append("database file does not exist")
        return result

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) AS c FROM requests WHERE status = 'pending'")
        pending = cur.fetchone()["c"]
        if pending > 0:
            result["passed"] = False
            result["violations"].append(f"{pending} pending requests remain")
        result["pending_requests"] = pending

        cur.execute(
            "SELECT COUNT(*) AS c FROM reservations WHERE status = 'active' "
            "AND expires_at > unixepoch('now')"
        )
        active = cur.fetchone()["c"]
        if active > 0:
            result["passed"] = False
            result["violations"].append(f"{active} active reservations remain")
        result["active_reservations"] = active

        cur.execute("SELECT COUNT(*) AS c FROM requests")
        result["total_requests"] = cur.fetchone()["c"]

        cur.execute("SELECT COUNT(*) AS c FROM request_attempts")
        result["total_attempts"] = cur.fetchone()["c"]

        conn.close()
    except sqlite3.DatabaseError as e:
        result["passed"] = False
        result["violations"].append(f"sqlite error: {e}")

    return result


# ---------------------------------------------------------------------------
# Public run-configuration dataclass and orchestration entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationRunConfig:
    """Parsed configuration for one runtime validation run."""

    profile_name: str
    duration_seconds: int
    output_path: Path
    seed: int


def build_run_config(args: argparse.Namespace) -> ValidationRunConfig:
    return ValidationRunConfig(
        profile_name=args.profile,
        duration_seconds=args.duration_seconds,
        output_path=Path(args.output),
        seed=args.seed,
    )


@dataclass
class ValidationResult:
    """Final result of a runtime validation run."""

    passed: bool
    failure_reasons: list[str]
    output_path: Path
    duration_s: float
    return_code: int


async def run_validation(
    config: ValidationRunConfig,
    *,
    duration_plan: DurationPlan | None = None,
    health_timeout_s: float = 45.0,
    quiescence_timeout_s: float | None = None,
    request_shapes: Iterable[str] | None = None,
) -> ValidationResult:
    """Execute one runtime validation run.

    The CLI entry point builds ``duration_plan`` from ``--duration-seconds``
    and uses production defaults. Tests pass narrow test-only
    dependencies (``duration_plan``, ``quiescence_timeout_s``,
    ``request_shapes``) without expanding the public CLI.
    """
    profile_name = config.profile_name
    seed = config.seed
    duration_seconds = config.duration_seconds
    output_path = config.output_path

    if profile_name not in PROFILES:
        print(f"Unknown profile: {profile_name}", file=sys.stderr)
        print(f"Available: {', '.join(sorted(PROFILES))}", file=sys.stderr)
        return ValidationResult(
            passed=False,
            failure_reasons=[f"unknown profile: {profile_name}"],
            output_path=output_path,
            duration_s=0.0,
            return_code=1,
        )

    profile = PROFILES[profile_name]
    plan = (
        duration_plan
        if duration_plan is not None
        else build_duration_plan(float(duration_seconds))
    )
    rng = random.Random(seed)

    if quiescence_timeout_s is None:
        quiescence_timeout_s = 15.0 if profile_name == "sbc-reference" else 10.0

    environment = _collect_environment()
    logger.info("Environment: %s", json.dumps(_redact_dict(environment), indent=2))
    logger.info(
        "Duration plan: warmup=%.1fs early=%.1fs drain=%.1fs late=%.1fs",
        plan.warmup_s,
        plan.early_window_s,
        plan.drain_s,
        plan.late_window_s,
    )

    work_dir = Path(tempfile.mkdtemp(prefix="eggpool-soak-"))
    logger.info("Working directory: %s", work_dir)

    process_log_path = work_dir / "process.log"
    db_path = str(work_dir / "eggpool.db")
    config_path = str(work_dir / "config.toml")

    api_key = f"soak-key-{seed % 10000:04d}"
    server_port = _free_port()

    mock_state = MockUpstreamState(
        chunks_per_stream=profile.chunks_per_stream,
        chunk_delay_s=profile.chunk_delay_s,
        error_rate=profile.error_rate,
    )
    upstream_server = _start_mock_upstream(mock_state)
    upstream_port = upstream_server.server_address[1]
    logger.info("Mock upstream on port %d", upstream_port)

    _write_soak_config(
        Path(config_path),
        server_port=server_port,
        upstream_port=upstream_port,
        db_path=db_path,
        api_key=api_key,
        server_threads=profile.server_threads,
        db_worker_threads=profile.db_worker_threads,
        dispatch_batch_size=profile.dispatch_batch_size,
        dispatch_batch_wait_ms=profile.dispatch_batch_wait_ms,
        maintenance_batch_size=profile.maintenance_batch_size,
        maintenance_budget_ms=profile.maintenance_budget_ms,
        metrics_write_mode=profile.metrics_write_mode,
        metrics_flush_interval_s=profile.metrics_flush_interval_s,
        routing_trace_mode=profile.routing_trace_mode,
        routing_trace_sample_rate=profile.routing_trace_sample_rate,
    )

    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    env["TZ"] = "UTC"

    logger.info("Starting EggPool on port %d...", server_port)
    proc = await _start_eggpool(config_path, str(process_log_path), env)

    try:
        healthy = await _wait_healthy(server_port, timeout=health_timeout_s)
        if not healthy:
            logger.error("EggPool did not become healthy within timeout")
            _write_atomic_json(
                output_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "script_version": SCRIPT_VERSION,
                    "passed": False,
                    "failure_reasons": [
                        "EggPool did not become healthy within timeout"
                    ],
                    "git_sha": environment.get("git_sha", "unknown"),
                    "profile": profile_name,
                    "seed": seed,
                    "requested_duration_seconds": duration_seconds,
                    "platform": {
                        "system": environment.get("system", "unknown"),
                        "arch": environment.get("arch", "unknown"),
                        "python": environment.get("python", "unknown"),
                    },
                },
            )
            return ValidationResult(
                passed=False,
                failure_reasons=["EggPool did not become healthy within timeout"],
                output_path=output_path,
                duration_s=0.0,
                return_code=10,
            )
        logger.info("EggPool is healthy (PID %d)", proc.pid)

        start_time = time.monotonic()
        metrics: deque[MetricsSnapshot] = deque(maxlen=10000)
        cancelled_flag = asyncio.Event()
        polling_stats = PollingStats()
        gen_counter = [0]

        # --- Warm-up phase ---
        logger.info("Warm-up phase: %.0fs", plan.warmup_s)
        warmup_deadline = start_time + plan.warmup_s
        await _generate_load(
            base_url=str(server_port),
            api_key=api_key,
            profile=profile,
            deadline=warmup_deadline,
            rng=rng,
            metrics=metrics,
            window=WindowMetrics(name="warmup", start_time=start_time),
            cancelled_flag=cancelled_flag,
            generation_counter=gen_counter,
            request_shapes=request_shapes,
        )

        # --- Early window ---
        logger.info("Early measurement window: %.0fs", plan.early_window_s)
        early_start = time.monotonic()
        early_window = WindowMetrics(name="early", start_time=early_start)

        early_load = _generate_load(
            base_url=str(server_port),
            api_key=api_key,
            profile=profile,
            deadline=early_start + plan.early_window_s,
            rng=rng,
            metrics=metrics,
            window=early_window,
            cancelled_flag=cancelled_flag,
            generation_counter=gen_counter,
            request_shapes=request_shapes,
        )
        early_poll = _poll_dashboard(
            base_url=str(server_port),
            api_key=api_key,
            poll_interval_s=plan.poll_interval_s,
            deadline=early_start + plan.early_window_s,
            upstream_state=mock_state,
            db_path=db_path,
            eggpool_pid=proc.pid,
            metrics=metrics,
            cancelled_flag=cancelled_flag,
            start_time=start_time,
            polling_stats=polling_stats,
        )
        await asyncio.gather(early_load, early_poll)
        early_window.end_time = time.monotonic()
        logger.info(
            "Early window complete: %d requests, %.2f req/s",
            early_window.request_count,
            early_window.throughput_rps,
        )

        logger.info("Drain period: %.0fs", plan.drain_s)
        drain_deadline = time.monotonic() + plan.drain_s
        while time.monotonic() < drain_deadline and not cancelled_flag.is_set():
            await asyncio.sleep(1.0)

        # --- Late window ---
        logger.info("Late measurement window: %.0fs", plan.late_window_s)
        late_start = time.monotonic()
        late_window = WindowMetrics(name="late", start_time=late_start)

        late_load = _generate_load(
            base_url=str(server_port),
            api_key=api_key,
            profile=profile,
            deadline=late_start + plan.late_window_s,
            rng=rng,
            metrics=metrics,
            window=late_window,
            cancelled_flag=cancelled_flag,
            generation_counter=gen_counter,
            request_shapes=request_shapes,
        )
        late_poll = _poll_dashboard(
            base_url=str(server_port),
            api_key=api_key,
            poll_interval_s=plan.poll_interval_s,
            deadline=late_start + plan.late_window_s,
            upstream_state=mock_state,
            db_path=db_path,
            eggpool_pid=proc.pid,
            metrics=metrics,
            cancelled_flag=cancelled_flag,
            start_time=start_time,
            polling_stats=polling_stats,
        )
        await asyncio.gather(late_load, late_poll)
        late_window.end_time = time.monotonic()
        logger.info(
            "Late window complete: %d requests, %.2f req/s",
            late_window.request_count,
            late_window.throughput_rps,
        )

        measurement_duration_s = time.monotonic() - start_time

        # --- Bounded post-load quiescence ---
        quiescence = await wait_for_runtime_quiescence(
            base_url=str(server_port),
            api_key=api_key,
            upstream_state=mock_state,
            db_path=db_path,
            eggpool_pid=proc.pid,
            start_time=start_time,
            polling_stats=polling_stats,
            timeout_s=quiescence_timeout_s,
            poll_interval_s=1.0,
            max_pending=plan.max_pending_requests,
            max_active=plan.max_active_reservations,
        )

        # --- Collect process RSS snapshot ---
        rss_end = read_process_rss_bytes(proc.pid)

        # --- Gate evaluation ---
        failure_reasons: list[str] = []
        gates: dict[str, Any] = {}

        # Workload gate — must run before ratio / throughput evaluation
        # so we never compute gates from empty samples.
        require_both_shapes = profile_name == "sbc-reference" and duration_seconds >= 60
        workload = evaluate_workload_gate(
            early_window,
            late_window,
            expected_error_rate=profile.error_rate,
            require_stream_and_nonstream=require_both_shapes,
        )
        gates["workload"] = {
            "passed": workload.passed,
            "failure_reasons": list(workload.failure_reasons),
            "early": {
                "attempts": workload.early_attempts,
                "successes": workload.early_successes,
                "errors": workload.early_errors,
                "observed_error_rate": round(workload.early_error_rate, 4),
            },
            "late": {
                "attempts": workload.late_attempts,
                "successes": workload.late_successes,
                "errors": workload.late_errors,
                "observed_error_rate": round(workload.late_error_rate, 4),
            },
            "expected_error_rate": workload.expected_error_rate,
            "allowed_error_fraction": round(workload.allowed_error_fraction, 4),
        }
        if not workload.passed:
            failure_reasons.extend(workload.failure_reasons)

        # Throughput decline — requires useful work in both windows
        if workload.passed and early_window.throughput_rps > 0:
            throughput_ratio = late_window.throughput_rps / early_window.throughput_rps
            throughput_pass = throughput_ratio >= (1.0 - plan.throughput_decline_limit)
            gates["throughput"] = {
                "early_rps": round(early_window.throughput_rps, 3),
                "late_rps": round(late_window.throughput_rps, 3),
                "ratio": round(throughput_ratio, 4),
                "decline_limit": plan.throughput_decline_limit,
                "passed": throughput_pass,
                "failure_reason": (
                    None
                    if throughput_pass
                    else (
                        f"throughput ratio {throughput_ratio:.4f} "
                        f"below limit {1.0 - plan.throughput_decline_limit:.4f}"
                    )
                ),
            }
            if not throughput_pass:
                failure_reasons.append(str(gates["throughput"]["failure_reason"]))
        else:
            gates["throughput"] = {
                "early_rps": round(early_window.throughput_rps, 3),
                "late_rps": round(late_window.throughput_rps, 3),
                "ratio": None,
                "decline_limit": plan.throughput_decline_limit,
                "passed": False,
                "failure_reason": ("no early/late throughput samples to compare"),
            }
            failure_reasons.append(str(gates["throughput"]["failure_reason"]))

        # Dispatch latency ratio — direct caps
        early_p95 = early_window.percentile(0.95)
        late_p95 = late_window.percentile(0.95)
        p95 = evaluate_ratio_gate(
            early_p95 if workload.passed else None,
            late_p95 if workload.passed else None,
            limit=plan.dispatch_p95_ratio_limit,
            label="dispatch_p95",
        )
        gates["dispatch_p95"] = {
            "early_ms": round(p95.early_ms, 2) if p95.early_ms is not None else None,
            "late_ms": round(p95.late_ms, 2) if p95.late_ms is not None else None,
            "ratio": round(p95.ratio, 4) if p95.ratio is not None else None,
            "ratio_limit": p95.limit,
            "passed": p95.passed,
            "failure_reason": p95.failure_reason,
        }
        if not p95.passed:
            failure_reasons.append(p95.failure_reason or "dispatch_p95 failed")

        early_p99 = early_window.percentile(0.99)
        late_p99 = late_window.percentile(0.99)
        p99 = evaluate_ratio_gate(
            early_p99 if workload.passed else None,
            late_p99 if workload.passed else None,
            limit=plan.dispatch_p99_ratio_limit,
            label="dispatch_p99",
        )
        gates["dispatch_p99"] = {
            "early_ms": round(p99.early_ms, 2) if p99.early_ms is not None else None,
            "late_ms": round(p99.late_ms, 2) if p99.late_ms is not None else None,
            "ratio": round(p99.ratio, 4) if p99.ratio is not None else None,
            "ratio_limit": p99.limit,
            "passed": p99.passed,
            "failure_reason": p99.failure_reason,
        }
        if not p99.passed:
            failure_reasons.append(p99.failure_reason or "dispatch_p99 failed")

        # Drain gate — bounded post-load quiescence observation
        gates["quiescence"] = {
            "drained": quiescence.drained,
            "attempts": quiescence.attempts,
            "elapsed_seconds": round(quiescence.elapsed_s, 3),
            "pending_requests": quiescence.pending_requests,
            "active_reservations": quiescence.active_reservations,
            "failure_reason": quiescence.failure_reason,
            "passed": quiescence.drained,
        }
        gates["drain_pass"] = quiescence.drained
        if not quiescence.drained:
            failure_reasons.append(
                quiescence.failure_reason or "runtime drain observation failed"
            )

        rss_pass = True
        if profile.rss_required and rss_end is None:
            rss_pass = False
            failure_reasons.append("child RSS unavailable")
        gates["rss"] = {
            "available": rss_end is not None,
            "required": profile.rss_required,
            "passed": rss_pass,
        }

        audit = _sqlite_offline_audit(db_path)
        gates["database_audit"] = {
            "passed": audit["passed"],
            "violations": audit.get("violations", []),
        }
        if not audit["passed"]:
            failure_reasons.append(f"sqlite audit: {audit.get('violations', [])}")

        all_passed = all(
            v.get("passed", True) is True
            for v in (
                gates["workload"],
                gates["throughput"],
                gates["dispatch_p95"],
                gates["dispatch_p99"],
                gates["quiescence"],
                gates["rss"],
                gates["database_audit"],
            )
        )
        gates["all_passed"] = all_passed

        # --- Collect early/late RSS samples ---
        early_rss_samples = [
            m.rss_bytes
            for m in metrics
            if m.rss_bytes is not None
            and m.elapsed_s < plan.warmup_s + plan.early_window_s
        ]
        late_rss_samples = [
            m.rss_bytes
            for m in metrics
            if m.rss_bytes is not None
            and m.elapsed_s >= plan.warmup_s + plan.early_window_s + plan.drain_s
        ]
        rss_peak = max(
            early_rss_samples + late_rss_samples,
            default=None,
        )

        # --- Build and write one atomic JSON file ---
        result_data = {
            "schema_version": SCHEMA_VERSION,
            "script_version": SCRIPT_VERSION,
            "passed": all_passed,
            "failure_reasons": failure_reasons,
            "git_sha": environment.get("git_sha", "unknown"),
            "profile": profile_name,
            "seed": seed,
            "requested_duration_seconds": duration_seconds,
            "measurement_duration_seconds": round(measurement_duration_s, 2),
            "quiescence_duration_seconds": round(quiescence.elapsed_s, 3),
            "platform": {
                "system": environment.get("system", "unknown"),
                "arch": environment.get("arch", "unknown"),
                "python": environment.get("python", "unknown"),
            },
            "process": {
                "eggpool_pid": proc.pid,
                "rss_start_bytes": early_rss_samples[0] if early_rss_samples else None,
                "rss_end_bytes": rss_end,
                "rss_peak_bytes": rss_peak,
            },
            "early": early_window.to_dict(),
            "late": late_window.to_dict(),
            "gates": gates,
            "database_audit": audit,
            "polling": polling_stats.to_dict(),
        }

        _write_atomic_json(output_path, result_data)

        # --- Print concise terminal summary ---
        print("\n" + "=" * 72)
        print("DISPATCH STABILITY SOAK COMPLETE")
        print("=" * 72)
        print(f"Profile: {profile_name}")
        print(f"Seed:    {seed}")
        print(
            f"Duration: {measurement_duration_s:.0f}s (requested {duration_seconds}s)"
        )
        print(f"Git SHA: {environment.get('git_sha', 'unknown')}")
        print()
        print("Early Window:")
        print(f"  Requests: {early_window.request_count}")
        print(f"  Successes: {early_window.success_count}")
        print(f"  Throughput: {early_window.throughput_rps:.2f} req/s")
        print(f"  Dispatch p95: {early_p95:.1f}ms  p99: {early_p99:.1f}ms")
        print(f"  Errors: {early_window.error_count}")
        print()
        print("Late Window:")
        print(f"  Requests: {late_window.request_count}")
        print(f"  Successes: {late_window.success_count}")
        print(f"  Throughput: {late_window.throughput_rps:.2f} req/s")
        print(f"  Dispatch p95: {late_p95:.1f}ms  p99: {late_p99:.1f}ms")
        print(f"  Errors: {late_window.error_count}")
        print()
        if rss_end is not None:
            print(f"RSS (child): {rss_end:,} bytes")
        else:
            print("RSS (child): unavailable")
        print()
        print("Gates:")
        for key in [
            "workload",
            "throughput",
            "dispatch_p95",
            "dispatch_p99",
            "quiescence",
            "rss",
            "database_audit",
            "all_passed",
        ]:
            value: Any = gates.get(key, "N/A")
            if key == "all_passed":
                mark = "PASS" if value is True else "FAIL"
                print(f"  {key}: {mark}")
            else:
                passed_value = _gate_passed(value)
                mark = "PASS" if passed_value is True else "FAIL"
                print(f"  {key}: {mark}")
        if failure_reasons:
            print()
            print("Failure reasons:")
            for reason in failure_reasons:
                print(f"  - {reason}")
        print()
        print(f"Output: {output_path}")
        print("=" * 72)

        return_code = 0 if all_passed else 1
        return ValidationResult(
            passed=all_passed,
            failure_reasons=failure_reasons,
            output_path=output_path,
            duration_s=measurement_duration_s + quiescence.elapsed_s,
            return_code=return_code,
        )

    finally:
        logger.info("Terminating EggPool (PID %d)...", proc.pid)
        _terminate_eggpool(proc)
        upstream_server.shutdown()
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profile",
        choices=list(PROFILES),
        default="balanced-file-backed",
        help="Soak test profile (default: balanced-file-backed)",
    )
    parser.add_argument(
        "--duration-seconds",
        type=_validate_duration,
        default=300,
        help="Total validation duration in seconds (default: 300, minimum: 30)",
    )
    parser.add_argument(
        "--output",
        default="/tmp/eggpool-runtime-validation.json",
        help="Output JSON file path (default: /tmp/eggpool-runtime-validation.json)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic random seed (default: 42)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    result = asyncio.run(run_validation(build_run_config(args)))
    return result.return_code


if __name__ == "__main__":
    raise SystemExit(main())
