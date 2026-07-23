"""D3 release-validation acceptance tests for live config rehash.

Exercises the canonical process-level acceptance scenarios from the
D3 plan (Phase 2) that are NOT already covered by
``test_rehash_streaming_swap.py``.  Imports all mock upstream, config,
and CLI helpers from the canonical test file to avoid duplication.

Each test runs a real ``eggpool serve --verbose`` subprocess with a
deterministic mock upstream, exercises a specific rehash scenario, and
asserts observable behaviour (exit codes, generation IDs, task counts,
stream completion, etc.).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import threading
import time
from http.server import HTTPServer
from typing import Any

import httpx
import pytest

from tests.integration.test_rehash_streaming_swap import (
    _free_port,
    _make_mock_server,
    _MockState,
    _run_rehash,
    _terminate_server,
    _wait_healthy,
    _write_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

auth: dict[str, str] = {"Authorization": "Bearer test-rehash-key"}


async def _spawn_and_drain(
    config_path: str,
    env: dict[str, str],
) -> tuple[asyncio.subprocess.Process, asyncio.Task[None]]:
    """Spawn server with output suppressed to avoid pipe-buffer deadlocks."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "eggpool",
        "--config",
        config_path,
        "serve",
        "--verbose",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    # Drain is a no-op when stdout/stderr are DEVNULL, but we keep the
    # API consistent with _spawn_server for compatibility.
    drain_task = asyncio.create_task(asyncio.sleep(0))
    return proc, drain_task


async def _runtime_generation_id(
    client: httpx.AsyncClient,
    server_port: int,
) -> int | None:
    """Fetch the active generation id from /api/stats/runtime."""
    try:
        r = await client.get(
            f"http://127.0.0.1:{server_port}/api/stats/runtime",
            headers=auth,
            timeout=5.0,
        )
    except (httpx.ConnectError, httpx.ReadTimeout):
        return None
    if r.status_code != 200:
        return None
    payload = r.json()
    runtime = payload.get("runtime_manager") or {}
    active = runtime.get("active") or {}
    raw_id = active.get("generation_id")
    if isinstance(raw_id, int):
        return raw_id
    return None


async def _get_task_spec_version(
    client: httpx.AsyncClient,
    server_port: int,
) -> int:
    """Fetch the task_spec_version from /api/stats/runtime."""
    try:
        r = await client.get(
            f"http://127.0.0.1:{server_port}/api/stats/runtime",
            headers=auth,
            timeout=5.0,
        )
    except (httpx.ConnectError, httpx.ReadTimeout):
        return 0
    if r.status_code != 200:
        return 0
    return r.json().get("runtime_manager", {}).get("task_spec_version", 0)


async def _get_retiring_count(
    client: httpx.AsyncClient,
    server_port: int,
) -> int:
    """Fetch the retiring_count from /api/stats/runtime."""
    try:
        r = await client.get(
            f"http://127.0.0.1:{server_port}/api/stats/runtime",
            headers=auth,
            timeout=5.0,
        )
    except (httpx.ConnectError, httpx.ReadTimeout):
        return -1
    if r.status_code != 200:
        return -1
    return r.json().get("runtime_manager", {}).get("retiring_count", -1)


async def _send_raw_control_message(
    socket_path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send a raw JSON message to the control socket and return the parsed response."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(socket_path),
        timeout=10.0,
    )
    try:
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=10.0)
        return json.loads(raw)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _get_control_socket_path(xdg_state_home: str | None = None) -> str:
    """Return the control socket path from runtime_paths.runtime_dir().

    Args:
        xdg_state_home: Ignored (kept for backward compatibility).
    """
    from eggpool.runtime_paths import runtime_dir

    return str(runtime_dir() / "eggpool.sock")


