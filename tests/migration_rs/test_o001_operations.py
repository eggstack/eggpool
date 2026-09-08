"""O001 guards for the complete M9 operational oracle boundary."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.migration_rs.operations_fixtures import (
    OBSERVATIONS_PATH,
    assert_secret_free,
    deferred_task_names,
    fixture_command_inventory,
    fixture_matrix,
    load_json,
    python_command_inventory,
)


def test_current_python_commands_have_one_owner_and_exact_options() -> None:
    expected = fixture_command_inventory()
    actual = python_command_inventory()

    assert len(expected) == 63
    assert actual == [
        {"path": row["path"], "options": row["options"]} for row in expected
    ]
    assert {row["owner"] for row in expected} <= {
        "O003",
        "O004",
        "O005",
        "O006",
        "O007",
        "O008",
        "O009",
    }
    assert fixture_matrix()["global_options"] == ["--config"]


def test_f003_deltas_are_explicit_and_resolved() -> None:
    deltas = fixture_matrix()["deltas_from_f003"]
    assert {row["path"] for row in deltas} == {
        "dashboard public",
        "stats recompute-costs",
        "stats repair-costs",
    }
    assert all("resolution" in row for row in deltas)
    assert "--off" in next(
        row["python"] for row in deltas if row["path"] == "dashboard public"
    )
    assert "--apply" in next(
        row["python"] for row in deltas if row["path"] == "stats recompute-costs"
    )


def test_checked_in_observations_are_secret_free_and_bounded() -> None:
    matrix = load_json(fixture_matrix_path())
    observations = load_json(OBSERVATIONS_PATH)
    assert_secret_free(matrix)
    assert_secret_free(observations)
    assert len(json.dumps(matrix)) < 50_000
    assert len(json.dumps(observations)) < 50_000
    assert observations["secret_policy"] == {
        "persisted_secret_values": False,
        "real_home_paths": False,
        "real_network_addresses": False,
        "unbounded_subprocess_output": False,
        "synthetic_secret_sentinel": False,
    }


def fixture_matrix_path() -> Path:
    """Return the matrix path through the public fixture loader's location."""
    from tests.migration_rs.operations_fixtures import MATRIX_PATH

    return MATRIX_PATH


def test_runtime_paths_and_config_precedence_stay_isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from eggpool.deploy_user import resolve_config_path
    from eggpool.runtime_paths import (
        default_log_file,
        default_pid_file,
        runtime_dir,
        state_dir,
    )

    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    log = tmp_path / "logs" / "eggpool.log"
    pid = runtime / "eggpool.pid"
    for key, value in {
        "EGGPOOL_STATE_DIR": str(state),
        "EGGPOOL_RUNTIME_DIR": str(runtime),
        "EGGPOOL_LOG_FILE": str(log),
        "EGGPOOL_PID_FILE": str(pid),
    }.items():
        monkeypatch.setenv(key, value)

    assert state_dir() == state
    assert runtime_dir() == runtime
    assert default_log_file() == log
    assert default_pid_file() == pid

    env_config = tmp_path / "env.toml"
    cli_config = tmp_path / "cli.toml"
    assert (
        resolve_config_path(
            cli_value=str(cli_config), env={"EGGPOOL_CONFIG": str(env_config)}
        )
        == cli_config
    )
    assert resolve_config_path(env={"EGGPOOL_CONFIG": str(env_config)}) == env_config


@pytest.mark.asyncio
async def test_control_contract_rejects_bad_frames_without_calling_reload() -> None:
    from eggpool.control.server import PROTOCOL_VERSION, ControlServer

    calls: list[Any] = []

    async def handler(request: Any) -> Any:
        calls.append(request)
        raise AssertionError("invalid frame reached reload handler")

    server = ControlServer(handler)
    wrong_version = await server._process_request(  # noqa: SLF001
        b'{"protocol_version": 2, "request_id": "req", "command": "reload_config"}'
    )
    assert wrong_version.protocol_version == PROTOCOL_VERSION
    assert wrong_version.request_id == "req"
    assert wrong_version.stage == "parse"
    assert not wrong_version.ok

    oversized = await server._process_request(b"x" * 65537)  # noqa: SLF001
    assert oversized.stage == "parse"
    assert "65536" in oversized.message
    assert calls == []


