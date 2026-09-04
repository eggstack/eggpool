"""Contract tests for the migration harness itself."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from tests.migration_rs.harness import (
    CommandObservation,
    Implementation,
    ImplementationIdentity,
    ProcessRunner,
    PythonLauncher,
    RustLauncher,
    StaticObservation,
    StubResponse,
    allocate_tcp_port,
    assert_distinct_implementations,
    capture_config,
    capture_database,
    capture_html,
    compare_observations,
    isolated_environment,
    normalize_observation,
    observe_http,
)


def test_distinct_implementation_guard_and_real_process_identity() -> None:
    python = PythonLauncher()
    rust = RustLauncher()
    assert_distinct_implementations(python, rust)
    if not rust.identity.executable.is_file():
        pytest.skip("Rust candidate is not built")

    with isolated_environment() as environment:
        python_result = python.run(["version"], environment=environment)
        rust_result = rust.run(["version"], environment=environment)

    assert python_result.identity.implementation is Implementation.PYTHON
    assert rust_result.identity.implementation is Implementation.RUST
    assert python_result.pid != rust_result.pid
    assert python_result.exit_code == 0
    assert rust_result.exit_code == 0


def test_same_implementation_or_executable_is_rejected(tmp_path: Path) -> None:
    executable = tmp_path / "candidate"
    executable.touch()
    python = PythonLauncher()
    same_kind = type("FakeLauncher", (), {})()
    same_kind.identity = ImplementationIdentity(Implementation.PYTHON, executable)
    with pytest.raises(AssertionError, match="implementation identities"):
        assert_distinct_implementations(python, same_kind)

    rust = RustLauncher(executable=python.identity.executable)
    with pytest.raises(AssertionError, match="executable paths"):
        assert_distinct_implementations(python, rust)


def test_timeout_terminates_process_group() -> None:
    identity = ImplementationIdentity(Implementation.PYTHON, Path(sys.executable))
    result = ProcessRunner().run(
        identity,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=Path.cwd(),
        env=os.environ,
        timeout=0.1,
    )
    assert result.timed_out
    assert result.exit_code is None
    with pytest.raises(ProcessLookupError):
        os.kill(result.pid, 0)


def test_isolated_environment_has_no_project_state() -> None:
    with isolated_environment() as environment:
        assert environment.home != Path.home()
        assert not (environment.home / ".config" / "eggpool").exists()
        (environment.state_home / "probe").write_text("x", encoding="utf-8")
        assert (environment.state_home / "probe").is_file()
    assert not environment.root.exists()


def test_normalization_removes_only_explicit_ephemeral_fields(tmp_path: Path) -> None:
    identity = ImplementationIdentity(Implementation.PYTHON, Path("/python"))
    raw = CommandObservation(
        identity=identity,
        argv=("eggpool", "--config", str(tmp_path / "config.toml")),
        exit_code=0,
        stdout=f"loaded {tmp_path / 'config.toml'}\ncontract-field=keep\n",
        stderr="",
        timed_out=False,
        pid=1234,
        duration_ms=22,
    )
    normalized = normalize_observation(raw, temp_roots=(tmp_path,))
    assert normalized["args"][-2:] == ["--config", "<TEMP_ROOT>/config.toml"]
    assert "<TEMP_ROOT>/config.toml" in normalized["stdout"]
    assert "contract-field=keep" in normalized["stdout"]
    assert "pid" not in normalized
    assert "duration_ms" not in normalized


def test_contractual_json_field_change_fails_comparison() -> None:
    identity = ImplementationIdentity(Implementation.PYTHON, Path("/python"))
    expected = observe_http_from_values(identity, {"status": "ok", "new": False})
    actual = observe_http_from_values(identity, {"status": "ok", "new": True})
    with pytest.raises(AssertionError, match="differential observation mismatch"):
        compare_observations(expected, actual)


def test_contractual_cli_exit_code_change_fails_comparison() -> None:
    identity = ImplementationIdentity(Implementation.PYTHON, Path("/python"))
    expected = CommandObservation(identity, ("eggpool",), 0, "", "", False, 1, 1)
    actual = CommandObservation(identity, ("eggpool",), 1, "", "", False, 2, 1)
    with pytest.raises(AssertionError, match="differential observation mismatch"):
        compare_observations(expected, actual)


def test_html_normalization_retains_text_and_dom_changes() -> None:
    identity = ImplementationIdentity(Implementation.PYTHON, Path("/python"))
    expected = capture_html(identity, "/", "<main><h1>EggPool</h1></main>")
    whitespace_variant = capture_html(identity, "/", "<main> <h1>EggPool</h1> </main>")
    changed_text = capture_html(identity, "/", "<main><h1>Different</h1></main>")
    with pytest.raises(AssertionError):
        compare_observations(expected, whitespace_variant)
    with pytest.raises(AssertionError):
        compare_observations(expected, changed_text)


def test_stub_http_drains_without_persisting_request_body() -> None:
    from tests.migration_rs.harness import StubHttpServer

    identity = ImplementationIdentity(Implementation.PYTHON, Path("/python"))
    with StubHttpServer(
        {("POST", "/probe"): StubResponse(body=b'{"status":"ok"}')}
    ) as server:
        observation = observe_http(
            identity,
            f"{server.base_url}/probe",
            method="POST",
            body=b"secret request content",
        )
    assert observation.status == 200
    assert observation.body == '{"status":"ok"}'
    assert server.requests[0].body_length == len(b"secret request content")
    assert not hasattr(server.requests[0], "body")


def test_python_config_and_database_observations_are_structured(tmp_path: Path) -> None:
    config_path = tmp_path / "valid.toml"
    config_path.write_text(
        """
