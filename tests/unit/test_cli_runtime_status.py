"""Tests for eggpool runtime-status CLI command."""

from __future__ import annotations

import contextlib
import io
import json
from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from eggpool.cli_full import (
    _format_bytes,
    _format_duration,
    _print_runtime_status,
)

# -- _format_duration tests ------------------------------------------------


def test_format_duration_seconds() -> None:
    assert _format_duration(45) == "45s"


def test_format_duration_minutes() -> None:
    assert _format_duration(125) == "2m 5s"


def test_format_duration_hours() -> None:
    assert _format_duration(3661) == "1h 1m"


def test_format_duration_zero() -> None:
    assert _format_duration(0) == "0s"


def test_format_duration_non_numeric() -> None:
    assert _format_duration("?") == "?"
    assert _format_duration(None) == "None"


def test_format_duration_float() -> None:
    result = _format_duration(90.5)
    assert result == "1m 30s"


# -- _format_bytes tests ---------------------------------------------------


def test_format_bytes_none() -> None:
    assert _format_bytes(None) == "N/A"


def test_format_bytes_zero() -> None:
    assert _format_bytes(0) == "0 B"


def test_format_bytes_bytes() -> None:
    assert _format_bytes(512) == "512 B"


def test_format_bytes_kilobytes() -> None:
    assert _format_bytes(1536) == "1.5 KB"


def test_format_bytes_megabytes() -> None:
    assert _format_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_format_bytes_gigabytes() -> None:
    assert _format_bytes(2 * 1024 * 1024 * 1024) == "2.00 GB"


def test_format_bytes_non_numeric() -> None:
    assert _format_bytes("not-a-number") == "not-a-number"


# -- _print_runtime_status tests -------------------------------------------


def test_print_runtime_status_basic() -> None:
    data: dict[str, Any] = {
        "server": {
            "pid": 1234,
            "ppid": 1,
            "uptime_seconds": 3661.0,
            "python_version": "3.11.0",
            "configured_server_threads": 2,
        },
        "memory": {
            "rss_bytes": 1024 * 1024,
            "vms_bytes": 2 * 1024 * 1024 * 1024,
            "open_fd_count": 42,
            "thread_count": 5,
        },
        "processes": {
            "eggpool_process_count": 2,
            "expected_worker_process_count": 2,
            "process_count_warning": False,
        },
        "background_tasks": [],
        "db": {
            "path": "/tmp/test.db",
            "file_size_bytes": 102400,
            "wal_size_bytes": 4096,
        },
        "routing_runtime": {
            "pending_count": 3,
            "active_reservations_count": 1,
            "reserved_microdollars": 500000,
        },
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "EggPool Runtime Status" in output
    assert "1234" in output
    assert "1h 1m" in output


def test_print_runtime_status_with_probe_errors() -> None:
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "probe_errors": ["resource.getrusage failed", "Process scan failed"],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "Probe Errors" in output
    assert "resource.getrusage failed" in output
    assert "Process scan failed" in output


def test_print_runtime_status_background_tasks_running() -> None:
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [
            {
                "name": "catalog-refresh",
                "running": True,
                "restart_count": 0,
                "done": False,
                "cancelled": False,
            },
            {
                "name": "cleanup",
                "running": False,
                "restart_count": 3,
                "done": True,
                "cancelled": False,
            },
        ],
        "db": {},
        "routing_runtime": {},
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "catalog-refresh: running" in output
    assert "cleanup: STOPPED (restarts: 3)" in output


def test_print_runtime_status_process_count_warning() -> None:
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {
            "eggpool_process_count": 5,
            "expected_worker_process_count": 2,
            "process_count_warning": True,
        },
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "5 (expected: 2)" in output
    assert "WARNING" in output


def test_print_runtime_status_memory_db() -> None:
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {
            "path": None,
            "is_memory_db": True,
            "file_size_bytes": None,
            "wal_size_bytes": None,
        },
        "routing_runtime": {},
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert ":memory:" in output


# -- CLI command tests via CliRunner ---------------------------------------


def test_runtime_status_exits_nonzero_when_unreachable() -> None:
    """Command exits non-zero when the server cannot be reached."""
    from eggpool.cli import cli

    runner = CliRunner()
    with patch("eggpool.cli_full.AppConfig.from_toml") as mock_config:
        mock_config.return_value = MagicMock(
            server=MagicMock(host="127.0.0.1", port=1, resolved_api_key=None)
        )
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=OSError("Connection refused"),
            ),
            patch(
                "eggpool.deploy_user.resolve_config_path",
                return_value="/tmp/fake-config.toml",
            ),
        ):
            result = runner.invoke(cli, ["runtime-status"])
            assert result.exit_code == 1


