"""Black-box acceptance tests for the F005 Rust read-plane server."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from tests.migration_rs.harness import (
    PythonLauncher,
    RustLauncher,
    allocate_tcp_port,
    isolated_environment,
    observe_http,
    wait_for_tcp,
)


def _write_config(root: Path, port: int, *, public: bool = True) -> Path:
    database = root / "server.sqlite3"
    config = root / "server.toml"
    config.write_text(
        f"""[server]
host = "127.0.0.1"
port = {port}
api_key = "server-key"
max_request_body_bytes = 32

[database]
path = "{database}"

[dashboard]
enabled = true
public = {str(public).lower()}
theme = "Cyber Red"

[models]
startup_refresh = false

[model_info]
enabled = false
startup_refresh = false
""",
        encoding="utf-8",
    )
    return config


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def test_health_is_identical_for_python_and_rust() -> None:
    rust = RustLauncher()
    if not rust.identity.executable.is_file():
        pytest.skip("Rust candidate is not built")

    observations = []
    with isolated_environment() as environment:
        for launcher in (PythonLauncher(), rust):
            port = allocate_tcp_port()
            config_root = environment.implementation_root(
                launcher.identity.implementation
            )
            config = _write_config(config_root, port)
            with launcher.spawn(
                ["--config", str(config), "serve", "--verbose"],
                environment=environment,
            ) as _server:
                wait_for_tcp("127.0.0.1", port)
                observations.append(
                    observe_http(
                        launcher.identity,
                        _url(port, "/v1/healthz"),
                    )
                )
        assert observations[0].status == observations[1].status == 200
        assert observations[0].body == observations[1].body == '{"status":"ok"}'


def test_rust_auth_body_limit_summary_ssr_and_shutdown() -> None:
    rust = RustLauncher()
    if not rust.identity.executable.is_file():
        pytest.skip("Rust candidate is not built")

    with isolated_environment() as environment:
        port = allocate_tcp_port()
        config = _write_config(environment.root, port, public=False)
        with rust.spawn(
            ["--config", str(config), "serve", "--verbose"],
            environment=environment,
        ) as server:
            wait_for_tcp("127.0.0.1", port)
            unauthorized = observe_http(
                rust.identity,
                _url(port, "/"),
            )
            assert unauthorized.status == 401
            assert json.loads(unauthorized.body) == {
                "detail": "Invalid or missing API key"
            }
            readiness = observe_http(rust.identity, _url(port, "/v1/readyz"))
            assert readiness.status == 503
            assert json.loads(readiness.body) == {
                "status": "degraded",
                "reason": "no accounts configured",
            }

            headers = {"Authorization": "Bearer server-key"}
            page = observe_http(
                rust.identity,
                _url(port, "/"),
                headers=headers,
            )
            assert page.status == 200
            assert '<main id="dashboard-content">' in page.body
            assert "<h2>Overview</h2>" in page.body
            assert "Account breakdown" in page.body
            assert "server-key" not in page.body

            summary = observe_http(
                rust.identity,
                _url(port, "/api/stats/summary?period=24h"),
                headers=headers,
            )
            assert summary.status == 200
            summary_payload = json.loads(summary.body)
            assert summary_payload["period"] == "24h"
            assert summary_payload["total_requests"] == 0
            assert summary_payload["cache_read_ratio"] is None

            malformed = observe_http(
                rust.identity,
                _url(port, "/api/stats/summary?period=not-a-period"),
                headers=headers,
            )
            assert malformed.status == 400

            oversized = observe_http(
                rust.identity,
                _url(port, "/v1/chat/completions"),
                method="POST",
                headers={**headers, "Content-Type": "application/json"},
                body=b"x" * 64,
            )
            assert oversized.status == 413

            static = observe_http(rust.identity, _url(port, "/static/dashboard.css"))
            source = (
                Path(__file__).parents[2]
                / "src"
                / "eggpool"
                / "dashboard"
                / "static"
                / "dashboard.css"
            ).read_bytes()
            assert static.status == 200
            assert (
                hashlib.sha256(static.body.encode()).hexdigest()
                == hashlib.sha256(source).hexdigest()
            )
            assert "text/css" in dict(static.headers)["content-type"]

            server.stop()
            deadline = time.monotonic() + 5
            while server.process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.process.poll() is not None

        # The listener was released by graceful shutdown and can be reused.
        replacement_port = allocate_tcp_port()
        assert replacement_port > 0


def test_rust_bind_failure_is_reported_without_a_second_listener() -> None:
    rust = RustLauncher()
    if not rust.identity.executable.is_file():
        pytest.skip("Rust candidate is not built")

    with isolated_environment() as environment:
        port = allocate_tcp_port()
        first_root = environment.root / "first"
        second_root = environment.root / "second"
        first_root.mkdir()
        second_root.mkdir()
        first_config = _write_config(first_root, port)
        second_config = _write_config(second_root, port)
        with rust.spawn(
            ["--config", str(first_config), "serve", "--verbose"],
            environment=environment,
        ) as _first:
            wait_for_tcp("127.0.0.1", port)
            second = rust.run(
                ["--config", str(second_config), "serve", "--verbose"],
                environment=environment,
                timeout=5,
            )
            assert second.exit_code == 1
            assert "cannot bind listener" in second.stderr