[server]
host = "127.0.0.1"
port = 11301

[database]
path = "migration.sqlite3"

[models]
startup_refresh = false

[model_info]
enabled = false
startup_refresh = false
""".lstrip(),
        encoding="utf-8",
    )
    with isolated_environment() as environment:
        result = capture_config(PythonLauncher(), config_path, environment)
    assert result.valid
    assert result.error_category is None
    assert result.command.identity.implementation is Implementation.PYTHON
    invalid = capture_config(
        PythonLauncher(),
        Path("tests/migration_rs/fixtures/config/invalid-port.toml"),
        environment,
    )
    assert not invalid.valid
    assert invalid.error_category == "schema"

    database_path = tmp_path / "observation.sqlite3"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE contract (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO contract (value) VALUES ('fixture')")
        connection.commit()
    finally:
        connection.close()
    observation = capture_database(
        ImplementationIdentity(Implementation.PYTHON, Path("/python")),
        database_path,
    )
    assert observation.row_counts == (("contract", 1),)
    assert observation.tables[0]["name"] == "contract"


def test_python_migration_creates_seed_schema_observation() -> None:
    with isolated_environment() as environment:
        config_path = environment.root / "migration.toml"
        database_path = environment.root / "migration.sqlite3"
        config_path.write_text(
            f"""[server]
host = "127.0.0.1"
port = 11301

[database]
path = "{database_path}"

[models]
startup_refresh = false

[model_info]
enabled = false
startup_refresh = false
""",
            encoding="utf-8",
        )
        result = PythonLauncher().run(
            ["--config", str(config_path), "migrate"],
            environment=environment,
        )
        assert result.exit_code == 0, result.stderr
        observation = capture_database(
            result.identity,
            database_path,
            checksum_path=(
                PythonLauncher().repository
                / "src"
                / "eggpool"
                / "db"
                / "schema"
                / "checksums.json"
            ),
        )
    assert observation.user_version == 0
    assert len(observation.migration_checksums) == 54
    assert ("_migrations", 54) in observation.row_counts


def test_python_server_health_is_a_black_box_observation() -> None:
    port = allocate_tcp_port()
    with isolated_environment() as environment:
        config_path = environment.root / "server.toml"
        database_path = environment.root / "server.sqlite3"
        config_path.write_text(
            f"""[server]
host = "127.0.0.1"
port = {port}

[database]
path = "{database_path}"

[models]
startup_refresh = false

[model_info]
enabled = false
startup_refresh = false

[dashboard]
enabled = false
""",
            encoding="utf-8",
        )
        launcher = PythonLauncher()
        with launcher.spawn(
            ["--config", str(config_path), "serve", "--verbose"],
            environment=environment,
        ) as server:
            from tests.migration_rs.harness import wait_for_tcp

            wait_for_tcp("127.0.0.1", port)
            observation = observe_http(
                server.identity, f"http://127.0.0.1:{port}/v1/healthz"
            )
    assert observation.status == 200
    assert observation.body == '{"status":"ok"}'


def test_sse_observation_preserves_native_frame_grammar() -> None:
    from tests.migration_rs.harness import StubHttpServer

    identity = ImplementationIdentity(Implementation.PYTHON, Path("/python"))
    with StubHttpServer(
        {
            ("GET", "/events"): StubResponse(
                body=b'event: message\nid: 7\ndata: {"step":1}\ndata: done\n\n',
                headers=(("content-type", "text/event-stream"),),
            )
        }
    ) as server:
        observation = observe_http(identity, f"{server.base_url}/events")
    assert observation.sse_frames[0].event == "message"
    assert observation.sse_frames[0].event_id == "7"
    assert observation.sse_frames[0].data == ('{"step":1}', "done")


def test_static_observation_hashes_exact_python_asset_bytes() -> None:
    launcher = PythonLauncher()
    asset = (
        launcher.repository / "src" / "eggpool" / "dashboard" / "static" / "favicon.svg"
    )
    observation = StaticObservation.from_bytes(
        ImplementationIdentity(Implementation.PYTHON, launcher.identity.executable),
        "/static/favicon.svg",
        "image/svg+xml",
        asset.read_bytes(),
    )
    assert observation.sha256 == (
        "593a7d0f464160e832f62399cf224a04dda68a55f3a6ce847dffb62016d37653"
    )


def observe_http_from_values(
    identity: ImplementationIdentity, value: dict[str, object]
) -> object:
    """Build an HTTP observation without making network traffic in unit tests."""
    import json

    from tests.migration_rs.harness import HttpObservation

    return HttpObservation(
        identity,
        "GET",
        "/v1/healthz",
        200,
        (("content-type", "application/json"),),
        json.dumps(value, sort_keys=True),
    )