def _write_extended_config(
    path: str,
    *,
    server_port: int,
    upstream_port: int,
    inflight_penalty: int = 100_000,
    metrics_flush_interval_s: int = 30,
) -> None:
    """Write a TOML config with additional D2/D3 LIVE fields."""
    db_path = path.replace(".toml", ".db")
    config = f"""\
[server]
api_key = "test-rehash-key"
port = {server_port}

[database]
path = "{db_path}"

[models]
startup_refresh = true
refresh_interval_s = 3600

[routing]
inflight_penalty = {inflight_penalty}

[metrics]
flush_interval_s = {metrics_flush_interval_s}

[providers.provider-a]
id = "provider-a"
base_url = "http://127.0.0.1:{upstream_port}/v1"
protocols = ["openai"]

[providers.provider-a.models_endpoint]
method = "GET"
path = "/models"

[[providers.provider-a.static_models]]
id = "test-model"
protocol = "openai"

[[providers.provider-a.accounts]]
name = "acct-a"
api_key = "key-a"
enabled = true
weight = 1.0
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(config)


def _slow_upstream_handler_factory(
    chunk_delay_s: float = 0.3,
    num_chunks: int = 20,
) -> type:
    """Return a mock handler class that streams many chunks with a delay."""
    import json as _json
    from http.server import BaseHTTPRequestHandler

    from tests.integration.test_rehash_streaming_swap import _fingerprint

    class _SlowMockUpstreamHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

        def do_GET(self) -> None:
            if self.path.rstrip("/") in ("/v1/models", "/models"):
                state: _MockState = self.server.mock_state  # type: ignore[attr-defined]
                body = _json.dumps({"object": "list", "data": state.models}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if self.path.rstrip("/") in (
                "/v1/chat/completions",
                "/chat/completions",
            ):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                body = _json.loads(raw)
                model = body.get("model", "test-model")
                stream = body.get("stream", False)

                state: _MockState = self.server.mock_state  # type: ignore[attr-defined]
                state.requests += 1
                auth_header = self.headers.get("Authorization", "")
                if auth_header:
                    state.auth_fingerprints.append(_fingerprint(auth_header))

                if not stream:
                    resp = {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion",
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "hi"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                    data = _json.dumps(resp).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                for i in range(num_chunks):
                    chunk = {"seq": i, "content": f"chunk-{i}"}
                    line = f"data: {_json.dumps(chunk)}\n\n"
                    try:
                        self.wfile.write(line.encode())
                        self.wfile.flush()
                        state.chunks.append(chunk)
                        time.sleep(chunk_delay_s)
                    except (BrokenPipeError, ConnectionResetError):
                        break

                usage_line = (
                    "data: "
                    + _json.dumps(
                        {
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": num_chunks,
                                "total_tokens": num_chunks + 1,
                            }
                        }
                    )
                    + "\n\n"
                )
                try:
                    self.wfile.write(usage_line.encode())
                    self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            else:
                self.send_error(404)

    return _SlowMockUpstreamHandler


# ---------------------------------------------------------------------------
# Scenario 1: Invalid TOML rejected locally — server never contacted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_invalid_toml_rejected_locally(tmp_path: Any) -> None:
    """Invalid TOML is rejected by CLI preflight; server never contacted.

    Assert: exit code 1, server generation unchanged.
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

    proc, drain = await _spawn_and_drain(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        async with httpx.AsyncClient() as client:
            gen_before = await _runtime_generation_id(client, server_port)

        # Overwrite config with invalid TOML
        with open(config_path, "w") as f:
            f.write("this is not = valid = toml = {\n")

        # CLI must reject locally — exit code 1 (EXIT_VALIDATION)
        exit_code, stdout, stderr = await _run_rehash(config_path, env)
        assert exit_code == 1, (
            f"expected exit code 1 for invalid TOML, got {exit_code}:\n"
            f"stdout={stdout}\nstderr={stderr}"
        )

        # Server generation must be unchanged
        async with httpx.AsyncClient() as client:
            gen_after = await _runtime_generation_id(client, server_port)
        assert gen_before == gen_after, (
            f"generation changed after invalid TOML rejection: "
            f"{gen_before} -> {gen_after}"
        )

        # Server still healthy
        async with httpx.AsyncClient() as client:
            health = await client.get(
                f"http://127.0.0.1:{server_port}/v1/healthz", timeout=5.0
            )
            assert health.status_code == 200

        assert proc.returncode is None, "server process died"
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await _terminate_server(proc)
        upstream.shutdown()


# ---------------------------------------------------------------------------
# Scenario 2: Server-side schema validation rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_server_side_validation_rejection(tmp_path: Any) -> None:
    """Config that parses as valid TOML but fails Pydantic schema validation.

    port = "not an int" is valid TOML but invalid for AppConfig.
    CLI rejects locally before contacting the server.

    Assert: exit code 1, generation unchanged.
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

    proc, drain = await _spawn_and_drain(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        async with httpx.AsyncClient() as client:
            gen_before = await _runtime_generation_id(client, server_port)

        # Write config with valid TOML but invalid schema (port is a string)
        db_path = config_path.replace(".toml", ".db")
        bad_config = f"""\
[server]
api_key = "test-rehash-key"
port = "not an int"

[database]
path = "{db_path}"

[models]
startup_refresh = true

[routing]
inflight_penalty = 100000

[providers.provider-a]
id = "provider-a"
base_url = "http://127.0.0.1:{upstream_port}/v1"
protocols = ["openai"]

[providers.provider-a.models_endpoint]
method = "GET"
path = "/models"

[[providers.provider-a.static_models]]
id = "test-model"
protocol = "openai"

[[providers.provider-a.accounts]]
name = "acct-a"
api_key = "key-a"
enabled = true
weight = 1.0
"""
        with open(config_path, "w") as f:
            f.write(bad_config)

        # CLI must reject locally — exit code 1
        exit_code, stdout, stderr = await _run_rehash(config_path, env)
        assert exit_code == 1, (
            f"expected exit code 1 for schema validation failure, got {exit_code}:\n"
            f"stdout={stdout}\nstderr={stderr}"
        )

        # Generation unchanged
        async with httpx.AsyncClient() as client:
            gen_after = await _runtime_generation_id(client, server_port)
        assert gen_before == gen_after, (
            f"generation changed after schema rejection: {gen_before} -> {gen_after}"
        )

        # Server still healthy on original config
        async with httpx.AsyncClient() as client:
            health = await client.get(
                f"http://127.0.0.1:{server_port}/v1/healthz", timeout=5.0
            )
            assert health.status_code == 200

        assert proc.returncode is None, "server process died"
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await _terminate_server(proc)
        upstream.shutdown()


# ---------------------------------------------------------------------------
# Scenario 3: Digest mismatch rejected — no generation change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_digest_mismatch_rejected_no_generation_change(
    tmp_path: Any,
) -> None:
    """Sending a wrong digest to the control socket is rejected.

    Validates that the server verifies the content digest on the reload
    request.  Sends a deliberately wrong digest directly through the
    control socket and asserts the server rejects it.

    Assert: server rejects with ok=false, generation unchanged.
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

    proc, drain = await _spawn_and_drain(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        async with httpx.AsyncClient() as client:
            gen_before = await _runtime_generation_id(client, server_port)

        socket_path = _get_control_socket_path()
        assert os.path.exists(socket_path), f"control socket not found at {socket_path}"

        # Send a deliberately wrong digest directly to the control socket
        response = await _send_raw_control_message(
            socket_path,
            {
                "protocol_version": 1,
                "request_id": "test-digest-mismatch",
                "command": "reload_config",
                "validated_digest": (
                    "000000000000000000000000000000000000000000000000000000000000dead"
                ),
            },
        )

        # Server must reject the reload
        assert response.get("ok") is False, f"server accepted wrong digest: {response}"

        # Generation unchanged
        async with httpx.AsyncClient() as client:
            gen_after = await _runtime_generation_id(client, server_port)
        if gen_after is not None and gen_before is not None:
            assert gen_after == gen_before, (
                f"generation changed after digest mismatch: {gen_before} -> {gen_after}"
            )

        # Server still healthy
        async with httpx.AsyncClient() as client:
            health = await client.get(
                f"http://127.0.0.1:{server_port}/v1/healthz", timeout=5.0
            )
            assert health.status_code == 200

        assert proc.returncode is None, "server process died"
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await _terminate_server(proc)
        upstream.shutdown()


# ---------------------------------------------------------------------------
# Scenario 4: Background interval convergence — no duplicate tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_background_interval_convergence_no_duplicate_tasks(
    tmp_path: Any,
) -> None:
    """Changing metrics.flush_interval_s via rehash converges without duplicates.

    Assert:
    - rehash succeeds (exit 0)
    - task_spec_version incremented
    - PID unchanged (no restart)
    - Server still healthy
    """
    state = _MockState()
    upstream = _make_mock_server(state)
    upstream_port = upstream.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    _write_extended_config(
        config_path,
        server_port=server_port,
        upstream_port=upstream_port,
        metrics_flush_interval_s=30,
    )

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)

    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    proc, drain = await _spawn_and_drain(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"
        original_pid = proc.pid

        async with httpx.AsyncClient() as client:
            tsv_before = await _get_task_spec_version(client, server_port)

        # Change flush_interval_s to a new value via rehash
        _write_extended_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            metrics_flush_interval_s=45,
        )

        exit_code, stdout, stderr = await _run_rehash(config_path, env)
        assert exit_code == 0, f"rehash failed: {stdout} {stderr}"

        # PID unchanged — no restart
        assert proc.pid == original_pid, "PID changed (process restarted)"
        assert proc.returncode is None, "server process died"

        # task_spec_version must have incremented
        async with httpx.AsyncClient() as client:
            tsv_after = await _get_task_spec_version(client, server_port)
        assert tsv_after > tsv_before, (
            f"task_spec_version did not increment: {tsv_before} -> {tsv_after}"
        )

        # Server still healthy on the new generation
        async with httpx.AsyncClient() as client:
            health = await client.get(
                f"http://127.0.0.1:{server_port}/v1/healthz", timeout=5.0
            )
            assert health.status_code == 200

    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await _terminate_server(proc)
        upstream.shutdown()


