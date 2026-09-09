"""Q003 database, backup, recovery, and rollback qualification."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import EXPECTED_SCHEMA_VERSION, MigrationRunner
from eggpool.db.repositories import AccountRepository, RequestRepository
from eggpool.lifecycle.backup import (
    BackupContents,
    create_runtime_backup,
    parse_zip_metadata,
    restore_backup,
)
from tests.migration_rs.harness import (
    IsolatedEnvironment,
    PythonLauncher,
    RustLauncher,
    StubHttpServer,
    StubResponse,
    allocate_tcp_port,
    isolated_environment,
    observe_http,
    wait_for_tcp,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


ROOT = Path(__file__).parents[2]
HISTORICAL_FIXTURE = ROOT / "tests/fixtures/schema/pre_phase17_v11.sql"
FIXTURE_MATRIX = ROOT / "migration-rs/fixtures/qualification/q003-fixture-matrix.json"
MODEL = "q003-fixture-model"
HISTORICAL_MODEL = "historical-model"
SERVER_KEY = "q003-server-key"


def _rust_or_skip() -> RustLauncher:
    rust = RustLauncher()
    if not rust.identity.executable.is_file():
        pytest.skip("Rust candidate is not built")
    return rust


def _write_config(
    root: Path,
    database: Path,
    backup_dir: Path,
    upstream: str,
    *,
    model: str = MODEL,
    account: str = "q003-account",
) -> Path:
    config = root / "q003.toml"
    config.write_text(
        f'''[server]
host = "127.0.0.1"
port = 11300
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

[backup]
directory = "{backup_dir}"
include_env = true
retain_count = 3

[providers.fixture]
id = "fixture"
base_url = "{upstream}"
protocols = ["openai"]

[providers.fixture.auth]
mode = "none"

[providers.fixture.models_endpoint]
method = "GET"

[[providers.fixture.static_models]]
id = "{model}"
protocol = "openai"

[[providers.fixture.accounts]]
name = "{account}"
''',
        encoding="utf-8",
    )
    return config


def _finite_response(model: str) -> StubResponse:
    return StubResponse(
        body=json.dumps(
            {
                "id": "q003-response",
                "model": model,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            }
        ).encode(),
    )


def _stream_response(model: str) -> StubResponse:
    del model
    return StubResponse(
        body=(
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,'
            b'"total_tokens":5}}\n\n'
            b"data: [DONE]\n\n"
        ),
        headers=(("content-type", "text/event-stream"),),
    )


def _seed_catalog(database: Path, model: str, provider: str = "fixture") -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO models "
            "(model_id, protocol, resolution_status, provider_id) "
            "VALUES (?, 'openai', 'resolved', ?)",
            (model, provider),
        )
        connection.execute(
            "INSERT OR IGNORE INTO account_models (account_id, model_id) "
            "SELECT id, ? FROM accounts WHERE name LIKE 'q003-%' LIMIT 1",
            (model,),
        )
        connection.commit()


def _request_body(model: str, stream: bool = False) -> bytes:
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "q003"}],
    }
    if stream:
        body["stream"] = True
    return json.dumps(body).encode()


def _response_factory(model: str) -> Any:
    calls = 0

    def route(_request: object) -> StubResponse:
        nonlocal calls
        calls += 1
        return _stream_response(model) if calls > 1 else _finite_response(model)

    def reset() -> None:
        nonlocal calls
        calls = 0

    route.reset = reset
    return route


def _exercise_server_on_port(
    launcher: PythonLauncher | RustLauncher,
    environment: IsolatedEnvironment,
    config: Path,
    database: Path,
    model: str,
    upstream: StubHttpServer,
) -> tuple[tuple[int, int], tuple[int, int]]:
    port = allocate_tcp_port()
    config_lines = config.read_text(encoding="utf-8").splitlines()
    config.write_text(
        "\n".join(
            f"port = {port}" if line.startswith("port = ") else line
            for line in config_lines
        )
        + "\n",
        encoding="utf-8",
    )
    _seed_catalog(database, model)
    runtime_root = environment.implementation_root(launcher.identity.implementation)
    env = {
        "EGGPOOL_RUNTIME_DIR": str(runtime_root / "runtime"),
        "EGGPOOL_PID_FILE": str(runtime_root / "eggpool.pid"),
    }
    with launcher.spawn(
        ["--config", str(config), "serve", "--verbose"],
        environment=environment,
        env_overrides=env,
    ) as server:
        wait_for_tcp("127.0.0.1", port)
        health = observe_http(launcher.identity, f"http://127.0.0.1:{port}/v1/healthz")
        assert health.status == 200
        headers = {
            "Authorization": f"Bearer {SERVER_KEY}",
            "Content-Type": "application/json",
        }
        finite = observe_http(
            launcher.identity,
            f"http://127.0.0.1:{port}/v1/chat/completions",
            method="POST",
            headers=headers,
            body=_request_body(model),
        )
        streaming = observe_http(
            launcher.identity,
            f"http://127.0.0.1:{port}/v1/chat/completions",
            method="POST",
            headers=headers,
            body=_request_body(model, True),
        )
        assert finite.status == 200, finite.body
        assert streaming.status == 200, streaming.body
        assert streaming.sse_frames[-1].data == ("[DONE]",)
        server.stop()
    assert len(upstream.requests) >= 2
    return (finite.status, len(finite.body)), (
        streaming.status,
        len(streaming.body),
    )


def _projection(database: Path) -> dict[str, Any]:
    """Return bounded semantic rows, excluding timestamps and secrets."""
    queries = {
        "accounts": (
            "SELECT name, enabled, weight, provider_id FROM accounts ORDER BY name"
        ),
        "models": (
            "SELECT model_id, protocol, resolution_status, provider_id "
            "FROM models ORDER BY model_id"
        ),
        "requests": (
            "SELECT model_id, status, protocol, streamed, input_tokens, "
            "output_tokens, cost_microdollars FROM requests ORDER BY id"
        ),
        "attempts": (
            "SELECT attempt_number, status_code, bytes_emitted, bytes_received "
            "FROM request_attempts ORDER BY id"
        ),
        "reservations": (
            "SELECT model_id, status, estimated_tokens, release_reason "
            "FROM reservations ORDER BY id"
        ),
        "routing": (
            "SELECT model_id, provider_id, outcome FROM routing_decisions ORDER BY id"
        ),
        "rollups": (
            "SELECT model_id, request_count, input_tokens, output_tokens "
            "FROM usage_rollups ORDER BY bucket_start, model_id"
        ),
    }
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        result: dict[str, Any] = {}
        for name, query in queries.items():
            try:
                result[name] = [list(row) for row in connection.execute(query)]
            except sqlite3.OperationalError:
                result[name] = []
        result["migration_versions"] = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM _migrations ORDER BY version"
            )
        ]
        result["schema_version"] = connection.execute("PRAGMA user_version").fetchone()[
            0
        ]
    return result


def _projection_hash(projection: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _python_repository_round_trip(database: Path, model: str) -> dict[str, Any]:
    db = Database(path=str(database))
    await db.connect()
    try:
        await MigrationRunner(db).run()
        account = AccountRepository(db)
        account_id = await account.sync_from_config(
            [
                {
                    "name": "q003-python-cycle",
                    "api_key_env": "Q003_TEST_KEY",
                    "provider_id": "fixture",
                }
            ]
        )
        request = RequestRepository(db)
        async with db.transaction():
            request_id = await request.create_pending(
                "q003-python-cycle",
                model,
                "openai",
                False,
                account_id["q003-python-cycle"],
                provider_id="fixture",
            )
        async with db.transaction():
            await request.update_after_completion(
                request_id,
                "success",
                status_code=200,
                input_tokens=4,
                output_tokens=2,
                cost_microdollars=12,
                exactness="exact",
            )
        readback = await request.get_by_id(request_id)
        assert readback is not None
        assert readback["status"] == "success"
        assert readback["input_tokens"] == 4
        return {"account": account_id["q003-python-cycle"], "request": request_id}
    finally:
        await db.disconnect()


def _seed_historical(database: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(HISTORICAL_FIXTURE.read_text(encoding="utf-8"))
        connection.commit()


def _run_interactive_recover(
    rust: RustLauncher,
    environment: IsolatedEnvironment,
    config: Path,
    archive: Path,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.run(
        rust.command(["--config", str(config), "recover", str(archive)]),
        cwd=environment.root,
        env=environment.env(),
        input=b"y\n",
        capture_output=True,
        timeout=20,
        check=False,
    )
    return process


def _write_archive(
    archive: Path,
    *,
    config_path: Path,
    database_path: Path,
    config_bytes: bytes,
    database_bytes: bytes,
    member_names: Iterable[str] = ("config.toml", "usage.sqlite3"),
    duplicate: bool = False,
) -> None:
    members = list(member_names)
    metadata = (
        "format_version = 1\n"
        f"created_at = {datetime.now(UTC).isoformat()!r}\n"
        "install_method = 'q003'\n"
        f"config_path = {str(config_path)!r}\n"
        f"db_path = {str(database_path)!r}\n"
        f"members = {json.dumps(members)}\n"
    )
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as value:
        value.writestr("META", metadata)
        config_members = [name for name in members if name.endswith("config.toml")]
        for name in config_members:
            value.writestr(name, config_bytes)
        if duplicate:
            value.writestr("config.toml", config_bytes)
        if "usage.sqlite3" in members:
            value.writestr("usage.sqlite3", database_bytes)


def test_q003_fixture_matrix_is_versioned_and_source_hashes_are_real() -> None:
    matrix = json.loads(FIXTURE_MATRIX.read_text(encoding="utf-8"))
    historical = matrix["historical_fixture"]
    assert matrix["manifest_version"] == "m10-q003.v1"
    assert (
        hashlib.sha256(HISTORICAL_FIXTURE.read_bytes()).hexdigest()
        == historical["sha256"]
    )
    checksum_path = ROOT / historical["migration_checksum_manifest"]
    assert (
        hashlib.sha256(checksum_path.read_bytes()).hexdigest()
        == historical["migration_checksum_manifest_sha256"]
    )
    assert matrix["archive_contract"]["supported_cross_implementation_shape"] == [
        "META",
        "config.toml",
        ".env (optional)",
        "usage.sqlite3",
    ]


@pytest.mark.asyncio
async def test_python_historical_database_rust_upgrade_and_python_rollback() -> None:
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        root = environment.root / "historical"
        root.mkdir()
        database = root / "usage.sqlite3"
        _seed_historical(database)
        backup_dir = root / "backups"
        config = _write_config(
            root,
            database,
            backup_dir,
            "http://127.0.0.1:0",
            model=HISTORICAL_MODEL,
            account="historical-account",
        )
        before = _projection(database)
        migrated = rust.run(
            ["--config", str(config), "migrate"], environment=environment
        )
        assert migrated.exit_code == 0, migrated.stderr
        assert (
            _projection(database)["migration_versions"][-1] == EXPECTED_SCHEMA_VERSION
        )
        assert _projection(database)["requests"] == before["requests"]

        with StubHttpServer(
            {
                ("GET", "/models"): StubResponse(
                    body=json.dumps({"data": [{"id": HISTORICAL_MODEL}]}).encode()
                ),
                ("POST", "/chat/completions"): _response_factory(HISTORICAL_MODEL),
            }
        ) as upstream:
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'base_url = "http://127.0.0.1:0"',
                    f'base_url = "{upstream.base_url}"',
                ),
                encoding="utf-8",
            )
            _seed_catalog(database, HISTORICAL_MODEL)
            result = _exercise_server_on_port(
                rust, environment, config, database, HISTORICAL_MODEL, upstream
            )
            assert result[0][0] == 200
            assert result[1][0] == 200
            for command in (
                ["--config", str(config), "models", "refresh"],
                ["--config", str(config), "modelinfo", "list"],
                ["--config", str(config), "stats", "explain-dashboard", "--json"],
                ["--config", str(config), "db", "vacuum"],
            ):
                observation = rust.run(command, environment=environment)
                assert observation.exit_code == 0, observation.stderr

        python_readback = await _python_repository_round_trip(
            database, HISTORICAL_MODEL
        )
        assert python_readback["request"]
        projection = _projection(database)
        assert projection["migration_versions"] == list(
            range(1, EXPECTED_SCHEMA_VERSION + 1)
        )
        assert projection["requests"]
        assert _projection_hash(projection) != _projection_hash(before)


@pytest.mark.asyncio
async def test_latest_python_database_accepts_rust_writes_and_python_reads_back() -> (
    None
):
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        root = environment.root / "latest-python"
        root.mkdir()
        database = root / "usage.sqlite3"
        config = _write_config(root, database, root / "backups", "http://127.0.0.1:0")
        python = PythonLauncher()
        migrated = python.run(
            ["--config", str(config), "migrate"], environment=environment
        )
        assert migrated.exit_code == 0, migrated.stderr
        response_route = _response_factory(MODEL)
        with StubHttpServer(
            {
                ("GET", "/models"): StubResponse(
                    body=json.dumps({"data": [{"id": MODEL}]}).encode()
                ),
                ("POST", "/chat/completions"): response_route,
            }
        ) as upstream:
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'base_url = "http://127.0.0.1:0"',
                    f'base_url = "{upstream.base_url}"',
                ),
                encoding="utf-8",
            )
            _seed_catalog(database, MODEL)
            python_result = _exercise_server_on_port(
                python, environment, config, database, MODEL, upstream
            )
            assert python_result[0][0] == 200
            response_route.reset()
            # Startup migration is deliberately rerun against the Python-written
            # latest DB before Rust owns the next request lifecycle.
            rust_migrate = rust.run(
                ["--config", str(config), "migrate"], environment=environment
            )
            assert rust_migrate.exit_code == 0, rust_migrate.stderr
            rust_result = _exercise_server_on_port(
                rust, environment, config, database, MODEL, upstream
            )
            assert rust_result[0][0] == 200
            assert rust_result[1][0] == 200
        await _python_repository_round_trip(database, MODEL)
        projection = _projection(database)
        assert projection["migration_versions"][-1] == EXPECTED_SCHEMA_VERSION
        assert len(projection["requests"]) >= 5
        assert projection["attempts"]
        assert projection["reservations"]


@pytest.mark.asyncio
async def test_rust_database_is_readable_and_writable_by_final_python_reference() -> (
    None
):
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        root = environment.root / "latest-rust"
        root.mkdir()
        database = root / "usage.sqlite3"
        config = _write_config(root, database, root / "backups", "http://127.0.0.1:0")
        migrated = rust.run(
            ["--config", str(config), "migrate"], environment=environment
        )
        assert migrated.exit_code == 0, migrated.stderr
        with StubHttpServer(
            {
                ("GET", "/models"): StubResponse(
                    body=json.dumps({"data": [{"id": MODEL}]}).encode()
                ),
                ("POST", "/chat/completions"): _response_factory(MODEL),
            }
        ) as upstream:
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'base_url = "http://127.0.0.1:0"',
                    f'base_url = "{upstream.base_url}"',
                ),
                encoding="utf-8",
            )
            _exercise_server_on_port(
                rust, environment, config, database, MODEL, upstream
            )
        readback = await _python_repository_round_trip(database, MODEL)
        assert readback["request"]
        assert (
            _projection(database)["migration_versions"][-1] == EXPECTED_SCHEMA_VERSION
        )


@pytest.mark.asyncio
async def test_cross_implementation_backup_and_recovery_round_trip() -> None:
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        root = environment.root / "backup-cross"
        root.mkdir()
        database = root / "usage.sqlite3"
        backup_dir = root / "backups"
        config = _write_config(root, database, backup_dir, "http://127.0.0.1:0")
        env_path = root / ".env"
        env_path.write_text("Q003_SYNTHETIC=fixture\n", encoding="utf-8")
        migrated = rust.run(
            ["--config", str(config), "migrate"], environment=environment
        )
        assert migrated.exit_code == 0, migrated.stderr
        _seed_catalog(database, MODEL)

        wal = sqlite3.connect(database)
        wal.execute("PRAGMA journal_mode = WAL")
        wal.execute(
            "INSERT INTO operational_events (event_type, details_json) VALUES (?, ?)",
            ("q003_wal_commit", "fixture"),
        )
        wal.commit()
        assert (database.with_name(database.name + "-wal")).exists()
        backup = rust.run(
            ["--config", str(config), "backup", "--output-dir", str(backup_dir)],
            environment=environment,
        )
        assert backup.exit_code == 0, backup.stderr
        wal.close()
        archives = sorted(backup_dir.glob("eggpool-backup-*.zip"))
        assert archives
        archive = archives[-1]
        with zipfile.ZipFile(archive) as value:
            assert set(value.namelist()) == {
                "META",
                "config.toml",
                ".env",
                "usage.sqlite3",
            }
            assert all(
                info.compress_type == zipfile.ZIP_STORED for info in value.infolist()
            )
        metadata = parse_zip_metadata(archive)
        assert metadata["format_version"] == 1
        assert sorted(metadata["members"]) == [".env", "config.toml", "usage.sqlite3"]

        python_restore_root = root / "python-restore"
        python_restore_root.mkdir()
        restore_targets = BackupContents(
            python_restore_root / "config.toml",
            python_restore_root / "usage.sqlite3",
            python_restore_root / ".env",
        )
        restore_backup(archive, restore_targets)
        await _python_repository_round_trip(restore_targets.db_path, MODEL)
        with sqlite3.connect(restore_targets.db_path) as connection:
            assert connection.execute(
                "SELECT details_json FROM operational_events "
                "WHERE event_type = 'q003_wal_commit'"
            ).fetchone() == ("fixture",)

        python_source = root / "python-source"
        python_source.mkdir()
        python_db = python_source / "usage.sqlite3"
        python_config = _write_config(
            python_source, python_db, root / "python-backups", "http://127.0.0.1:0"
        )
        python_db_result = PythonLauncher().run(
            ["--config", str(python_config), "migrate"], environment=environment
        )
        assert python_db_result.exit_code == 0, python_db_result.stderr
        _seed_catalog(python_db, MODEL)
        python_archive = await create_runtime_backup(
            db_path=python_db,
            config_path=python_config,
            env_path=None,
            output_dir=root / "python-backups",
            install_method="python",
            include_env=False,
        )
        python_config.write_text("[server\n", encoding="utf-8")
        python_db.write_bytes(b"not a database")
        recovered = _run_interactive_recover(
            rust, environment, python_config, python_archive
        )
        assert recovered.returncode == 0, recovered.stderr.decode()
        await _python_repository_round_trip(python_db, MODEL)


@pytest.mark.parametrize(
    "fault", ["missing-meta", "bad-config", "bad-database", "traversal", "duplicate"]
)
def test_failed_restore_keeps_original_state_for_archive_faults(
    fault: str,
) -> None:
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        root = environment.root / fault
        root.mkdir()
        database = root / "usage.sqlite3"
        config = _write_config(root, database, root / "backups", "http://127.0.0.1:0")
        migrated = rust.run(
            ["--config", str(config), "migrate"], environment=environment
        )
        assert migrated.exit_code == 0, migrated.stderr
        original_config = config.read_bytes()
        original_database = database.read_bytes()
        archive = root / "fault.zip"
        if fault == "missing-meta":
            with zipfile.ZipFile(archive, "w") as value:
                value.writestr("config.toml", original_config)
                value.writestr("usage.sqlite3", original_database)
        elif fault == "traversal":
            _write_archive(
                archive,
                config_path=config,
                database_path=database,
                config_bytes=original_config,
                database_bytes=original_database,
                member_names=("../config.toml", "usage.sqlite3"),
            )
        elif fault == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                _write_archive(
                    archive,
                    config_path=config,
                    database_path=database,
                    config_bytes=original_config,
                    database_bytes=original_database,
                    duplicate=True,
                )
        elif fault == "bad-config":
            _write_archive(
                archive,
                config_path=config,
                database_path=database,
                config_bytes=b"[server\n",
                database_bytes=original_database,
            )
        else:
            _write_archive(
                archive,
                config_path=config,
                database_path=database,
                config_bytes=original_config,
                database_bytes=b"not a database",
            )
        failed = _run_interactive_recover(rust, environment, config, archive)
        assert failed.returncode != 0
        assert config.read_bytes() == original_config
        assert database.read_bytes() == original_database


def test_oversized_archive_member_is_rejected_before_restore() -> None:
    rust = _rust_or_skip()
    with isolated_environment() as environment:
        root = environment.root / "oversized"
        root.mkdir()
        database = root / "usage.sqlite3"
        config = _write_config(root, database, root / "backups", "http://127.0.0.1:0")
        migrated = rust.run(
            ["--config", str(config), "migrate"], environment=environment
        )
        assert migrated.exit_code == 0, migrated.stderr
        original_config = config.read_bytes()
        original_database = database.read_bytes()
        archive = root / "oversized.zip"
        metadata = (
            "format_version = 1\n"
            f"config_path = {str(config)!r}\n"
            f"db_path = {str(database)!r}\n"
            'members = ["config.toml", "usage.sqlite3"]\n'
        )
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as value:
            value.writestr("META", metadata)
            value.writestr("config.toml", original_config + b"x" * (8 * 1024 * 1024))
            value.writestr("usage.sqlite3", original_database)
        failed = _run_interactive_recover(rust, environment, config, archive)
        assert failed.returncode != 0
        assert config.read_bytes() == original_config
        assert database.read_bytes() == original_database
