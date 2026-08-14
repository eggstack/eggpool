"""Tests for resolve_apply_outcome helper.

The safe-fallback policy requires that a healthy server is never
silently restarted when the control socket happens to be unavailable.
These tests pin every branch of the decision tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from eggpool.providers.connect import resolve_apply_outcome

if TYPE_CHECKING:
    from pathlib import Path


def _valid_config_body() -> str:
    return (
        "[server]\n"
        'api_key = "ep_test_server_key_1234567890"\n'
        "\n"
        "[providers.opencode-go]\n"
        'id = "opencode-go"\n'
        'base_url = "https://opencode.ai/zen/go/v1"\n'
        'protocols = ["openai"]\n'
        "\n[providers.opencode-go.models_endpoint]\n"
        'method = "GET"\npath = "/models"\n'
        "\n[[providers.opencode-go.accounts]]\n"
        'name = "default"\n'
        'api_key = "sk-test-account-key-1234567890"\n'
        "enabled = true\n"
        "weight = 1.0\n"
    )


def _write_config(path: Path, body: str | None = None) -> None:
    path.write_text(body or _valid_config_body(), encoding="utf-8")


class TestResolveApplyOutcome:
    """Unit tests for the resolve_apply_outcome decision tree."""

    def test_live_apply_when_control_socket_succeeds(self, tmp_path: Path) -> None:
        """Live rehash returns (True, msg) → caller gets (True, msg)."""
        config = tmp_path / "config.toml"
        _write_config(config)
        with patch(
            "eggpool.cli_rehash_helper.try_live_rehash",
            return_value=(True, "Live reload applied."),
        ):
            applied, message = resolve_apply_outcome(str(config))
        assert applied is True
        assert "applied" in message.lower()

    def test_control_unavailable_does_not_restart_healthy_server(
        self, tmp_path: Path
    ) -> None:
        """Server healthy + socket unavailable → no restart."""
        config = tmp_path / "config.toml"
        _write_config(config)
        with (
            patch(
                "eggpool.cli_rehash_helper.try_live_rehash",
                return_value=(
                    False,
                    "Control socket unavailable (ConnectionRefusedError).",
                ),
            ),
            patch(
                "eggpool.providers.connect.restart_server",
            ) as mock_restart,
        ):
            applied, message = resolve_apply_outcome(
                str(config), health_check=lambda: True
            )
        assert applied is False
        assert "control unavailable" in message.lower()
        mock_restart.assert_not_called()

    def test_unhealthy_server_triggers_restart(self, tmp_path: Path) -> None:
        """Server not running → restart is attempted."""
        config = tmp_path / "config.toml"
        _write_config(config)
        with (
            patch(
                "eggpool.cli_rehash_helper.try_live_rehash",
                return_value=(
                    False,
                    "Control socket unavailable (ConnectionRefusedError).",
                ),
            ),
            patch(
                "eggpool.providers.connect.restart_server",
                return_value=True,
            ) as mock_restart,
        ):
            applied, message = resolve_apply_outcome(
                str(config), health_check=lambda: False
            )
        assert applied is True
        assert "restarted" in message.lower()
        mock_restart.assert_called_once_with(str(config))

    def test_health_probe_receives_selected_config_path(self, tmp_path: Path) -> None:
        """The healthz fallback probes the same config selected by the CLI."""
        config = tmp_path / "custom.toml"
        _write_config(config)
        with (
            patch(
                "eggpool.cli_rehash_helper.try_live_rehash",
                return_value=(False, "Control socket unavailable (OSError)."),
            ),
            patch(
                "eggpool.providers.connect._is_server_healthy",
                return_value=False,
            ) as health_probe,
            patch(
                "eggpool.providers.connect.restart_server",
                return_value=False,
            ),
        ):
            resolve_apply_outcome(str(config))

        health_probe.assert_called_once_with(None, str(config))

    def test_validation_failure_returns_false_without_restart(
        self, tmp_path: Path
    ) -> None:
        """Invalid TOML → (False, 'validation failed'), no restart."""
        invalid = tmp_path / "bad.toml"
        invalid.write_text("this is not valid TOML [[[", encoding="utf-8")
        with patch(
            "eggpool.providers.connect.restart_server",
        ) as mock_restart:
            applied, message = resolve_apply_outcome(str(invalid))
        assert applied is False
        assert "validation failed" in message.lower()
        mock_restart.assert_not_called()

    def test_restart_required_does_not_restart_when_live_apply_declined(
        self, tmp_path: Path
    ) -> None:
        """Restart-required message + healthy server → no restart."""
        config = tmp_path / "config.toml"
        _write_config(config)
        with (
            patch(
                "eggpool.cli_rehash_helper.try_live_rehash",
                return_value=(False, "Restart required for: server.port"),
            ),
            patch(
                "eggpool.providers.connect.restart_server",
            ) as mock_restart,
        ):
            applied, message = resolve_apply_outcome(
                str(config), health_check=lambda: True
            )
        assert applied is False
        assert "restart required" in message.lower()
        mock_restart.assert_not_called()

    def test_permission_denied_socket_does_not_restart_healthy_server(
        self, tmp_path: Path
    ) -> None:
        """Permission-denied socket + healthy server → no restart."""
        config = tmp_path / "config.toml"
        _write_config(config)
        with (
            patch(
                "eggpool.cli_rehash_helper.try_live_rehash",
                return_value=(
                    False,
                    "Control socket unavailable (PermissionError).",
                ),
            ),
            patch(
                "eggpool.providers.connect.restart_server",
            ) as mock_restart,
        ):
            applied, message = resolve_apply_outcome(
                str(config), health_check=lambda: True
            )
        assert applied is False
        assert "control unavailable" in message.lower()
        mock_restart.assert_not_called()

    def test_restart_server_returns_false_when_already_stopped(
        self, tmp_path: Path
    ) -> None:
        """Server not running + restart_server returns False."""
        config = tmp_path / "config.toml"
        _write_config(config)
        with (
            patch(
                "eggpool.cli_rehash_helper.try_live_rehash",
                return_value=(
                    False,
                    "Control socket unavailable (ConnectionRefusedError).",
                ),
            ),
            patch(
                "eggpool.providers.connect.restart_server",
                return_value=False,
            ),
        ):
            applied, message = resolve_apply_outcome(
                str(config), health_check=lambda: False
            )
        assert applied is False
        assert "not running" in message.lower()

    def test_prefer_live_false_skips_control_socket(self, tmp_path: Path) -> None:
        """apply_or_restart(prefer_live=False) always restarts."""
        from eggpool.providers.connect import apply_or_restart

        config = tmp_path / "config.toml"
        _write_config(config)
        with (
            patch(
                "eggpool.cli_rehash_helper.try_live_rehash",
            ) as mock_rehash,
            patch(
                "eggpool.providers.connect.restart_server",
                return_value=True,
            ),
        ):
            applied, message = apply_or_restart(str(config), prefer_live=False)
        mock_rehash.assert_not_called()
        assert applied is True