def test_runtime_status_exits_nonzero_on_http_error() -> None:
    """Command exits non-zero when the server returns an HTTP error."""
    from urllib.error import HTTPError

    from eggpool.cli import cli

    runner = CliRunner()
    with patch("eggpool.cli_full.AppConfig.from_toml") as mock_config:
        mock_config.return_value = MagicMock(
            server=MagicMock(host="127.0.0.1", port=1, resolved_api_key=None)
        )
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=HTTPError(
                    url="http://127.0.0.1:1/api/stats/runtime",
                    code=401,
                    msg="Unauthorized",
                    hdrs=None,
                    fp=None,
                ),
            ),
            patch(
                "eggpool.deploy_user.resolve_config_path",
                return_value="/tmp/fake-config.toml",
            ),
        ):
            result = runner.invoke(cli, ["runtime-status"])
            assert result.exit_code == 1
            assert "401" in result.output


def test_runtime_status_prints_summary_on_success() -> None:
    """Command prints a summary when the endpoint returns valid JSON."""
    from eggpool.cli import cli

    response_data = {
        "server": {
            "pid": 9999,
            "ppid": 1,
            "uptime_seconds": 120.0,
            "python_version": "3.12.0",
            "configured_server_threads": 2,
        },
        "memory": {
            "rss_bytes": 1048576,
            "vms_bytes": 2097152,
            "open_fd_count": 10,
            "thread_count": 3,
        },
        "processes": {
            "eggpool_process_count": 2,
            "expected_worker_process_count": 2,
            "process_count_warning": False,
        },
        "background_tasks": [],
        "db": {
            "path": None,
            "is_memory_db": True,
            "file_size_bytes": None,
            "wal_size_bytes": None,
        },
        "routing_runtime": {
            "pending_count": 0,
            "active_reservations_count": 0,
            "reserved_microdollars": 0,
        },
        "probe_errors": [],
    }

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    runner = CliRunner()
    with patch("eggpool.cli_full.AppConfig.from_toml") as mock_config:
        mock_config.return_value = MagicMock(
            server=MagicMock(
                host="127.0.0.1",
                port=1,
                resolved_api_key="test-key-12345678",
            )
        )
        with (
            patch("urllib.request.urlopen", return_value=mock_response),
            patch(
                "eggpool.deploy_user.resolve_config_path",
                return_value="/tmp/fake-config.toml",
            ),
        ):
            result = runner.invoke(cli, ["runtime-status"])
            assert result.exit_code == 0
            assert "9999" in result.output
            assert "EggPool Runtime Status" in result.output


def test_runtime_status_json_output() -> None:
    """Command outputs raw JSON when --json is passed."""
    from eggpool.cli import cli

    response_data = {
        "server": {
            "pid": 42,
            "ppid": 1,
            "uptime_seconds": 60.0,
            "python_version": "3.12.0",
            "configured_server_threads": 1,
        },
        "memory": {"rss_bytes": 1048576, "vms_bytes": 2097152},
        "processes": {
            "eggpool_process_count": 2,
            "expected_worker_process_count": 2,
            "process_count_warning": False,
        },
        "background_tasks": [],
        "db": {"path": "/tmp/test.db", "is_memory_db": False},
        "routing_runtime": {},
        "probe_errors": [],
    }

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    runner = CliRunner()
    with patch("eggpool.cli_full.AppConfig.from_toml") as mock_config:
        mock_config.return_value = MagicMock(
            server=MagicMock(host="127.0.0.1", port=1, resolved_api_key=None)
        )
        with (
            patch("urllib.request.urlopen", return_value=mock_response),
            patch(
                "eggpool.deploy_user.resolve_config_path",
                return_value="/tmp/fake-config.toml",
            ),
        ):
            result = runner.invoke(cli, ["runtime-status", "--json"])
            assert result.exit_code == 0
            parsed = json.loads(result.output)
            assert parsed["server"]["pid"] == 42
            assert "EggPool Runtime Status" not in result.output


def test_runtime_status_normalizes_bind_address() -> None:
    """Command normalizes 0.0.0.0 to 127.0.0.1 for the localhost probe."""
    from eggpool.cli import cli

    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(
        {
            "server": {
                "pid": 1,
                "uptime_seconds": 0,
                "configured_server_threads": 1,
            },
            "memory": {},
            "processes": {},
            "background_tasks": [],
            "db": {},
            "routing_runtime": {},
            "probe_errors": [],
        }
    ).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    runner = CliRunner()
    with patch("eggpool.cli_full.AppConfig.from_toml") as mock_config:
        mock_config.return_value = MagicMock(
            server=MagicMock(host="0.0.0.0", port=5000, resolved_api_key=None)
        )
        with (
            patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen,
            patch(
                "eggpool.deploy_user.resolve_config_path",
                return_value="/tmp/fake-config.toml",
            ),
        ):
            result = runner.invoke(cli, ["runtime-status"])
            assert result.exit_code == 0
            # Verify the URL used 127.0.0.1 instead of 0.0.0.0
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert "127.0.0.1" in req.full_url
            assert "0.0.0.0" not in req.full_url