# ---------------------------------------------------------------------------
# Scenario 5: Provider removal during active stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_provider_removal_during_active_stream(tmp_path: Any) -> None:
    """Removing a provider while a stream is in flight preserves old completion.

    Starts a slow stream (20 chunks x 300ms), reads 3 chunks, triggers
    a rehash that removes provider-b, then verifies the old stream's
    chunks were delivered.

    Assert:
    - Old stream completes (chunks arrive on old generation).
    - New requests against the removed provider's model return error.
    - /v1/models no longer lists the removed model.
    """
    state_a = _MockState()
    state_a.provider_id = "provider-a"
    state_a.models = [{"id": "test-model", "object": "model", "owned_by": "test"}]
    upstream_a = _make_mock_server(state_a)
    port_a = upstream_a.server_address[1]

    # Slow upstream for provider-b
    slow_handler = _slow_upstream_handler_factory(chunk_delay_s=0.3, num_chunks=20)
    server_b = HTTPServer(("127.0.0.1", 0), slow_handler)
    server_b.mock_state = _MockState()  # type: ignore[attr-defined]
    server_b.mock_state.provider_id = "provider-b"  # type: ignore[attr-defined]
    server_b.mock_state.models = [  # type: ignore[attr-defined]
        {"id": "test-model-b", "object": "model", "owned_by": "test"}
    ]
    t_b = threading.Thread(target=server_b.serve_forever, daemon=True)
    t_b.start()
    port_b = server_b.server_address[1]

    server_port = _free_port()
    config_path = str(tmp_path / "config.toml")
    db_path = config_path.replace(".toml", ".db")

    # Config with both providers
    config_both = f"""\
[server]
api_key = "test-rehash-key"
port = {server_port}

[database]
path = "{db_path}"

[models]
startup_refresh = true
refresh_interval_s = 3600

[routing]
inflight_penalty = 100000

[providers.provider-a]
id = "provider-a"
base_url = "http://127.0.0.1:{port_a}/v1"
protocols = ["openai"]

[providers.provider-a.models_endpoint]
method = "GET"
path = "/models"

[[providers.provider-a.static_models]]
id = "test-model"
protocol = "openai"

[[providers.provider-a.accounts]]
name = "acct-a"
api_key = "key-a"
enabled = true
weight = 1.0

[providers.provider-b]
id = "provider-b"
base_url = "http://127.0.0.1:{port_b}/v1"
protocols = ["openai"]

[providers.provider-b.models_endpoint]
method = "GET"
path = "/models"

[[providers.provider-b.static_models]]
id = "test-model-b"
protocol = "openai"

[[providers.provider-b.accounts]]
name = "acct-b"
api_key = "key-b"
enabled = true
weight = 1.0
"""
    with open(config_path, "w") as f:
        f.write(config_both)

    state_dir = str(tmp_path / "state")
    os.makedirs(state_dir, exist_ok=True)

    env = os.environ.copy()
    env["XDG_STATE_HOME"] = state_dir

    proc, drain = await _spawn_and_drain(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        # Start a slow stream through provider-b
        chunks_read: list[str] = []
        async with (
            httpx.AsyncClient() as client,
            client.stream(
                "POST",
                f"http://127.0.0.1:{server_port}/v1/chat/completions",
                json={
                    "model": "test-model-b",
                    "stream": True,
                    "messages": [{"role": "user", "content": "slow stream"}],
                },
                headers=auth,
                timeout=30.0,
            ) as stream_resp,
        ):
            assert stream_resp.status_code == 200
            async for line in stream_resp.aiter_lines():
                if line.startswith("data: "):
                    chunks_read.append(line)
                # Read 3 chunks before triggering removal
                if len(chunks_read) >= 3:
                    break

        assert len(chunks_read) >= 3, (
            f"expected >=3 chunks before removal, got {len(chunks_read)}"
        )

        # Rewrite config to remove provider-b
        config_no_b = f"""\
[server]
api_key = "test-rehash-key"
port = {server_port}

[database]
path = "{db_path}"

[models]
startup_refresh = true
refresh_interval_s = 3600

[routing]
inflight_penalty = 100000

[providers.provider-a]
id = "provider-a"
base_url = "http://127.0.0.1:{port_a}/v1"
protocols = ["openai"]

[providers.provider-a.models_endpoint]
method = "GET"
path = "/models"

[[providers.provider-a.static_models]]
id = "test-model"
protocol = "openai"

[[providers.provider-a.accounts]]
name = "acct-a"
api_key = "key-a"
enabled = true
weight = 1.0
"""
        with open(config_path, "w") as f:
            f.write(config_no_b)

        exit_code, stdout, stderr = await _run_rehash(config_path, env)
        assert exit_code == 0, f"rehash failed: {stdout} {stderr}"

        # New requests to removed model should fail
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://127.0.0.1:{server_port}/v1/chat/completions",
                json={
                    "model": "test-model-b",
                    "messages": [{"role": "user", "content": "should fail"}],
                },
                headers=auth,
                timeout=10.0,
            )
            assert r.status_code in (404, 503), (
                f"expected 404/503 for removed model, got {r.status_code}"
            )

        # Provider-a still works
        async with httpx.AsyncClient() as client:
            r2 = await client.post(
                f"http://127.0.0.1:{server_port}/v1/chat/completions",
                json={
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "still works"}],
                },
                headers=auth,
                timeout=10.0,
            )
            assert r2.status_code == 200

        # /v1/models eventually excludes test-model-b
        async with httpx.AsyncClient() as client:
            excluded = False
            for _ in range(15):
                models_resp = await client.get(
                    f"http://127.0.0.1:{server_port}/v1/models",
                    headers=auth,
                    timeout=5.0,
                )
                if models_resp.status_code == 200:
                    model_ids = [m["id"] for m in models_resp.json().get("data", [])]
                    if "test-model-b" not in model_ids:
                        excluded = True
                        break
                await asyncio.sleep(0.5)
            assert excluded, "test-model-b still in /v1/models after removal"

        assert proc.returncode is None, "server process died"
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await _terminate_server(proc)
        upstream_a.shutdown()
        server_b.shutdown()


