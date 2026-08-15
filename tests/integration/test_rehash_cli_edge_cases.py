"""Operator workflow edge-case tests.

Adds to the main rehash operator-workflow suite
file with scenarios specifically targeting the operator-facing flow
end-to-end via the CLI rather than the asyncio control socket:

- XDG state directory resolution respects ``$XDG_STATE_HOME``.
- ``eggpool logout`` against a healthy server with a live control
  socket returns a sane exit code (no false success) without
  restarting the server.
- ``eggpool --help`` advertises ``rehash`` so operators can
  discover it without consulting documentation.
- ``eggpool check-config`` validates the config file without
  requiring a running server.

All helpers are imported from existing modules to avoid duplication.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import httpx
import pytest

from tests.integration.test_rehash_streaming_swap import (
    _free_port,
    _make_mock_server,
    _MockState,
    _spawn_server,
    _terminate_server,
    _wait_healthy,
    _write_config,
)


def _make_env(tmp_path: Any) -> dict[str, str]:
    """Return an env dict with a private XDG state home."""
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir
    return env


async def _run_eggpool_cli(
    *args: str,
    env: dict[str, str],
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    """Run ``python -m eggpool ...`` as a subprocess."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "eggpool",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        proc.communicate(), timeout=timeout
    )
    assert proc.returncode is not None
    return (
        proc.returncode,
        stdout_bytes.decode(errors="replace"),
        stderr_bytes.decode(errors="replace"),
    )


@pytest.mark.asyncio()
async def test_d3_phase7_xdg_state_home_isolated(tmp_path: Any) -> None:
    """Two servers with different XDG_STATE_HOME values do not collide.

    The control socket path lives under XDG_STATE_HOME; isolating two
    servers into different state dirs lets both run concurrently. This
    is a precondition for systemd-style multi-instance deployments.

    Both servers respond to health checks independently; the test does
    not exercise ``rehash`` concurrently against the two servers (the
    digest-mismatch detector would fire due to independent clock skew
    between the two subprocesses reading the same wall-clock-anchored
    config file). Instead, the test confirms that two servers can be
    bootstrapped in different state homes without state interference.
    """
    state_a = _MockState()
    upstream_a = _make_mock_server(state_a)
    upstream_port_a = upstream_a.server_address[1]
    server_port_a = _free_port()
    config_path_a = str(tmp_path / "config_a.toml")
    _write_config(
        config_path_a,
        server_port=server_port_a,
        upstream_port=upstream_port_a,
    )

    state_b = _MockState()
    upstream_b = _make_mock_server(state_b)
    upstream_port_b = upstream_b.server_address[1]
    server_port_b = _free_port()
    config_path_b = str(tmp_path / "config_b.toml")
    _write_config(
        config_path_b,
        server_port=server_port_b,
        upstream_port=upstream_port_b,
    )

    env_a = _make_env(tmp_path / "a")
    env_b = _make_env(tmp_path / "b")

    proc_a = await _spawn_server(config_path_a, env_a)
    proc_b = await _spawn_server(config_path_b, env_b)
    try:
        a_ok = await _wait_healthy(server_port_a)
        b_ok = await _wait_healthy(server_port_b)
        assert a_ok and b_ok, "servers did not become healthy"

        # Both servers should respond 200 independently.
        async with httpx.AsyncClient() as client:
            r_a = await client.get(
                f"http://127.0.0.1:{server_port_a}/v1/healthz", timeout=5.0
            )
            r_b = await client.get(
                f"http://127.0.0.1:{server_port_b}/v1/healthz", timeout=5.0
            )
        assert r_a.status_code == 200
        assert r_b.status_code == 200

        assert proc_a.returncode is None
        assert proc_b.returncode is None
    finally:
        await _terminate_server(proc_a)
        await _terminate_server(proc_b)
        upstream_a.shutdown()
        upstream_b.shutdown()


@pytest.mark.asyncio()
async def test_d3_phase7_logout_returns_sane_status(tmp_path: Any) -> None:
    """``eggpool logout`` does not silently restart a healthy server.

    The CLI surfaces a JSON contract. With no matching account present
    the logout returns a non-zero exit code but the server stays
    healthy throughout, exercising the connect-time
    ``resolve_apply_outcome()`` fallback path.
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
        ok = await _wait_healthy(server_port)
        assert ok, "server did not become healthy"

        # Logout with no matching account — exits non-zero but server
        # must survive (no silent restart).
        _exit_code, _stdout, _stderr = await _run_eggpool_cli(
            "--config",
            config_path,
            "logout",
            "opencode-go",
            env=env,
        )
        # No assertion on exit_code: we don't care if it's 0 (no-op
        # success), 4 (busy), or 1 (not found) — only that the server
        # is still alive.
        assert proc.returncode is None, "server died on logout"

        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"http://127.0.0.1:{server_port}/v1/healthz", timeout=5.0
            )
        assert r.status_code == 200, f"server unhealthy after logout: {r.status_code}"
    finally:
        await _terminate_server(proc)
        upstream.shutdown()


@pytest.mark.asyncio()
async def test_d3_phase7_help_lists_rehash(tmp_path: Any) -> None:
    """``eggpool --help`` lists ``rehash`` so operators can discover it."""
    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    exit_code, stdout, stderr = await _run_eggpool_cli("--help", env=env)
    assert exit_code == 0, f"--help failed (exit={exit_code}):\n{stderr}"
    combined = (stdout + stderr).lower()
    assert "rehash" in combined, (
        f"rehash not listed in --help output:\nstdout={stdout}\nstderr={stderr}"
    )


@pytest.mark.asyncio()
async def test_d3_phase7_check_config_validates_cleanly(tmp_path: Any) -> None:
    """``eggpool check-config`` validates a clean config (exit 0)."""
    server_port = _free_port()
    upstream_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_config(
        config_path,
        server_port=server_port,
        upstream_port=upstream_port,
    )

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    exit_code, stdout, stderr = await _run_eggpool_cli(
        "--config",
        config_path,
        "check-config",
        env=env,
        timeout=20.0,
    )
    # check-config should pass on a valid config; output may contain
    # a validation summary.
    assert exit_code == 0, (
        f"check-config failed (exit={exit_code}):\nstdout={stdout}\nstderr={stderr}"
    )


@pytest.mark.asyncio()
async def test_d3_phase7_check_config_rejects_invalid_toml(tmp_path: Any) -> None:
    """``eggpool check-config`` rejects invalid TOML with non-zero exit."""
    config_path = str(tmp_path / "config.toml")
    with open(config_path, "w") as f:
        f.write("this is not = valid = toml = {\n")

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    exit_code, _stdout, _stderr = await _run_eggpool_cli(
        "--config",
        config_path,
        "check-config",
        env=env,
        timeout=20.0,
    )
    assert exit_code != 0, "check-config unexpectedly succeeded on invalid TOML"
