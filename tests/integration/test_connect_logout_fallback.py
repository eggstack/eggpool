"""Integration test for safe connect/logout fallback policy.

Proves that ``eggpool rehash`` (and by extension ``connect``/``logout``)
never silently restarts a healthy server when the control socket is
unavailable.  The test:

1. Spawns a healthy server
2. Removes the control socket file (simulating a missing/stale socket)
3. Writes a valid LIVE config change
4. Runs ``eggpool rehash`` subprocess
5. Asserts exit code is EXIT_CONTROL_UNAVAILABLE (3)
6. Asserts the server is still alive (PID unchanged)
7. Asserts ``/v1/healthz`` still responds 200
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Mock upstream HTTP server
# ---------------------------------------------------------------------------


class _MockUpstreamHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible upstream handler."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_GET(self) -> None:
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            body = json.dumps(
                {"object": "list", "data": [{"id": "test-model"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") in (
            "/v1/chat/completions",
            "/chat/completions",
        ):
            self.send_error(501)
        else:
            self.send_error(404)


def _start_mock_upstream() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _MockUpstreamHandler)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config_body(upstream_port: int, server_port: int, db_path: str) -> str:
    return (
        "[server]\n"
        f"port = {server_port}\n"
        'host = "127.0.0.1"\n'
        'api_key = "ep_test_server_key_1234567890"\n'
        "\n"
        "[database]\n"
        f'path = "{db_path}"\n'
        "wal = true\n"
        "busy_timeout_ms = 5000\n"
        "\n"
        "[providers.test-provider]\n"
        'id = "test-provider"\n'
        f'base_url = "http://127.0.0.1:{upstream_port}/v1"\n'
        'protocols = ["openai"]\n'
        "\n"
        "[providers.test-provider.models_endpoint]\n"
        'method = "GET"\npath = "/models"\n'
        "\n"
        "[[providers.test-provider.accounts]]\n"
        'name = "default"\n'
        'api_key = "sk-test-account-key-1234567890"\n'
        "enabled = true\n"
        "weight = 1.0\n"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRehashDoesNotRestartHealthyServer:
    """eggpool rehash must not silently restart when socket is missing."""

    def test_exit_code_3_when_socket_gone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        upstream = _start_mock_upstream()
        upstream_port = upstream.server_address[1]

        server_port = _free_port()
        config_path = tmp_path / "config.toml"
        db_path = tmp_path / "usage.sqlite3"
        config_path.write_text(
            _config_body(upstream_port, server_port, str(db_path)),
            encoding="utf-8",
        )

        # Use a private HOME so ``runtime_paths.state_dir()`` resolves to
        # ``<tmp_path>/.local/state/eggpool`` and the control socket is
        # isolated from the real user state. Pin the PID file path so the
        # test process and subprocess agree on its location. ``monkeypatch``
        # restores both variables after the test so subsequent tests are
        # not affected.
        fake_home = tmp_path / "home"
        fake_home.mkdir(parents=True, exist_ok=True)
        pid_file = tmp_path / "eggpool.pid"
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("EGGPOOL_PID_FILE", str(pid_file))
        env = os.environ.copy()
        env["HOME"] = str(fake_home)
        env["EGGPOOL_PID_FILE"] = str(pid_file)

        # Start the server
        proc = subprocess.Popen(  # noqa: S603,S602
            [
                sys.executable,
                "-m",
                "eggpool",
                "--config",
                str(config_path),
                "serve",
                "--verbose",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            # Wait for the server to become healthy
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    r = httpx.get(
                        f"http://127.0.0.1:{server_port}/v1/healthz",
                        timeout=1.0,
                    )
                    if r.status_code == 200:
                        break
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass
                time.sleep(0.5)
            else:
                pytest.fail("Server did not become healthy within 30s")

            # Read the PID file to confirm the server is alive
            from eggpool.runtime import read_pid

            original_pid = read_pid()
            assert original_pid is not None, "PID file not written"

            # Remove the control socket to simulate it being missing
            from eggpool.runtime_paths import runtime_dir

            sock_path = runtime_dir() / "eggpool.sock"
            if sock_path.exists():
                sock_path.unlink()

            # Now run rehash — it should NOT restart the server
            rehash_result = subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "eggpool",
                    "--config",
                    str(config_path),
                    "rehash",
                ],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )

            # Exit code should be EXIT_CONTROL_UNAVAILABLE (3)
            assert rehash_result.returncode == 3, (
                f"Expected exit code 3, got {rehash_result.returncode}.\n"
                f"stdout: {rehash_result.stdout}\n"
                f"stderr: {rehash_result.stderr}"
            )

            # Server should still be alive with the same PID
            current_pid = read_pid()
            assert current_pid == original_pid, (
                f"Server PID changed from {original_pid} to {current_pid} — "
                "the server was silently restarted!"
            )

            # Healthz should still respond 200
            r = httpx.get(
                f"http://127.0.0.1:{server_port}/v1/healthz",
                timeout=2.0,
            )
            assert r.status_code == 200

        finally:
            # Clean up the server
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            upstream.shutdown()