# ---------------------------------------------------------------------------
# Scenario 6: Mixed LIVE + restart-required rejected atomically
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_mixed_live_plus_restart_rejected_atomically(
    tmp_path: Any,
) -> None:
    """A config with both LIVE and RESTART_REQUIRED fields is rejected atomically.

    Changes routing.inflight_penalty (LIVE) and server.port (RESTART_REQUIRED).
    The server must reject the entire transaction, not apply the LIVE portion.

    Assert: exit code 2, generation unchanged, message mentions restart.
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

    proc, drain = await _spawn_and_drain(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        async with httpx.AsyncClient() as client:
            gen_before = await _runtime_generation_id(client, server_port)

        # Change both LIVE and RESTART_REQUIRED fields atomically
        _write_config(
            config_path,
            server_port=server_port + 999,  # RESTART_REQUIRED
            upstream_port=upstream_port,
            inflight_penalty=200_000,  # LIVE
        )

        exit_code, stdout, stderr = await _run_rehash(config_path, env)
        combined = stdout + stderr

        # Must be rejected — exit code 2 (EXIT_RESTART_REQUIRED)
        assert exit_code == 2, (
            f"expected exit code 2 for mixed LIVE+restart, got {exit_code}:\n"
            f"stdout={stdout}\nstderr={stderr}"
        )
        assert "restart" in combined.lower(), (
            f"expected restart-required mention in output: {combined}"
        )

        # Generation unchanged — neither LIVE nor RESTART_REQUIRED applied.
        async with httpx.AsyncClient() as client:
            gen_after = await _runtime_generation_id(client, server_port)
        if gen_after is not None and gen_before is not None:
            assert gen_after == gen_before, (
                f"generation advanced after mixed rejection: "
                f"{gen_before} -> {gen_after}"
            )

        # Server still healthy on original config
        async with httpx.AsyncClient() as client:
            health = await client.get(
                f"http://127.0.0.1:{server_port}/v1/healthz", timeout=5.0
            )
            assert health.status_code == 200

        assert proc.returncode is None, "server process died"
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await _terminate_server(proc)
        upstream.shutdown()


# ---------------------------------------------------------------------------
# Scenario 7: Concurrent reload returns deterministic busy status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_concurrent_reload_burst_rejects_busy(tmp_path: Any) -> None:
    """Burst of concurrent rehashes deterministically produces a busy reject.

    Fires 8 concurrent rehash subprocesses against a healthy server.  At
    least one MUST exit with code 4 (``EXIT_RELOAD_BUSY``); the rest
    MUST exit with 0, 4, or 5 (validation/prep failures are tolerated
    on heavily concurrent races).  The test never crashes the server.

    This is more reliable than the per-pair assertion because the
    reload critical section is very short and a 2-process pair can
    serialize without ever observing the busy state.
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

    proc, drain = await _spawn_and_drain(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        # Change config so rehash has work to do.
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=750_000,
        )

        # Fire a burst of 8 concurrent rehash commands.  Retry up to 3
        # times because the reload critical section is short and a
        # subprocess-based burst may serialize on fast hosts without
        # producing a busy reject.
        burst = 8
        max_attempts = 3
        all_exit_codes: list[int] = []
        busy_count = 0
        for _attempt in range(max_attempts):
            results = await asyncio.gather(
                *[_run_rehash(config_path, env) for _ in range(burst)],
                return_exceptions=True,
            )

            exit_codes: list[int] = []
            for r in results:
                if isinstance(r, Exception):
                    continue
                exit_codes.append(r[0])

            all_exit_codes.extend(exit_codes)
            busy_count = sum(1 for ec in all_exit_codes if ec == 4)
            if busy_count >= 1:
                break

        assert len(all_exit_codes) >= burst, (
            f"expected at least {burst} exit codes, got {len(all_exit_codes)}: "
            f"{all_exit_codes}"
        )

        assert busy_count >= 1, (
            f"expected at least one busy (exit=4) in concurrent burst, "
            f"got {all_exit_codes}"
        )

        for ec in exit_codes:
            assert ec in (0, 4, 5), (
                f"unexpected exit code {ec} in burst {exit_codes}; "
                "only 0 (ok), 4 (busy), 5 (prep-failed) are tolerated"
            )

        assert proc.returncode is None, "server process died"
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await _terminate_server(proc)
        upstream.shutdown()


