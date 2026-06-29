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


# -- Network diagnostics rendering -----------------------------------------


def test_print_runtime_status_network_section() -> None:
    """_print_runtime_status renders the network section."""
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "outbound_client": {
            "build_count": 1,
            "request_count": 42,
            "error_count": 2,
            "has_client": True,
            "scopes": {"global": 1},
            "per_host_requests": {},
            "per_host_errors": {},
        },
        "provider_client_pool": {
            "build_count": 3,
            "providers": {"openai": 1, "anthropic": 1, "opencode-go": 1},
        },
        "dns_cache": {
            "enabled": True,
            "size": 7,
            "hits": 100,
            "misses": 10,
            "negative_hits": 0,
            "stale_hits": 0,
            "evictions": 0,
            "resolution_errors": {},
            "hosts": [],
            "cache_hits_total": 100,
            "cache_misses_owner_total": 10,
            "singleflight_waits_total": 0,
            "resolver_calls_total": 10,
            "resolver_successes_total": 10,
            "resolver_errors_total": 0,
            "cache_hit_rate": 0.9091,
            "dns_suppression_rate": 0.9091,
            "resolver_calls_per_logical_resolve": 0.1,
            "worst_missers": [],
        },
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "Network:" in output
    assert "enabled" in output
    assert "Outbound builds:" in output
    assert "Outbound requests:" in output
    assert "DNS cache:" in output
    assert "DNS suppression:" in output
    assert "Resolver calls:" in output
    assert "Cache hits:" in output
    assert "Owner misses:" in output
    assert "Provider clients:" in output


def test_print_runtime_status_network_disabled() -> None:
    """Network section shows 'disabled' when DNS cache is off."""
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "outbound_client": {"build_count": 0, "request_count": 0, "error_count": 0},
        "dns_cache": {
            "enabled": False,
            "cache_hits_total": 0,
            "cache_misses_owner_total": 0,
            "singleflight_waits_total": 0,
            "resolver_calls_total": 0,
            "resolver_successes_total": 0,
            "resolver_errors_total": 0,
            "cache_hit_rate": 0.0,
            "dns_suppression_rate": 0.0,
            "resolver_calls_per_logical_resolve": 0.0,
            "worst_missers": [],
        },
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "disabled" in output


def test_print_runtime_status_network_empty_data() -> None:
    """Network section handles missing outbound_client/dns_cache gracefully."""
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "Network:" in output
    assert "0" in output


def test_print_runtime_status_dns_hit_rate_calculation() -> None:
    """DNS hit rate is calculated correctly."""
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "outbound_client": {"build_count": 1, "request_count": 0, "error_count": 0},
        "dns_cache": {
            "enabled": True,
            "size": 3,
            "hits": 90,
            "misses": 10,
            "negative_hits": 0,
            "stale_hits": 0,
            "evictions": 0,
            "resolution_errors": {},
            "cache_hits_total": 90,
            "cache_misses_owner_total": 10,
            "singleflight_waits_total": 0,
            "resolver_calls_total": 10,
            "resolver_successes_total": 10,
            "resolver_errors_total": 0,
            "cache_hit_rate": 0.9,
            "dns_suppression_rate": 0.9,
            "resolver_calls_per_logical_resolve": 0.1,
            "worst_missers": [],
        },
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "90.0%" in output


def test_runtime_status_includes_network_in_json() -> None:
    """JSON output includes outbound_client and dns_cache keys."""
    from eggpool.cli import cli

    response_data = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "outbound_client": {"build_count": 1, "request_count": 5, "error_count": 0},
        "provider_client_pool": {"build_count": 2, "providers": {"a": 1, "b": 1}},
        "dns_cache": {
            "enabled": True,
            "hits": 10,
            "misses": 2,
            "hosts": [],
            "cache_hits_total": 10,
            "cache_misses_owner_total": 2,
            "singleflight_waits_total": 0,
            "resolver_calls_total": 2,
            "resolver_successes_total": 2,
            "resolver_errors_total": 0,
            "cache_hit_rate": 0.8333,
            "dns_suppression_rate": 0.8333,
            "resolver_calls_per_logical_resolve": 0.1667,
            "worst_missers": [],
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
            assert "outbound_client" in parsed
            assert "dns_cache" in parsed
            assert "provider_client_pool" in parsed


def test_print_runtime_status_dns_cache_hosts() -> None:
    """DNS cache hosts are rendered when present."""
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "outbound_client": {"build_count": 1, "request_count": 0, "error_count": 0},
        "provider_client_pool": {"build_count": 0, "providers": {}},
        "dns_cache": {
            "enabled": True,
            "size": 2,
            "hits": 50,
            "misses": 5,
            "resolution_errors": {},
            "hosts": [
                {
                    "host": "api.openai.com",
                    "family": "ipv4",
                    "state": "positive",
                    "expires_in_seconds": 241.0,
                    "stale_available": True,
                    "last_error_kind": None,
                },
                {
                    "host": "api.anthropic.com",
                    "family": "ipv4",
                    "state": "negative",
                    "expires_in_seconds": 30.0,
                    "stale_available": False,
                    "last_error_kind": "ConnectError",
                },
            ],
        },
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "DNS cache entries:" in output
    assert "api.openai.com (ipv4) state=positive" in output
    assert "stale_ok" in output
    assert "api.anthropic.com (ipv4) state=negative" in output
    assert "error=ConnectError" in output


def test_print_runtime_status_provider_clients() -> None:
    """Provider client counts are rendered."""
    data: dict[str, Any] = {
        "server": {"pid": 1, "uptime_seconds": 0, "configured_server_threads": 1},
        "memory": {},
        "processes": {},
        "background_tasks": [],
        "db": {},
        "routing_runtime": {},
        "outbound_client": {"build_count": 1, "request_count": 0, "error_count": 0},
        "provider_client_pool": {
            "build_count": 3,
            "providers": {"anthropic": 1, "openai": 1, "opencode-go": 1},
        },
        "dns_cache": {"enabled": True, "hosts": []},
        "probe_errors": [],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _print_runtime_status(data)
    output = buf.getvalue()
    assert "Provider clients:" in output
    assert "anthropic: 1" in output
    assert "openai: 1" in output
    assert "opencode-go: 1" in output
