"""Migration-wide Q002 scenarios that compose the existing black-box seams."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING

import pytest

from tests.migration_rs.harness import (
    PythonLauncher,
    RustLauncher,
    StubHttpServer,
    StubResponse,
    allocate_tcp_port,
    capture_database,
    isolated_environment,
    observe_http,
    wait_for_tcp,
)

if TYPE_CHECKING:
    from pathlib import Path

    from tests.migration_rs.harness import IsolatedEnvironment


MODEL = "q002-fixture-model"
SERVER_KEY = "q002-server-key"


def _rust_or_skip() -> RustLauncher:
    rust = RustLauncher()
    if not rust.identity.executable.is_file():
        pytest.skip("Rust candidate is not built")
    return rust


def _config(
    root: Path,
    port: int,
    database: Path,
    upstream: str,
    *,
    provider_count: int = 1,
) -> Path:
    providers: list[str] = []
    for index in range(provider_count):
        provider_id = f"fixture-{index}"
        providers.append(
            f"""[providers.{provider_id}]
id = "{provider_id}"
base_url = "{upstream}"
protocols = ["openai"]

[providers.{provider_id}.auth]
mode = "none"

[providers.{provider_id}.models_endpoint]
method = "GET"

[[providers.{provider_id}.static_models]]
id = "{MODEL}"
protocol = "openai"

[[providers.{provider_id}.accounts]]
name = "fixture-account-{index}"
"""
        )
    path = root / "q002.toml"
    path.write_text(
        f"""[server]
host = "127.0.0.1"
port = {port}
api_key = "{SERVER_KEY}"

[database]
path = "{database}"

[dashboard]
enabled = false

[models]
startup_refresh = true

[model_info]
enabled = false
startup_refresh = false

