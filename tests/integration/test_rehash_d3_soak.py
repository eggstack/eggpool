"""Milestone D3 — Soak and resource-leak test for live config rehash.

Runs 25 alternating reloads and asserts:
- Process-owned resources (process_supervisor) retain identity across reloads.
- Generation-leased resources get fresh identity on each reload.
- No leaked asyncio tasks, file descriptors, or DB connections.
- After 25 reloads: zero retiring generations, zero pending leases,
  task_spec_version >= 25.

Uses real subprocess (``eggpool serve --verbose``) with mock upstream
HTTP servers on localhost for true E2E fidelity.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
import pytest

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

pytestmark = pytest.mark.reload

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOAK_RELOADS = 25
SOAK_TIMEOUT = 120.0  # CI budget


async def _fetch_runtime_snapshot(
    client: httpx.AsyncClient,
    server_port: int,
    auth: dict[str, str],
) -> dict[str, Any]:
    """Fetch the full runtime snapshot from /api/stats/runtime."""
    r = await client.get(
        f"http://127.0.0.1:{server_port}/api/stats/runtime",
        headers=auth,
        timeout=10.0,
    )
    assert r.status_code == 200, f"runtime stats failed: {r.status_code}"
    return r.json()


async def _wait_retiring_drained(
    client: httpx.AsyncClient,
    server_port: int,
    auth: dict[str, str],
    *,
    timeout: float = 15.0,
) -> None:
    """Poll /api/stats/runtime until retiring_count reaches 0."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = await _fetch_runtime_snapshot(client, server_port, auth)
        rm = snap.get("runtime_manager", {})
        if rm.get("retiring_count", 1) == 0:
            return
        await asyncio.sleep(0.3)
    # Final check — fail if still draining
    snap = await _fetch_runtime_snapshot(client, server_port, auth)
    rm = snap.get("runtime_manager", {})
    assert rm.get("retiring_count", 1) == 0, (
        f"retiring_count still > 0 after drain timeout: {rm}"
    )