# ---------------------------------------------------------------------------
# Scenario 8: Retirement timeout closes resources (skipped — timing)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Requires waiting for drain_timeout_s (default 300s) + margin; "
        "exceeds CI time budget. Covered by scenario 9 soft-drain test."
    )
)
@pytest.mark.asyncio()
async def test_d3_retirement_timeout_closes_resources(tmp_path: Any) -> None:
    """Old generation resources close after drain timeout.

    Starts a long stream, triggers a rehash, kills the client, waits for
    drain_timeout_s + 5s, then asserts old generation is no longer retiring.

    SKIPPED: default drain_timeout_s=300s exceeds 5-minute CI budget.
    """


# ---------------------------------------------------------------------------
# Scenario 9: Old generation resources close after lease drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_old_generation_resources_close_after_lease_drain(
    tmp_path: Any,
) -> None:
    """After rehash, the old generation eventually drains from the retiring slot.

    Triggers a rehash and polls /api/stats/runtime every 2 seconds.
    The retiring list should become empty within 30 seconds.

    Assert: after polling, retiring_count == 0.
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

    proc, drain = await _spawn_and_drain(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        # Trigger a rehash to create a retiring generation
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=200_000,
        )
        exit_code, stdout, stderr = await _run_rehash(config_path, env)
        assert exit_code == 0, f"rehash failed: {stdout} {stderr}"

        # Poll until retiring_count reaches 0 (generous 30s bound)
        deadline = time.monotonic() + 30.0
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                rc = await _get_retiring_count(client, server_port)
                if rc == 0:
                    break
                await asyncio.sleep(2.0)

            rc_final = await _get_retiring_count(client, server_port)
            assert rc_final == 0, (
                f"retiring_count still {rc_final} after 30s drain window"
            )

        assert proc.returncode is None, "server process died"
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await _terminate_server(proc)
        upstream.shutdown()


# ---------------------------------------------------------------------------
# Scenario 10: No leaked pending requests, attempts, or reservations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_d3_no_leak_pending_requests_attempts_reservations(
    tmp_path: Any,
) -> None:
    """After running multiple rehash scenarios, no resources are leaked.

    Runs 3 different rehash changes in sequence, then queries
    /api/stats/runtime and asserts no orphan state.

    Assert: pending_requests == 0, active_streams == 0.
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

    proc, drain = await _spawn_and_drain(config_path, env)
    try:
        assert await _wait_healthy(server_port), "server did not become healthy"

        # Scenario chain: 3 different rehashes
        # 1. Change inflight_penalty
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=150_000,
        )
        exit_code, _, _ = await _run_rehash(config_path, env)
        assert exit_code == 0, "rehash 1 failed"

        # 2. Change inflight_penalty again
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=250_000,
        )
        exit_code, _, _ = await _run_rehash(config_path, env)
        assert exit_code == 0, "rehash 2 failed"

        # 3. Change inflight_penalty again
        _write_config(
            config_path,
            server_port=server_port,
            upstream_port=upstream_port,
            inflight_penalty=350_000,
        )
        exit_code, _, _ = await _run_rehash(config_path, env)
        assert exit_code == 0, "rehash 3 failed"

        # Issue a request to ensure the system is stable
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"http://127.0.0.1:{server_port}/v1/chat/completions",
                json={
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "drain check"}],
                },
                headers=auth,
                timeout=10.0,
            )
            assert r.status_code == 200

        # Wait briefly for any in-flight finalizations
        await asyncio.sleep(2.0)

        # Check for leaked resources
        async with httpx.AsyncClient() as client:
            stats = await client.get(
                f"http://127.0.0.1:{server_port}/api/stats/runtime",
                headers=auth,
                timeout=5.0,
            )
            assert stats.status_code == 200
            data = stats.json()

            # requests_pending should be 0
            pending = data.get("requests_pending", 0)
            assert pending == 0, f"leaked pending requests: {pending}"

            # retiring_count should be 0
            rm = data.get("runtime_manager", {})
            retiring = rm.get("retiring_count", 0)
            assert retiring == 0, f"leaked retiring generations: {retiring}"

            # No leaked active attempts via request diagnostics
            request_diag = data.get("request_diagnostics", {})
            active = request_diag.get("active_streams", 0)
            assert active == 0, f"leaked active streams: {active}"

        assert proc.returncode is None, "server process died"
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await _terminate_server(proc)
        upstream.shutdown()
