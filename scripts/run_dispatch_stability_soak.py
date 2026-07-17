"""Dispatch stability extended-soak runner.

Runs a process-level, file-backed SQLite soak test of EggPool's dispatch
stability.  Starts EggPool as a real ``eggpool serve --verbose`` subprocess
with a deterministic local mock upstream, exercises dashboard/runtime metrics
polling, and produces structured artifacts for release evidence.

Usage::

    uv run python scripts/run_dispatch_stability_soak.py \
        --profile balanced-file-backed \
        --mode nightly \
        --output artifacts/dispatch-soak/nightly

The runner never persists request content, provider secrets, or credential
values.  All artifact paths, environment snapshots, and config dumps are
redacted before writing.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import platform
import random
import resource
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
from typing import Any

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("dispatch_soak")

# ---------------------------------------------------------------------------
# Version / schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
SCRIPT_VERSION = "1.0.0"

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


def _memory_total_bytes() -> int:
    """Return total system memory in bytes (stdlib-only)."""
    if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        return page_size * page_count  # type: ignore[return-value]
    # macOS fallback via ctypes
    try:
        import ctypes

        buf = ctypes.create_string_buffer(8)
        ctypes.CDLL("libc.so.1").sysctlbyname(
            b"hw.memsize", buf, ctypes.pointer(ctypes.c_size_t(8)), None, 0
        )
        return struct.unpack("Q", buf)[0]
    except Exception:
        return 0


def _collect_environment() -> dict[str, Any]:
    return {
        "git_sha": _git_sha(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "arch": platform.machine(),
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": _memory_total_bytes(),
        "pid": os.getpid(),
    }


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

        # Streaming
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
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
    ),
}

# ---------------------------------------------------------------------------
# Duration modes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DurationMode:
    name: str
    description: str
    warmup_s: float
    early_window_s: float
    late_window_s: float
    total_s: float
    poll_interval_s: float
    # Gate thresholds
    dispatch_p95_ratio_limit: float
    dispatch_p99_ratio_limit: float
    throughput_decline_limit: float
    max_pending_requests: int
    max_active_reservations: int


DURATION_MODES: dict[str, DurationMode] = {
    "smoke": DurationMode(
        name="smoke",
        description="2-5 minutes, developer-only harness verification",
        warmup_s=30.0,
        early_window_s=60.0,
        late_window_s=60.0,
        total_s=180.0,
        poll_interval_s=5.0,
        dispatch_p95_ratio_limit=1.50,
        dispatch_p99_ratio_limit=2.00,
        throughput_decline_limit=0.20,
        max_pending_requests=0,
        max_active_reservations=0,
    ),
    "ci": DurationMode(
        name="ci",
        description="10-30 minutes, bounded correctness and drain validation",
        warmup_s=60.0,
        early_window_s=300.0,
        late_window_s=300.0,
        total_s=900.0,
        poll_interval_s=5.0,
        dispatch_p95_ratio_limit=1.30,
        dispatch_p99_ratio_limit=1.80,
        throughput_decline_limit=0.15,
        max_pending_requests=0,
        max_active_reservations=0,
    ),
    "nightly": DurationMode(
        name="nightly",
        description="1-3 hours, file-backed early/late comparison",
        warmup_s=300.0,
        early_window_s=1800.0,
        late_window_s=1800.0,
        total_s=7200.0,
        poll_interval_s=10.0,
        dispatch_p95_ratio_limit=1.20,
        dispatch_p99_ratio_limit=1.50,
        throughput_decline_limit=0.10,
        max_pending_requests=0,
        max_active_reservations=0,
    ),
    "reference": DurationMode(
        name="reference",
        description="6-24 hours, release evidence on representative hardware",
        warmup_s=600.0,
        early_window_s=7200.0,
        late_window_s=7200.0,
        total_s=36000.0,
        poll_interval_s=15.0,
        dispatch_p95_ratio_limit=1.20,
        dispatch_p99_ratio_limit=1.50,
        throughput_decline_limit=0.10,
        max_pending_requests=0,
        max_active_reservations=0,
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
    error_count: int = 0
    dispatch_latencies_ms: list[float] = field(  # pyright: ignore[reportUnknownVariableType]
        default_factory=list
    )
    pending_at_end: int = 0
    active_reservations_at_end: int = 0
    upstream_requests: int = 0
    upstream_errors: int = 0
    db_size_bytes: int = 0
    rss_bytes: int = 0

    @property
    def duration_s(self) -> float:
        return self.end_time - self.start_time

    @property
    def throughput_rps(self) -> float:
        return self.request_count / self.duration_s if self.duration_s > 0 else 0.0

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
            "error_count": self.error_count,
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
    pending_requests: int
    active_reservations: int
    db_size_bytes: int
    rss_bytes: int
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


# ---------------------------------------------------------------------------
# Load generator
# ---------------------------------------------------------------------------


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
) -> None:
    """Generate load against the running EggPool server until deadline."""
    import httpx

    sem = asyncio.Semaphore(profile.concurrency)
    chunk_counts: list[int] = []

    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{base_url}",
        timeout=httpx.Timeout(120.0, connect=10.0, read=120.0, write=30.0, pool=30.0),
    ) as client:

        async def _dispatch_one(idx: int) -> None:
            streaming = rng.random() < profile.streaming_ratio
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
                with _thread_lock:
                    window.request_count += 1
                    window.dispatch_latencies_ms.append(elapsed_ms)
                    if resp.status_code >= 400:
                        window.error_count += 1

                # Consume streaming response fully (may timeout on connection close)
                if streaming and resp.status_code == 200:
                    chunks = 0
                    try:
                        async for _ in resp.aiter_bytes():
                            chunks += 1
                    except Exception:
                        pass  # ReadTimeout from mock upstream connection close
                    chunk_counts.append(chunks)
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

            # Inter-burst interval
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
# Thread-safe lock for metrics accumulation
# ---------------------------------------------------------------------------

_thread_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Dashboard / runtime metrics poller
# ---------------------------------------------------------------------------


async def _poll_dashboard(
    *,
    base_url: str,
    api_key: str,
    poll_interval_s: float,
    deadline: float,
    upstream_state: MockUpstreamState,
    db_path: str,
    metrics: deque[MetricsSnapshot],
    cancelled_flag: asyncio.Event,
    start_time: float,
) -> None:
    """Poll dashboard endpoints and collect metrics at a fixed cadence."""
    import httpx

    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{base_url}",
        timeout=httpx.Timeout(30.0),
    ) as client:
        while time.monotonic() < deadline and not cancelled_flag.is_set():
            us = upstream_state.snapshot()

            # Query EggPool stats
            pending = 0
            active_resv = 0
            db_lock_p95 = None
            db_lock_max = None
            db_lock_count = None
            try:
                r = await client.get(
                    "/api/stats/summary",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    pending = data.get("pending_requests", 0)
                    active_resv = data.get("active_reservations", 0)
            except Exception:
                pass

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
            except Exception:
                pass

            # DB file size
            db_size = 0
            if os.path.exists(db_path):
                db_size = os.path.getsize(db_path)

            # RSS
            usage = resource.getrusage(resource.RUSAGE_SELF)
            rss = (
                usage.ru_maxrss * 1024
                if platform.system() == "Darwin"
                else usage.ru_maxrss
            )

            snap = MetricsSnapshot(
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
            metrics.append(snap)

            remaining = deadline - time.monotonic()
            if remaining > 0 and not cancelled_flag.is_set():
                await asyncio.sleep(min(poll_interval_s, remaining))


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
# Artifact writers
# ---------------------------------------------------------------------------


def _write_metrics_jsonl(path: Path, snapshots: list[MetricsSnapshot]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for snap in snapshots:
            f.write(json.dumps(snap.to_dict()) + "\n")


def _compute_manifest(output_dir: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for p in sorted(output_dir.iterdir()):
        if p.is_file() and p.name != "manifest.json":
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            manifest[p.name] = h
    return manifest


def _write_manifest(output_dir: Path) -> None:
    manifest = _compute_manifest(output_dir)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_summary_md(
    path: Path,
    *,
    profile_name: str,
    mode_name: str,
    environment: dict[str, Any],
    early_window: WindowMetrics,
    late_window: WindowMetrics,
    gate_status: dict[str, Any],
    total_duration_s: float,
) -> None:
    lines = [
        "# Dispatch Stability Soak Summary",
        "",
        f"- **Profile**: {profile_name}",
        f"- **Mode**: {mode_name}",
        f"- **Git SHA**: {environment.get('git_sha', 'unknown')}",
        f"- **Python**: {environment.get('python', 'unknown')}",
        f"- **Platform**: {environment.get('platform', 'unknown')}",
        f"- **Duration**: {total_duration_s:.0f}s",
        "",
        "## Windows",
        "",
        "| Metric | Early | Late | Ratio | Gate |",
        "|--------|-------|------|-------|------|",
    ]

    for label, early_val, late_val, ratio_key, limit in [
        (
            "Requests",
            early_window.request_count,
            late_window.request_count,
            "throughput_decline",
            gate_status.get("throughput_decline_limit", 0),
        ),
    ]:
        ratio = late_val / early_val if early_val > 0 else 0.0
        passed = ratio >= (1.0 - limit) if ratio_key == "throughput_decline" else True
        mark = "PASS" if passed else "FAIL"
        lines.append(f"| {label} | {early_val} | {late_val} | {ratio:.3f} | {mark} |")

    for label, early_val, late_val, _ratio_key, limit in [
        (
            "Dispatch p95 (ms)",
            early_window.percentile(0.95),
            late_window.percentile(0.95),
            "dispatch_p95_ratio",
            gate_status.get("dispatch_p95_ratio_limit", 0),
        ),
        (
            "Dispatch p99 (ms)",
            early_window.percentile(0.99),
            late_window.percentile(0.99),
            "dispatch_p99_ratio",
            gate_status.get("dispatch_p99_ratio_limit", 0),
        ),
    ]:
        ratio = late_val / early_val if early_val > 0 else 0.0
        passed = ratio <= (1.0 + limit)
        mark = "PASS" if passed else "FAIL"
        lines.append(
            f"| {label} | {early_val:.1f} | {late_val:.1f} | {ratio:.3f} | {mark} |"
        )

    lines.extend(
        [
            "",
            "## Gate Status",
            "",
        ]
    )
    for k, v in gate_status.items():
        mark = "PASS" if v is True else ("FAIL" if v is False else str(v))
        lines.append(f"- **{k}**: {mark}")

    lines.extend(
        [
            "",
            "## Windows Detail",
            "",
            "### Early Window",
            "",
            f"- Duration: {early_window.duration_s:.0f}s",
            f"- Requests: {early_window.request_count}",
            f"- Errors: {early_window.error_count}",
            f"- Throughput: {early_window.throughput_rps:.2f} req/s",
            f"- Pending at end: {early_window.pending_at_end}",
            f"- Active reservations at end: {early_window.active_reservations_at_end}",
            f"- DB size: {early_window.db_size_bytes:,} bytes",
            "",
            "### Late Window",
            "",
            f"- Duration: {late_window.duration_s:.0f}s",
            f"- Requests: {late_window.request_count}",
            f"- Errors: {late_window.error_count}",
            f"- Throughput: {late_window.throughput_rps:.2f} req/s",
            f"- Pending at end: {late_window.pending_at_end}",
            f"- Active reservations at end: {late_window.active_reservations_at_end}",
            f"- DB size: {late_window.db_size_bytes:,} bytes",
            "",
        ],
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# SQLite offline audit (reads the file directly, no EggPool needed)
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

        # Check for pending requests
        cur.execute("SELECT COUNT(*) AS c FROM requests WHERE status = 'pending'")
        pending = cur.fetchone()["c"]
        if pending > 0:
            result["passed"] = False
            result["violations"].append(f"{pending} pending requests remain")
        result["pending_requests"] = pending

        # Check for active reservations
        cur.execute(
            "SELECT COUNT(*) AS c FROM reservations WHERE status = 'active' "
            "AND expires_at > unixepoch('now')"
        )
        active = cur.fetchone()["c"]
        if active > 0:
            result["passed"] = False
            result["violations"].append(f"{active} active reservations remain")
        result["active_reservations"] = active

        # Total request count
        cur.execute("SELECT COUNT(*) AS c FROM requests")
        result["total_requests"] = cur.fetchone()["c"]

        # Total attempt count
        cur.execute("SELECT COUNT(*) AS c FROM request_attempts")
        result["total_attempts"] = cur.fetchone()["c"]

        conn.close()
    except sqlite3.DatabaseError as e:
        result["passed"] = False
        result["violations"].append(f"sqlite error: {e}")

    return result


# ---------------------------------------------------------------------------
# Main soak runner
# ---------------------------------------------------------------------------


async def _run_soak(args: argparse.Namespace) -> int:
    profile_name: str = args.profile
    mode_name: str = args.mode
    output_dir = Path(args.output)
    seed: int = args.seed

    if profile_name not in PROFILES:
        print(f"Unknown profile: {profile_name}", file=sys.stderr)
        print(f"Available: {', '.join(sorted(PROFILES))}", file=sys.stderr)
        return 1
    if mode_name not in DURATION_MODES:
        print(f"Unknown mode: {mode_name}", file=sys.stderr)
        print(f"Available: {', '.join(sorted(DURATION_MODES))}", file=sys.stderr)
        return 1

    profile = PROFILES[profile_name]
    mode = DURATION_MODES[mode_name]
    rng = random.Random(seed)

    output_dir.mkdir(parents=True, exist_ok=True)
    process_log_path = output_dir / "process.log"
    metrics_jsonl_path = output_dir / "metrics.jsonl"
    summary_json_path = output_dir / "summary.json"
    summary_md_path = output_dir / "summary.md"

    environment = _collect_environment()
    logger.info("Environment: %s", json.dumps(_redact_dict(environment), indent=2))

    # Create working directory for DB and config
    work_dir = Path(tempfile.mkdtemp(prefix="eggpool-soak-"))
    logger.info("Working directory: %s", work_dir)

    db_path = str(work_dir / "eggpool.db")
    config_path = str(work_dir / "config.toml")

    api_key = f"soak-key-{seed % 10000:04d}"
    server_port = _free_port()

    # Start mock upstream
    mock_state = MockUpstreamState(
        chunks_per_stream=profile.chunks_per_stream,
        chunk_delay_s=profile.chunk_delay_s,
        error_rate=profile.error_rate,
    )
    upstream_server = _start_mock_upstream(mock_state)
    upstream_port = upstream_server.server_address[1]
    logger.info("Mock upstream on port %d", upstream_port)

    # Write config
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

    # Environment for subprocess
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    env["TZ"] = "UTC"

    # Start EggPool
    logger.info("Starting EggPool on port %d...", server_port)
    proc = await _start_eggpool(config_path, str(process_log_path), env)

    try:
        healthy = await _wait_healthy(server_port, timeout=45.0)
        if not healthy:
            logger.error("EggPool did not become healthy within timeout")
            return 10
        logger.info("EggPool is healthy (PID %d)", proc.pid)

        start_time = time.monotonic()
        metrics: deque[MetricsSnapshot] = deque(maxlen=10000)
        cancelled_flag = asyncio.Event()

        # --- Warm-up phase ---
        logger.info("Warm-up phase: %.0fs", mode.warmup_s)
        warmup_deadline = start_time + mode.warmup_s
        gen_counter = [0]
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
        )

        # --- Early window ---
        logger.info("Early measurement window: %.0fs", mode.early_window_s)
        early_start = time.monotonic()
        early_window = WindowMetrics(name="early", start_time=early_start)

        early_load = _generate_load(
            base_url=str(server_port),
            api_key=api_key,
            profile=profile,
            deadline=early_start + mode.early_window_s,
            rng=rng,
            metrics=metrics,
            window=early_window,
            cancelled_flag=cancelled_flag,
            generation_counter=gen_counter,
        )
        early_poll = _poll_dashboard(
            base_url=str(server_port),
            api_key=api_key,
            poll_interval_s=mode.poll_interval_s,
            deadline=early_start + mode.early_window_s,
            upstream_state=mock_state,
            db_path=db_path,
            metrics=metrics,
            cancelled_flag=cancelled_flag,
            start_time=start_time,
        )
        await asyncio.gather(early_load, early_poll)
        early_window.end_time = time.monotonic()
        logger.info(
            "Early window complete: %d requests, %.2f req/s",
            early_window.request_count,
            early_window.throughput_rps,
        )

        # Drain period between windows
        drain_s = min(30.0, mode.total_s * 0.01)
        logger.info("Drain period: %.0fs", drain_s)
        drain_deadline = time.monotonic() + drain_s
        while time.monotonic() < drain_deadline and not cancelled_flag.is_set():
            await asyncio.sleep(1.0)

        # --- Late window ---
        logger.info("Late measurement window: %.0fs", mode.late_window_s)
        late_start = time.monotonic()
        late_window = WindowMetrics(name="late", start_time=late_start)

        late_load = _generate_load(
            base_url=str(server_port),
            api_key=api_key,
            profile=profile,
            deadline=late_start + mode.late_window_s,
            rng=rng,
            metrics=metrics,
            window=late_window,
            cancelled_flag=cancelled_flag,
            generation_counter=gen_counter,
        )
        late_poll = _poll_dashboard(
            base_url=str(server_port),
            api_key=api_key,
            poll_interval_s=mode.poll_interval_s,
            deadline=late_start + mode.late_window_s,
            upstream_state=mock_state,
            db_path=db_path,
            metrics=metrics,
            cancelled_flag=cancelled_flag,
            start_time=start_time,
        )
        await asyncio.gather(late_load, late_poll)
        late_window.end_time = time.monotonic()
        logger.info(
            "Late window complete: %d requests, %.2f req/s",
            late_window.request_count,
            late_window.throughput_rps,
        )

        total_duration_s = time.monotonic() - start_time

        # --- Gate evaluation ---
        gate_status: dict[str, Any] = {}

        # Throughput decline
        if early_window.throughput_rps > 0:
            throughput_ratio = late_window.throughput_rps / early_window.throughput_rps
            throughput_pass = throughput_ratio >= (1.0 - mode.throughput_decline_limit)
        else:
            throughput_ratio = 0.0
            throughput_pass = early_window.request_count == 0
        gate_status["throughput_decline"] = round(throughput_ratio, 4)
        gate_status["throughput_decline_limit"] = mode.throughput_decline_limit
        gate_status["throughput_decline_pass"] = throughput_pass

        # Dispatch latency ratio
        early_p95 = early_window.percentile(0.95)
        late_p95 = late_window.percentile(0.95)
        p95_ratio = late_p95 / early_p95 if early_p95 > 0 else 0.0
        p95_pass = p95_ratio <= (1.0 + mode.dispatch_p95_ratio_limit)
        gate_status["dispatch_p95_ratio"] = round(p95_ratio, 4)
        gate_status["dispatch_p95_ratio_limit"] = mode.dispatch_p95_ratio_limit
        gate_status["dispatch_p95_pass"] = p95_pass

        early_p99 = early_window.percentile(0.99)
        late_p99 = late_window.percentile(0.99)
        p99_ratio = late_p99 / early_p99 if early_p99 > 0 else 0.0
        p99_pass = p99_ratio <= (1.0 + mode.dispatch_p99_ratio_limit)
        gate_status["dispatch_p99_ratio"] = round(p99_ratio, 4)
        gate_status["dispatch_p99_ratio_limit"] = mode.dispatch_p99_ratio_limit
        gate_status["dispatch_p99_pass"] = p99_pass

        # Queue drain (final snapshot)
        final_snap = metrics[-1] if metrics else None
        pending_final = final_snap.pending_requests if final_snap else 0
        active_final = final_snap.active_reservations if final_snap else 0
        drain_pass = (
            pending_final <= mode.max_pending_requests
            and active_final <= mode.max_active_reservations
        )
        gate_status["pending_at_end"] = pending_final
        gate_status["active_reservations_at_end"] = active_final
        gate_status["drain_pass"] = drain_pass

        # Offline SQLite audit
        audit = _sqlite_offline_audit(db_path)
        gate_status["sqlite_audit_pass"] = audit["passed"]
        gate_status["sqlite_audit_violations"] = audit.get("violations", [])

        all_passed = all(
            gate_status.get(k, True) is True
            for k in [
                "throughput_decline_pass",
                "dispatch_p95_pass",
                "dispatch_p99_pass",
                "drain_pass",
                "sqlite_audit_pass",
            ]
        )
        gate_status["all_passed"] = all_passed

        # --- Write artifacts ---
        _write_metrics_jsonl(metrics_jsonl_path, list(metrics))

        summary_data = {
            "schema_version": SCHEMA_VERSION,
            "script_version": SCRIPT_VERSION,
            "environment": _redact_dict(environment),
            "profile": dataclasses.asdict(profile),
            "mode": dataclasses.asdict(mode),
            "seed": seed,
            "config": {
                "server_port": server_port,
                "upstream_port": upstream_port,
                "db_path": db_path,
                "working_directory": str(work_dir),
            },
            "windows": {
                "early": early_window.to_dict(),
                "late": late_window.to_dict(),
            },
            "gate_status": gate_status,
            "total_duration_s": round(total_duration_s, 2),
            "metrics_samples": len(metrics),
        }
        summary_json_path.write_text(
            json.dumps(summary_data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        _write_summary_md(
            summary_md_path,
            profile_name=profile_name,
            mode_name=mode_name,
            environment=environment,
            early_window=early_window,
            late_window=late_window,
            gate_status=gate_status,
            total_duration_s=total_duration_s,
        )

        _write_manifest(output_dir)

        # --- Print summary ---
        print("\n" + "=" * 72)
        print("DISPATCH STABILITY SOAK COMPLETE")
        print("=" * 72)
        print(f"Profile: {profile_name}")
        print(f"Mode:    {mode_name}")
        print(f"Seed:    {seed}")
        print(f"Duration: {total_duration_s:.0f}s")
        print(f"Git SHA: {environment.get('git_sha', 'unknown')}")
        print()
        print("Early Window:")
        print(f"  Requests: {early_window.request_count}")
        print(f"  Throughput: {early_window.throughput_rps:.2f} req/s")
        print(f"  Dispatch p95: {early_p95:.1f}ms  p99: {early_p99:.1f}ms")
        print(f"  Errors: {early_window.error_count}")
        print()
        print("Late Window:")
        print(f"  Requests: {late_window.request_count}")
        print(f"  Throughput: {late_window.throughput_rps:.2f} req/s")
        print(f"  Dispatch p95: {late_p95:.1f}ms  p99: {late_p99:.1f}ms")
        print(f"  Errors: {late_window.error_count}")
        print()
        print("Gates:")
        for k in [
            "throughput_decline_pass",
            "dispatch_p95_pass",
            "dispatch_p99_pass",
            "drain_pass",
            "sqlite_audit_pass",
            "all_passed",
        ]:
            v = gate_status.get(k, "N/A")
            mark = "PASS" if v is True else ("FAIL" if v is False else str(v))
            print(f"  {k}: {mark}")
        print()
        print(f"Artifacts: {output_dir}")
        print("=" * 72)

        return 0 if all_passed else 1

    finally:
        # Cleanup
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
        "--mode",
        choices=list(DURATION_MODES),
        default="smoke",
        help="Duration mode (default: smoke)",
    )
    parser.add_argument(
        "--output",
        default="artifacts/dispatch-soak",
        help="Output directory for artifacts (default: artifacts/dispatch-soak)",
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

    rc = asyncio.run(_run_soak(args))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
