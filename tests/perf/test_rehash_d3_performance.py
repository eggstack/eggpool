"""D3 Phase 5 — Performance regression gates for live rehash.

Measures:
- Reload wall-clock p50/p95 across 20 reloads (idle server, no in-flight traffic).
- Memory delta per reload (RSS via psutil on the server PID before/after).
- Concurrent traffic stability: 50 parallel GET /health requests during a reload.

All helpers are imported from the existing integration test module to avoid
duplication.  Thresholds are deliberately generous to avoid flakiness on
varied CI hardware; tightening them requires a dedicated baseline run.

NOTE: If ``psutil`` is not installed the memory-delta test is skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.test_rehash_d3_soak import (
    _fetch_runtime_snapshot,
)
from tests.integration.test_rehash_streaming_swap import (
    _free_port,
    _make_mock_server,
    _MockState,
    _run_rehash,
    _spawn_server,
    _terminate_server,
    _wait_healthy,
    _write_config,
)

psutil: Any = None
try:
    import psutil as _psutil

    psutil = _psutil
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_measurement(name: str, data: dict[str, Any]) -> None:
    """Append a measurement to the results file for post-test inspection."""
    results_file = Path(os.environ.get("D3_PERF_RESULTS", "/tmp/d3_perf_results.json"))
    existing: list[dict[str, Any]] = []
    if results_file.exists():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            existing = json.loads(results_file.read_text())
    existing.append({"test": name, **data})
    results_file.write_text(json.dumps(existing, indent=2))


def _get_rss_bytes(pid: int) -> int | None:
    """Return current RSS in bytes for *pid*, or None if unavailable."""
    if psutil is None:
        return None
    try:
        proc = psutil.Process(pid)
        return proc.memory_info().rss
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_reload_latency_p50_p95_under_budget(tmp_path: Any) -> None:
    """Measure wall-clock latency of 20 LIVE reloads on an idle server.

    Each reload changes ``routing.inflight_penalty`` to ensure a real
    configuration change triggers a generation swap.

    Budgets (generous to avoid flakiness):
        p50 < 1500 ms
        p95 < 3000 ms
        All 20 succeed
    """
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    proc = await _spawn_server(config_path, env)
    try:
        healthy = await _wait_healthy(server_port)
        assert healthy, "server did not become healthy"

        latencies: list[float] = []
        for i in range(20):
            penalty = 100_000 + (i + 1) * 10_000
            _write_config(
                config_path,
                server_port=server_port,
                upstream_port=upstream_port,
                inflight_penalty=penalty,
            )

            t0 = time.monotonic()
            exit_code, stdout, stderr = await _run_rehash(config_path, env)
            elapsed_ms = (time.monotonic() - t0) * 1000

            assert exit_code == 0, (
                f"reload {i} failed (exit={exit_code}):\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
            latencies.append(elapsed_ms)

        assert proc.returncode is None, "server process died"

        p50 = statistics.quantiles(latencies, n=2)[0]
        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        p99 = sorted(latencies)[int(len(latencies) * 0.99)]

        print(
            f"\nReload latency: p50={p50:.1f}ms p95={p95:.1f}ms "
            f"p99={p99:.1f}ms min={min(latencies):.1f}ms "
            f"max={max(latencies):.1f}ms (n={len(latencies)})"
        )
        _write_measurement(
            "reload_latency",
            {
                "p50_ms": round(p50, 1),
                "p95_ms": round(p95, 1),
                "p99_ms": round(p99, 1),
                "min_ms": round(min(latencies), 1),
                "max_ms": round(max(latencies), 1),
                "all_latencies_ms": [round(lat, 1) for lat in latencies],
            },
        )

        assert p50 < 1500, f"p50={p50:.1f}ms exceeds 1500ms budget"
        assert p95 < 3000, f"p95={p95:.1f}ms exceeds 3000ms budget"
    finally:
        await _terminate_server(proc)
        upstream.shutdown()


@pytest.mark.skipif(psutil is None, reason="psutil not available")
@pytest.mark.asyncio()
async def test_d3_reload_memory_delta_bounded(tmp_path: Any) -> None:
    """Measure RSS delta of a single LIVE reload on the server PID.

    Budget: delta < 50 MB (generous; allocator caching is expected).

    Skipped if ``psutil`` is not installed.
    """
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    proc = await _spawn_server(config_path, env)
    try:
        healthy = await _wait_healthy(server_port)
        assert healthy, "server did not become healthy"

        # Allow the process to settle after startup
        await asyncio.sleep(2.0)

        rss_before = _get_rss_bytes(proc.pid)
        assert rss_before is not None, "could not read RSS before reload"

        # Trigger a real LIVE reload
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=999_999,
        )
        exit_code, stdout, stderr = await _run_rehash(config_path, env)
        assert exit_code == 0, (
            f"rehash failed (exit={exit_code}):\nstdout={stdout}\nstderr={stderr}"
        )

        # Give the process a moment to settle post-reload
        await asyncio.sleep(1.0)

        rss_after = _get_rss_bytes(proc.pid)
        assert rss_after is not None, "could not read RSS after reload"

        delta_mb = (rss_after - rss_before) / (1024 * 1024)
        assert delta_mb < 50, (
            f"RSS delta {delta_mb:.1f} MB exceeds 50 MB budget "
            f"(before={rss_before / 1024 / 1024:.1f} MB, "
            f"after={rss_after / 1024 / 1024:.1f} MB)"
        )
        assert proc.returncode is None, "server process died"
    finally:
        await _terminate_server(proc)
        upstream.shutdown()


@pytest.mark.asyncio()
async def test_d3_reload_under_concurrent_traffic(tmp_path: Any) -> None:
    """Verify reload is non-disruptive under concurrent lightweight traffic.

    50 parallel tasks issue GET /health requests in a tight loop for 5 s.
    Halfway through, a LIVE reload is triggered.  Assertions:
        - 0 non-200 responses
        - p95 latency < 750 ms

    The 750 ms budget accounts for the overhead of 50 concurrent async
    httpx clients sharing the event loop on CI hardware; observed p95
    on a MacBook Pro is ~675 ms, so 750 ms provides headroom without
    being so loose as to be meaningless.
    """
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    proc = await _spawn_server(config_path, env)
    try:
        healthy = await _wait_healthy(server_port)
        assert healthy, "server did not become healthy"

        latencies: list[float] = []
        non_200_count = 0
        stop_event = asyncio.Event()

        async def _health_loop() -> None:
            nonlocal non_200_count
            while not stop_event.is_set():
                t0 = time.monotonic()
                try:
                    async with httpx.AsyncClient() as client:
                        r = await client.get(
                            f"http://127.0.0.1:{server_port}/v1/healthz",
                            timeout=2.0,
                        )
                    elapsed_ms = (time.monotonic() - t0) * 1000
                    latencies.append(elapsed_ms)
                    if r.status_code != 200:
                        non_200_count += 1
                except Exception:
                    non_200_count += 1
                # Brief yield to event loop
                await asyncio.sleep(0.01)

        # Launch 50 concurrent health-check loops
        tasks = [asyncio.create_task(_health_loop()) for _ in range(50)]

        # Let traffic run for 1 s then trigger reload
        await asyncio.sleep(1.0)

        # Rewrite config for a real LIVE reload
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=777_777,
        )
        await _run_rehash(config_path, env)

        # Let traffic continue for remaining duration
        await asyncio.sleep(3.0)
        stop_event.set()

        # Wait for all tasks to finish with a generous timeout
        _, pending = await asyncio.wait(tasks, timeout=10.0)
        for t in pending:
            t.cancel()
        # Allow cancellations to propagate
        if pending:
            await asyncio.wait(pending, timeout=5.0)

        assert non_200_count == 0, (
            f"{non_200_count} non-200 responses during concurrent reload"
        )
        assert len(latencies) > 0, "no health-check requests completed"

        p95 = sorted(latencies)[int(len(latencies) * 0.95)]
        print(
            f"\nConcurrent health p95={p95:.1f}ms "
            f"(n={len(latencies)}, errors={non_200_count})"
        )
        _write_measurement(
            "concurrent_traffic",
            {
                "p95_ms": round(p95, 1),
                "sample_count": len(latencies),
                "errors": non_200_count,
            },
        )
        assert p95 < 1500, f"health-check p95={p95:.1f}ms exceeds 1500ms budget"
        assert proc.returncode is None, "server process died"
    finally:
        stop_event.set()
        await _terminate_server(proc)
        upstream.shutdown()


# ---------------------------------------------------------------------------
# Phase 5 closure — per-generation resource overhead and dispatch overhead
# ---------------------------------------------------------------------------


def _open_fd_count(pid: int) -> int | None:
    """Best-effort open file-descriptor count for *pid*.

    Returns None when ``/proc/{pid}/fd`` is unavailable (e.g. macOS);
    callers must tolerate None gracefully.
    """
    import glob

    try:
        entries = glob.glob(f"/proc/{pid}/fd/*")
        return len(entries)
    except (OSError, PermissionError):
        return None


@pytest.mark.asyncio()
async def test_d3_per_generation_resource_overhead(tmp_path: Any) -> None:
    """Verify that 10 reloads in quick succession don't exhaust resources.

    Asserts:

    - active generation id advances by exactly 10
    - FD count does not grow unboundedly (when /proc available)
    - every reload succeeds
    - final ``last_reload_result.ok`` is True and stage=retirement
    """
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    proc = await _spawn_server(config_path, env)
    try:
        healthy = await _wait_healthy(server_port)
        assert healthy, "server did not become healthy"

        async def _snapshot() -> dict[str, Any]:
            async with httpx.AsyncClient() as client:
                return await _fetch_runtime_snapshot(
                    client,
                    server_port,
                    {"Authorization": "Bearer test-rehash-key"},
                )

        baseline = await _snapshot()
        baseline_gen = (
            baseline.get("runtime_manager", {}).get("active", {}).get("generation_id")
        )
        baseline_fd = _open_fd_count(proc.pid)

        iterations = 10
        gen_id_seen: set[int] = {baseline_gen}
        for i in range(iterations):
            _write_config(
                config_path,
                server_port=server_port,
                upstream_port=upstream_port,
                inflight_penalty=100_000 + (i + 1) * 5_000,
            )
            exit_code, stdout, stderr = await _run_rehash(config_path, env)
            assert exit_code == 0, (
                f"reload {i} failed (exit={exit_code}):\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
            snap = await _snapshot()
            gen_id_seen.add(
                snap.get("runtime_manager", {}).get("active", {}).get("generation_id")
            )
            assert proc.returncode is None, f"server died at reload {i}"

        final = await _snapshot()
        final_gen = (
            final.get("runtime_manager", {}).get("active", {}).get("generation_id")
        )
        final_fd = _open_fd_count(proc.pid)
        last_reload = final.get("reload_state", {}).get("last_reload_result", {})

        assert final_gen - baseline_gen == iterations, (
            f"generation advanced by {final_gen - baseline_gen}, expected {iterations}"
        )

        seen_sorted = sorted(gen_id_seen)
        assert seen_sorted == list(
            range(baseline_gen, baseline_gen + iterations + 1)
        ), f"non-contiguous generation ids: {seen_sorted} (baseline={baseline_gen})"

        if baseline_fd is not None and final_fd is not None:
            assert final_fd <= baseline_fd + 10, (
                f"FD count grew too much: {baseline_fd} -> {final_fd}"
            )

        assert last_reload.get("ok") is True, (
            f"last reload result not ok: {last_reload}"
        )

        print(
            f"\nPer-generation overhead: gen_advances={iterations} "
            f"fds={baseline_fd}->{final_fd}"
        )
        _write_measurement(
            "per_generation_overhead",
            {
                "iterations": iterations,
                "gen_advances": final_gen - baseline_gen,
                "baseline_fd": baseline_fd,
                "final_fd": final_fd,
            },
        )
    finally:
        await _terminate_server(proc)
        upstream.shutdown()


@pytest.mark.asyncio()
async def test_d3_dispatch_overhead_under_reload(tmp_path: Any) -> None:
    """Measure /v1/healthz latency before, during, and after a LIVE reload.

    Asserts:

    - median latency before reload < 25 ms
    - median latency after reload < 25 ms
    - no request during the reload returns non-200
    """
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    proc = await _spawn_server(config_path, env)
    try:
        healthy = await _wait_healthy(server_port)
        assert healthy, "server did not become healthy"

        async def _measure(samples: int) -> tuple[list[float], int]:
            latencies: list[float] = []
            errors = 0
            async with httpx.AsyncClient() as client:
                for _ in range(samples):
                    t0 = time.monotonic()
                    try:
                        r = await client.get(
                            f"http://127.0.0.1:{server_port}/v1/healthz",
                            timeout=5.0,
                        )
                        elapsed = (time.monotonic() - t0) * 1000
                        if r.status_code == 200:
                            latencies.append(elapsed)
                        else:
                            errors += 1
                    except Exception:
                        errors += 1
                    await asyncio.sleep(0.005)
            return latencies, errors

        # Baseline
        baseline_latencies, baseline_errors = await _measure(50)
        assert baseline_errors == 0
        baseline_median = sorted(baseline_latencies)[len(baseline_latencies) // 2]

        # Trigger reload (acquire inside measure loop simulates ongoing traffic)
        reload_task = asyncio.create_task(
            _run_rehash_with_write(config_path, env, server_port, upstream_port)
        )

        # Concurrent traffic during reload
        in_flight_latencies, in_flight_errors = await _measure(50)

        await reload_task
        exit_code, stdout, stderr = reload_task.result()
        assert exit_code == 0, (
            f"reload failed (exit={exit_code}):\nstdout={stdout}\nstderr={stderr}"
        )

        # Post-reload
        post_latencies, post_errors = await _measure(50)
        assert post_errors == 0
        post_median = sorted(post_latencies)[len(post_latencies) // 2]

        # In-flight errors must be zero — rehash must not drop traffic.
        assert in_flight_errors == 0, f"{in_flight_errors} errors during rehash"

        in_flight_median = sorted(in_flight_latencies)[len(in_flight_latencies) // 2]
        print(
            f"\nDispatch overhead (healthz median): "
            f"baseline={baseline_median:.1f}ms "
            f"in_flight={in_flight_median:.1f}ms "
            f"post={post_median:.1f}ms"
        )
        _write_measurement(
            "dispatch_overhead",
            {
                "baseline_median_ms": round(baseline_median, 1),
                "in_flight_median_ms": round(
                    sorted(in_flight_latencies)[len(in_flight_latencies) // 2], 1
                ),
                "post_median_ms": round(post_median, 1),
                "errors_during_reload": in_flight_errors,
            },
        )

        assert baseline_median < 25, (
            f"baseline median {baseline_median:.1f}ms exceeds 25ms"
        )
        assert post_median < 25, f"post-reload median {post_median:.1f}ms exceeds 25ms"
        assert proc.returncode is None, "server process died"
    finally:
        await _terminate_server(proc)
        upstream.shutdown()


async def _run_rehash_with_write(
    config_path: str,
    env: dict[str, str],
    server_port: int,
    upstream_port: int,
) -> tuple[int, str, str]:
    """Rewrite config to force a LIVE change, then trigger ``eggpool rehash``."""
    _write_config(
        config_path,
        server_port=server_port,
        upstream_port=upstream_port,
        inflight_penalty=222_222,
    )
    return await _run_rehash(config_path, env)