def test_backup_archive_is_allowlisted_and_fixed_targeted(tmp_path: Path) -> None:
    from eggpool.lifecycle.backup import BackupContents, _build_archive, _plan_restore

    config = tmp_path / "config.toml"
    database = tmp_path / "usage.sqlite3"
    config.write_text("[server]\nport = 11300\n", encoding="utf-8")
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE marker (value TEXT)")
    connection.execute("INSERT INTO marker VALUES ('synthetic')")
    connection.commit()
    connection.close()

    archive_path = tmp_path / "backup.zip"
    _build_archive(
        archive_path,
        BackupContents(config, database, install_method="test"),
        now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["META", "config.toml", "usage.sqlite3"]
        assert all(
            info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()
        )

    traversal_alias = tmp_path / "traversal-alias.zip"
    with zipfile.ZipFile(traversal_alias, "w") as archive:
        archive.writestr("META", "format_version = 1\n")
        archive.writestr("../config.toml", "[server]\n")
    with zipfile.ZipFile(traversal_alias) as archive:
        plan = _plan_restore(archive)
    assert plan.config_target == Path("config.toml")
    assert "config.toml" in plan.members
    assert "../config.toml" not in plan.members
    assert (
        "backup archive member allowlist and fixed-target traversal classification"
        in fixture_matrix()["safety_cases"]
    )


def test_deployment_renderers_are_deterministic_and_fakeable() -> None:
    from eggpool.deploy import (
        build_personal_systemd_unit,
        build_personal_watchdog_cron,
        strip_managed_cron_blocks,
    )

    unit_a = build_personal_systemd_unit("eggpool", "config.toml", "data")
    unit_b = build_personal_systemd_unit("eggpool", "config.toml", "data")
    assert unit_a == unit_b
    cron = build_personal_watchdog_cron("eggpool", "config.toml", "eggpool.log")
    assert "ensure-running" in cron
    assert strip_managed_cron_blocks("unrelated\n" + cron) == "unrelated\n"
    assert fixture_matrix()["safety_cases"][5] == "fake systemctl/cron/logrotate only"


def test_deferred_r008_inventory_is_exactly_three_and_not_duplicated() -> None:
    from eggpool.runtime_task_inventory import RUNTIME_TASK_INVENTORY

    names = [spec.name for spec in RUNTIME_TASK_INVENTORY]
    assert len(names) == len(set(names))
    assert deferred_task_names() == (
        "metrics_flush",
        "update_checker",
        "automatic_backup",
    )
    assert load_json(OBSERVATIONS_PATH)["deferred_r008"]["fourth_capability"] is False


def test_update_observations_use_local_fake_metadata() -> None:
    from eggpool.update_checker import (
        check_exact_release,
        is_newer_version,
        normalize_requested_version,
    )

    assert normalize_requested_version("v0.7.4") == "0.7.4"
    assert is_newer_version("0.7.4", "0.7.5")
    assert not is_newer_version("0.7.5", "0.7.4")

    def fake_get(*_args: Any, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(
            200,
            json={"info": {"version": "0.7.4"}},
            request=httpx.Request("GET", "https://release.invalid"),
        )

    assert check_exact_release("v0.7.4", http_get=fake_get) == ("0.7.4", "")


def test_env_backed_server_key_mutation_preserves_directive(tmp_path: Path) -> None:
    from eggpool.config_utils import write_server_api_key

    config = tmp_path / "config.toml"
    original = (
        '[server]\napi_key_env = "SERVER_API_KEY"\n\n'
        "[models]\nrefresh_interval_s = 300\n"
    )
    config.write_text(original, encoding="utf-8")
    success, warning = write_server_api_key(str(config), "synthetic-secret")
    assert success
    assert warning is not None
    assert config.read_text(encoding="utf-8") == original


def test_no_real_destructive_command_is_needed_for_fixture_collection() -> None:
    observations = load_json(OBSERVATIONS_PATH)
    assert observations["deployment"]["host_mutation"] is False
    assert observations["deployment"]["fake_commands_only"] == [
        "systemctl",
        "crontab",
        "logrotate",
    ]