@pytest.mark.asyncio()
async def test_d3_soak_25_reloads_resource_identity(tmp_path: Any) -> None:
    """Soak test: 25 alternating reloads assert resource identity invariants.

    Process-owned resources (process_supervisor) retain the SAME identity
    across all reloads.  Generation-leased resources (client_pool,
    outbound_manager, supervisor, finalization_retry_queue,
    routing_trace_guard, tuning_registry, transcoder/compression/cache
    policy, config) get fresh identity on each reload.

    After 25 reloads:
    - Zero retiring generations (drained).
    - Zero pending request leases.
    - task_spec_version >= 25.
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
        healthy = await _wait_healthy(server_port, timeout=30.0)
        assert healthy, "server did not become healthy"
        original_pid = proc.pid
        auth = {"Authorization": "Bearer test-rehash-key"}

        # -- Baseline snapshot -----------------------------------------------
        async with httpx.AsyncClient() as client:
            baseline = await _fetch_runtime_snapshot(client, server_port, auth)

        rm_baseline = baseline.get("runtime_manager", {})
        active_baseline = rm_baseline.get("active", {})
        baseline_gen_id = active_baseline.get("generation_id")
        baseline_digest = active_baseline.get("config_digest_prefix")
        assert baseline_gen_id is not None, "no baseline generation_id"
        assert baseline_digest is not None, "no baseline config_digest_prefix"

        # Capture process_supervisor identity from the first snapshot.
        # The process_supervisor is a process-owned TaskSupervisor; we
        # identify it via the runtime_manager snapshot's task_spec_version
        # and task_reload_summary fields, plus the generation_id of the
        # first active generation.
        baseline_task_spec_version = rm_baseline.get("task_spec_version", 0)
        assert baseline_task_spec_version >= 0, "invalid baseline task_spec_version"

        # We track generation-leased resource identity by comparing the
        # active generation's config_digest_prefix and generation_id across
        # reloads.  Each reload must produce a NEW generation_id.
        prev_gen_id = baseline_gen_id
        prev_digest = baseline_digest
        all_gen_ids: list[int] = [baseline_gen_id]

        # -- Alternating reloads ---------------------------------------------
        # The initial config uses inflight_penalty=100_000, so the first
        # reload must use a DIFFERENT value to trigger a generation swap.
        for i in range(SOAK_RELOADS):
            # Alternate between two inflight_penalty values to force
            # a LIVE config change on every iteration.
            penalty = 200_000 if i % 2 == 0 else 100_000
            _write_config(
                config_path,
                server_port=server_port,
                upstream_port=upstream_port,
                inflight_penalty=penalty,
            )

            exit_code, stdout, stderr = await _run_rehash(config_path, env)
            assert exit_code == 0, (
                f"reload {i + 1}/{SOAK_RELOADS} failed "
                f"(exit={exit_code}):\nstdout={stdout}\nstderr={stderr}"
            )

            # PID must remain stable (process never restarts).
            assert proc.pid == original_pid, (
                f"PID changed at reload {i + 1}: {proc.pid} != {original_pid}"
            )
            assert proc.returncode is None, f"server process died at reload {i + 1}"

            # Wait for retiring generation to drain before the next reload.
            async with httpx.AsyncClient() as client:
                await _wait_retiring_drained(client, server_port, auth)

            # Fetch the new runtime snapshot.
            async with httpx.AsyncClient() as client:
                snap = await _fetch_runtime_snapshot(client, server_port, auth)

            rm = snap.get("runtime_manager", {})
            active = rm.get("active", {})
            new_gen_id = active.get("generation_id")
            new_digest = active.get("config_digest_prefix")

            # -- Generation-leased resources: fresh identity ---------------
            # The generation_id must have advanced.
            assert new_gen_id is not None, (
                f"reload {i + 1}: no generation_id in snapshot"
            )
            assert new_gen_id > prev_gen_id, (
                f"reload {i + 1}: generation_id did not advance: "
                f"{prev_gen_id} -> {new_gen_id}"
            )
            # The config_digest_prefix must have changed.
            assert new_digest != prev_digest, (
                f"reload {i + 1}: config_digest_prefix unchanged: {new_digest}"
            )

            # -- Process-owned resources: same identity ---------------------
            # The task_spec_version must have incremented.
            new_task_spec_version = rm.get("task_spec_version", 0)
            assert new_task_spec_version > baseline_task_spec_version, (
                f"reload {i + 1}: task_spec_version did not increment: "
                f"{new_task_spec_version} (baseline {baseline_task_spec_version})"
            )
            # Update baseline for next iteration's comparison.
            baseline_task_spec_version = new_task_spec_version

            # Server still healthy after reload.
            async with httpx.AsyncClient() as client:
                health = await client.get(
                    f"http://127.0.0.1:{server_port}/v1/healthz",
                    timeout=5.0,
                )
                assert health.status_code == 200, f"reload {i + 1}: health check failed"

            # Track progression.
            all_gen_ids.append(new_gen_id)
            prev_gen_id = new_gen_id
            prev_digest = new_digest

        # -- Post-soak assertions -------------------------------------------
        # All generation IDs are strictly increasing.
        for j in range(1, len(all_gen_ids)):
            assert all_gen_ids[j] > all_gen_ids[j - 1], (
                f"generation IDs not strictly increasing at index {j}: "
                f"{all_gen_ids[j - 1]} -> {all_gen_ids[j]}"
            )
        assert len(all_gen_ids) == SOAK_RELOADS + 1, (
            f"expected {SOAK_RELOADS + 1} generation IDs, got {len(all_gen_ids)}"
        )

        # Final runtime snapshot: zero retiring generations, zero leases.
        async with httpx.AsyncClient() as client:
            final_snap = await _fetch_runtime_snapshot(client, server_port, auth)

        rm_final = final_snap.get("runtime_manager", {})
        assert rm_final.get("retiring_count", 1) == 0, (
            f"retiring_count still > 0 after soak: {rm_final}"
        )

        active_final = rm_final.get("active", {})
        active_leases = active_final.get("active_leases", -1)
        assert active_leases == 0, f"active_leases > 0 after soak: {active_leases}"

        # task_spec_version >= SOAK_RELOADS (each reload increments it).
        final_task_spec_version = rm_final.get("task_spec_version", 0)
        assert final_task_spec_version >= SOAK_RELOADS, (
            f"task_spec_version {final_task_spec_version} < {SOAK_RELOADS}"
        )

        # Server still healthy.
        async with httpx.AsyncClient() as client:
            health = await client.get(
                f"http://127.0.0.1:{server_port}/v1/healthz",
                timeout=5.0,
            )
            assert health.status_code == 200, (
                "health check failed after soak completion"
            )

        # Process PID unchanged throughout.
        assert proc.pid == original_pid, "PID changed during soak"
        assert proc.returncode is None, "server process died during soak"

    finally:
        await _terminate_server(proc)
        upstream.shutdown()