{"".join(providers)}
""",
        encoding="utf-8",
    )
    return path


def _seed_catalog_model(database: Path, provider_id: str = "fixture-0") -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO models "
            "(model_id, protocol, resolution_status, provider_id) "
            "VALUES (?, 'openai', 'resolved', ?)",
            (MODEL, provider_id),
        )
        connection.commit()


def _client_body(stream: bool = False) -> bytes:
    value: dict[str, object] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "q002"}],
    }
    if stream:
        value["stream"] = True
    return json.dumps(value).encode()


def _finite_response() -> StubResponse:
    return StubResponse(
        body=json.dumps(
            {
                "id": "fixture-response",
                "model": MODEL,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                },
            }
        ).encode(),
    )


def _stream_response() -> StubResponse:
    return StubResponse(
        body=(
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1,'
            b'"total_tokens":3}}\n\n'
            b"data: [DONE]\n\n"
        ),
        headers=(("content-type", "text/event-stream"),),
    )


def _run_lifecycle(
    launcher: PythonLauncher | RustLauncher,
    environment: IsolatedEnvironment,
    config: Path,
    port: int,
) -> tuple[dict[str, object], object]:
    runtime_root = environment.implementation_root(launcher.identity.implementation)
    env_overrides = {
        "EGGPOOL_RUNTIME_DIR": str(runtime_root / "runtime"),
        "EGGPOOL_PID_FILE": str(runtime_root / "eggpool.pid"),
    }
    with launcher.spawn(
        ["--config", str(config), "serve", "--verbose"],
        environment=environment,
        env_overrides=env_overrides,
    ) as bootstrap:
        wait_for_tcp("127.0.0.1", port)
        bootstrap_health = observe_http(
            launcher.identity, f"http://127.0.0.1:{port}/v1/healthz"
        )
        assert bootstrap_health.status == 200
        bootstrap.stop()

    _seed_catalog_model(environment.database_path(launcher.identity.implementation))

    with launcher.spawn(
        ["--config", str(config), "serve", "--verbose"],
        environment=environment,
        env_overrides=env_overrides,
    ) as server:
        wait_for_tcp("127.0.0.1", port)
        headers = {"Authorization": f"Bearer {SERVER_KEY}"}
        health = observe_http(launcher.identity, f"http://127.0.0.1:{port}/v1/healthz")
        readiness = observe_http(
            launcher.identity, f"http://127.0.0.1:{port}/v1/readyz"
        )
        finite = observe_http(
            launcher.identity,
            f"http://127.0.0.1:{port}/v1/chat/completions",
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
            body=_client_body(),
        )
        streaming = observe_http(
            launcher.identity,
            f"http://127.0.0.1:{port}/v1/chat/completions",
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
            body=_client_body(True),
        )
        runtime = observe_http(
            launcher.identity,
            f"http://127.0.0.1:{port}/api/stats/runtime",
            headers=headers,
        )
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                'api_key = "q002-server-key"',
                'api_key = "q002-server-key"\nmax_request_body_bytes = 4096',
            ),
            encoding="utf-8",
        )
        rehash = launcher.run(
            ["--config", str(config), "rehash", "--json"],
            environment=environment,
            env_overrides=env_overrides,
        )
        server.stop()
        deadline = time.monotonic() + 5
        while server.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.process.poll() is not None
    return (
        {
            "health": (health.status, health.body),
            "readiness": (readiness.status, readiness.body),
            "finite": (finite.status, finite.body),
            "streaming": (
                streaming.status,
                tuple(frame.to_dict() for frame in streaming.sse_frames),
            ),
            "runtime_status": runtime.status,
            "rehash_exit": rehash.exit_code,
        },
        server,
    )


def test_fresh_local_deployment_lifecycle_is_two_sided_and_durable() -> None:
    rust = _rust_or_skip()
    observations: list[dict[str, object]] = []
    databases: list[Path] = []
    with isolated_environment() as environment:
        for launcher in (PythonLauncher(), rust):
            root = environment.implementation_root(launcher.identity.implementation)
            port = allocate_tcp_port()

            def route(_request: object) -> StubResponse:
                return _finite_response()

            with StubHttpServer(
                {
                    ("GET", "/models"): StubResponse(
                        body=json.dumps({"data": [{"id": MODEL}]}).encode()
                    ),
                    ("POST", "/chat/completions"): route,
                }
            ) as upstream:
                port = allocate_tcp_port()
                config = _config(
                    root,
                    port,
                    environment.database_path(launcher.identity.implementation),
                    upstream.base_url,
                )
                observation, _server = _run_lifecycle(
                    launcher, environment, config, port
                )
                observations.append(observation)
            databases.append(
                environment.database_path(launcher.identity.implementation)
            )
        assert observations[0] == observations[1]
        assert observations[0]["finite"]  # finite path was exercised
        assert observations[0]["streaming"]  # streaming path was exercised
        snapshots = [
            capture_database(launcher.identity, path)
            for launcher, path in zip((PythonLauncher(), rust), databases, strict=True)
        ]
        assert snapshots[0].user_version == snapshots[1].user_version
        assert snapshots[0].tables == snapshots[1].tables
        for table in (
            "accounts",
            "models",
            "providers",
            "requests",
            "request_attempts",
            "reservations",
        ):
            counts = [dict(snapshot.row_counts).get(table) for snapshot in snapshots]
            assert counts[0] == counts[1], (table, counts)


@pytest.mark.parametrize("stream", [False, True])
def test_provider_failure_ownership_and_no_post_handoff_replay(stream: bool) -> None:
    rust = _rust_or_skip()
    launchers = (PythonLauncher(), rust)
    for launcher in launchers:
        with isolated_environment() as environment:
            root = environment.implementation_root(launcher.identity.implementation)
            port = allocate_tcp_port()
            calls = 0

            def route(_request: object) -> StubResponse:
                nonlocal calls
                calls += 1
                if stream:
                    return _stream_response()
                if calls == 1:
                    return StubResponse(
                        status=503,
                        body=b'{"error":{"message":"fixture","type":"server_error"}}',
                    )
                return _finite_response()

            with StubHttpServer(
                {
                    ("GET", "/models"): StubResponse(
                        body=json.dumps({"data": [{"id": MODEL}]}).encode()
                    ),
                    ("POST", "/chat/completions"): route,
                }
            ) as upstream:
                port = upstream.port + 1
                config = _config(
                    root,
                    port,
                    environment.database_path(launcher.identity.implementation),
                    upstream.base_url,
                    provider_count=2,
                )
                with launcher.spawn(
                    ["--config", str(config), "serve", "--verbose"],
                    environment=environment,
                    env_overrides={
                        "EGGPOOL_RUNTIME_DIR": str(root / "runtime"),
                        "EGGPOOL_PID_FILE": str(root / "eggpool.pid"),
                    },
                ) as _server:
                    wait_for_tcp("127.0.0.1", port)
                    health = observe_http(
                        launcher.identity, f"http://127.0.0.1:{port}/v1/healthz"
                    )
                    assert health.status == 200
                    _seed_catalog_model(
                        environment.database_path(launcher.identity.implementation)
                    )
                    body = _client_body(stream)
                    response = observe_http(
                        launcher.identity,
                        f"http://127.0.0.1:{port}/v1/chat/completions",
                        method="POST",
                        headers={
                            "Authorization": f"Bearer {SERVER_KEY}",
                            "Content-Type": "application/json",
                        },
                        body=body,
                    )
                if stream:
                    assert response.status == 200, response.body
                    assert calls == 1
                else:
                    assert response.status == 200, response.body
                    assert calls == 2


def test_restart_recovery_and_operation_mutation_keep_durable_state_bounded() -> None:
    rust = _rust_or_skip()
    for launcher in (PythonLauncher(), rust):
        with isolated_environment() as environment:
            root = environment.implementation_root(launcher.identity.implementation)
            database = environment.database_path(launcher.identity.implementation)
            port = allocate_tcp_port()
            config = _config(root, port, database, "http://127.0.0.1:9")
            env_overrides = {
                "EGGPOOL_RUNTIME_DIR": str(root / "runtime"),
                "EGGPOOL_PID_FILE": str(root / "eggpool.pid"),
            }
            migrated = launcher.run(
                ["--config", str(config), "migrate"], environment=environment
            )
            assert migrated.exit_code == 0
            with launcher.spawn(
                ["--config", str(config), "serve", "--verbose"],
                environment=environment,
                env_overrides=env_overrides,
            ) as bootstrap:
                wait_for_tcp("127.0.0.1", port)
                health = observe_http(
                    launcher.identity, f"http://127.0.0.1:{port}/v1/healthz"
                )
                assert health.status == 200
                _seed_catalog_model(database)
                bootstrap.stop()
            with sqlite3.connect(database) as connection:
                account_id = connection.execute(
                    "SELECT id FROM accounts WHERE name = ?", ("fixture-account-0",)
                ).fetchone()
                assert account_id is not None
                connection.execute(
                    "INSERT INTO requests "
                    "(account_id, model_id, status, protocol, streamed, "
                    "proxy_request_id, provider_id) VALUES (?, ?, 'pending', "
                    "'openai', 0, ?, 'fixture-0')",
                    (
                        account_id[0],
                        MODEL,
                        f"q002-{launcher.identity.implementation.value}",
                    ),
                )
                connection.commit()

            with launcher.spawn(
                ["--config", str(config), "serve", "--verbose"],
                environment=environment,
                env_overrides=env_overrides,
            ) as server:
                wait_for_tcp("127.0.0.1", port)
                health = observe_http(
                    launcher.identity, f"http://127.0.0.1:{port}/v1/healthz"
                )
                assert health.status == 200
                live = config.read_text(encoding="utf-8").replace(
                    'api_key = "q002-server-key"',
                    'api_key = "q002-server-key"\nmax_request_body_bytes = 4096',
                )
                config.write_text(live, encoding="utf-8")
                reapplied = launcher.run(
                    ["--config", str(config), "rehash", "--json"],
                    environment=environment,
                    env_overrides=env_overrides,
                )
                assert reapplied.exit_code == 0
                config.write_text(
                    live.replace(f"port = {port}", f"port = {allocate_tcp_port()}"),
                    encoding="utf-8",
                )
                restart_required = launcher.run(
                    ["--config", str(config), "rehash", "--json"],
                    environment=environment,
                    env_overrides=env_overrides,
                )
                assert restart_required.exit_code != 0
                server.stop()
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT status FROM requests WHERE proxy_request_id = ?",
                    (f"q002-{launcher.identity.implementation.value}",),
                ).fetchone()
            assert row is not None
            assert row[0] in {"interrupted", "error"}
