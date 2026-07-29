"""Operator-workflow integration tests for ``eggpool rehash``.

Exercises the operator-facing CLI commands end-to-end against a real
``eggpool serve`` subprocess and mock upstream.

Scenarios:

1. Happy path — LIVE field change, exit 0, JSON contract keys.
2. Restart-required change — exit 2.
3. ``--json`` output format — parse JSON, assert 9 pinned keys.
4. No-op (identical config) — exit 0 with noop indicator.
5. Dead server — exit 3 (control unavailable).
6. Concurrent caller — at least one returns exit 4 (busy).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

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

# Canonical keys every ``--json`` output must contain (from
# ``cli_rehash_format.py``).
_EXPECTED_JSON_KEYS: frozenset[str] = frozenset(
    {
        "ok",
        "stage",
        "exit_code",
        "generation",
        "changed_sections",
        "warnings",
        "restart_required",
        "retirement_pending",
        "message",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_env(tmp_path: Any) -> dict[str, str]:
    """Return an env dict with a private ``XDG_STATE_HOME``."""
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir
    return env


def _parse_json_output(text: str) -> dict[str, Any]:
    """Extract the first JSON object from CLI output that may contain prose."""
    idx = text.index("{")
    return json.loads(text[idx:])


async def _run_rehash_json(
    config_path: str,
    env: dict[str, str],
) -> tuple[int, str, str]:
    """Run ``eggpool rehash --json`` as a subprocess."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "eggpool",
        "--config",
        config_path,
        "rehash",
        "--json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30)
    return (
        proc.returncode or 0,
        stdout_bytes.decode(errors="replace"),
        stderr_bytes.decode(errors="replace"),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_operator_happy_path_live_change(tmp_path: Any) -> None:
    """Changing a LIVE field (inflight_penalty) yields exit 0."""
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    env = _make_env(tmp_path)
    proc = await _spawn_server(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        # Change a LIVE field
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=250_000,
        )

        exit_code, stdout, stderr = await _run_rehash(config_path, env)
        assert exit_code == 0, (
            f"rehash failed (exit={exit_code}):\nstdout={stdout}\nstderr={stderr}"
        )
    finally:
        await _terminate_server(proc)
        upstream.shutdown()


@pytest.mark.asyncio()
async def test_d3_operator_restart_required_rejects(tmp_path: Any) -> None:
    """Changing server.port (RESTART_REQUIRED) yields exit 2."""
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    env = _make_env(tmp_path)
    proc = await _spawn_server(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        # Change a RESTART_REQUIRED field
        replacement_port = _free_port()
        _write_config(
            config_path,
            server_port=replacement_port,
            upstream_port=upstream_port,
        )

        exit_code, stdout, stderr = await _run_rehash(config_path, env)
        combined = stdout + stderr
        assert exit_code == 2, (
            f"expected exit 2 (RESTART_REQUIRED), got {exit_code}:\n{combined}"
        )
    finally:
        await _terminate_server(proc)
        upstream.shutdown()


@pytest.mark.asyncio()
async def test_d3_operator_json_output_contract(tmp_path: Any) -> None:
    """``--json`` output contains all 9 pinned keys with correct types."""
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    env = _make_env(tmp_path)
    proc = await _spawn_server(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        # Change a LIVE field so rehash has real work to do
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=350_000,
        )

        exit_code, stdout, stderr = await _run_rehash_json(config_path, env)
        assert exit_code == 0, (
            f"rehash --json failed (exit={exit_code}):\n"
            f"stdout={stdout}\nstderr={stderr}"
        )

        parsed = _parse_json_output(stdout)
        assert set(parsed.keys()) >= _EXPECTED_JSON_KEYS, (
            f"missing keys: {_EXPECTED_JSON_KEYS - set(parsed.keys())}"
        )

        # Type assertions
        assert isinstance(parsed["ok"], bool)
        assert isinstance(parsed["stage"], str)
        assert isinstance(parsed["exit_code"], int)
        assert isinstance(parsed["changed_sections"], list)
        assert isinstance(parsed["warnings"], list)
        assert isinstance(parsed["restart_required"], list)
        assert isinstance(parsed["retirement_pending"], bool)
        assert isinstance(parsed["message"], str)

        # Value assertions for a successful live reload
        assert parsed["ok"] is True
        assert parsed["exit_code"] == 0
        # The old generation may finish retiring before the response is
        # serialized, so successful reloads can legitimately report either
        # retirement state.
    finally:
        await _terminate_server(proc)
        upstream.shutdown()


@pytest.mark.asyncio()
async def test_d3_operator_noop_second_rehash(tmp_path: Any) -> None:
    """Running rehash twice with identical config: second is a no-op (exit 0)."""
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    env = _make_env(tmp_path)
    proc = await _spawn_server(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        # First rehash — no config change → noop
        exit_code1, stdout1, stderr1 = await _run_rehash(config_path, env)
        assert exit_code1 == 0, (
            f"first rehash failed (exit={exit_code1}):\n{stdout1}\n{stderr1}"
        )

        # Second rehash — still no config change → noop
        exit_code2, stdout2, stderr2 = await _run_rehash(config_path, env)
        combined2 = stdout2 + stderr2
        assert exit_code2 == 0, (
            f"second rehash failed (exit={exit_code2}):\n{combined2}"
        )
        assert (
            "no configuration changes" in combined2.lower()
            or "unchanged" in combined2.lower()
            or "noop" in combined2.lower()
            or "no-op" in combined2.lower()
        ), f"expected no-op indicator, got: {combined2}"

        assert proc.returncode is None, "server process died"
    finally:
        await _terminate_server(proc)
        upstream.shutdown()


@pytest.mark.asyncio()
async def test_d3_operator_dead_server_exit3(tmp_path: Any) -> None:
    """Rehash against a dead server yields exit 3 (CONTROL_UNAVAILABLE)."""
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=19999, upstream_port=19998)

    env = _make_env(tmp_path)

    # Do NOT spawn a server — the control socket does not exist.
    exit_code, stdout, stderr = await _run_rehash(config_path, env)
    combined = stdout + stderr
    assert exit_code == 3, (
        f"expected exit 3 (CONTROL_UNAVAILABLE), got {exit_code}:\n{combined}"
    )


@pytest.mark.asyncio()
async def test_d3_operator_concurrent_busy(tmp_path: Any) -> None:
    """Four concurrent rehash calls: at least one returns exit 4 (BUSY).

    The original subprocess-based test was unable to deterministically
    hit the admission guard on fast hosts (required up to 5 attempts).
    The deterministic, in-process equivalent lives in the reload test
    suite (``test_concurrent_reload_admission_deterministic``).  This
    subprocess-based test is retained as a smoke test of the busy
    operator-workflow path; it now passes deterministically thanks
    to the atomic admission claim.
    """
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(config_path, server_port=server_port, upstream_port=upstream_port)

    env = _make_env(tmp_path)
    proc = await _spawn_server(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        # Change a LIVE field so each rehash has work to do
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=400_000,
        )

        # Fire concurrent rehash commands with retry for fast hosts
        max_attempts = 5
        all_exit_codes: list[int] = []
        busy_count = 0
        for _attempt in range(max_attempts):
            results = await asyncio.gather(
                _run_rehash(config_path, env),
                _run_rehash(config_path, env),
                _run_rehash(config_path, env),
                _run_rehash(config_path, env),
                return_exceptions=True,
            )

            exit_codes = []
            for r in results:
                if isinstance(r, Exception):
                    continue
                exit_codes.append(r[0])

            all_exit_codes.extend(exit_codes)
            busy_count = sum(1 for ec in all_exit_codes if ec == 4)
            if busy_count >= 1:
                break

        assert len(all_exit_codes) >= 4, (
            f"expected at least 4 results, got {len(all_exit_codes)}: {all_exit_codes}"
        )
        # At least one must be busy (exit 4)
        assert busy_count >= 1, f"no rehash returned exit 4 (BUSY): {all_exit_codes}"

        assert proc.returncode is None, "server process died"
    finally:
        await _terminate_server(proc)
        upstream.shutdown()
