"""Tests for the shared validate-and-rehash CLI helper.

The closure-pass plan (§7.4) calls for one CLI helper used by every
config-mutating command.  These tests pin the failure paths:
validation failure, control-socket unavailable, restart-required
rejection, and successful reload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from eggpool.cli_exit_codes import (
    EXIT_RESTART_REQUIRED,
    EXIT_VALIDATION,
)
from eggpool.cli_rehash_helper import validate_and_rehash

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


class TestValidateAndRehash:
    def test_validation_failure_exits_with_exit_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        invalid = tmp_path / "invalid.toml"
        invalid.write_text("this is not valid TOML [[[", encoding="utf-8")
        captured_messages: list[str] = []
        with pytest.raises(SystemExit) as exc_info:
            validate_and_rehash(
                str(invalid), echo_err=lambda msg: captured_messages.append(msg)
            )
        assert exc_info.value.code == EXIT_VALIDATION
        assert any("validation failed" in m.lower() for m in captured_messages)

    def test_control_socket_unavailable_exits_with_exit_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "config.toml"
        _write_config(config)
        captured_messages: list[str] = []
        with (
            patch("asyncio.run", side_effect=ConnectionRefusedError("no socket")),
            pytest.raises(SystemExit) as exc_info,
        ):
            validate_and_rehash(
                str(config), echo_err=lambda msg: captured_messages.append(msg)
            )
        assert exc_info.value.code == EXIT_VALIDATION
        assert any("control socket" in m.lower() for m in captured_messages)

    def test_restart_required_response_exits_with_exit_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "config.toml"
        _write_config(config)

        fake_response = _FakeResponse(
            ok=False,
            stage="diff",
            generation=None,
            changed_sections=(),
            warnings=(),
            restart_required=("server.port",),
            retirement_pending=False,
            message="server.port: 8000 -> 9000 requires restart",
        )
        captured_messages: list[str] = []
        with (
            patch("asyncio.run", return_value=fake_response),
            pytest.raises(SystemExit) as exc_info,
        ):
            validate_and_rehash(
                str(config),
                echo_err=lambda msg: captured_messages.append(msg),
            )
        assert exc_info.value.code == EXIT_RESTART_REQUIRED
        assert any("restart" in m.lower() for m in captured_messages)


class TestTryLiveRehash:
    def test_successful_reload_returns_true(self, tmp_path: Path) -> None:
        from eggpool.cli_rehash_helper import try_live_rehash

        config = tmp_path / "config.toml"
        _write_config(config)

        fake_response = _FakeResponse(
            ok=True,
            stage="retirement",
            generation=5,
            changed_sections=("providers", "accounts"),
            warnings=(),
            restart_required=(),
            retirement_pending=True,
            message="Live reload applied.",
        )
        with patch("asyncio.run", return_value=fake_response):
            applied, message = try_live_rehash(str(config))
        assert applied is True
        assert "applied" in message.lower()


class _FakeResponse:
    """Minimal stand-in for ``ControlResponse``."""

    def __init__(
        self,
        *,
        ok: bool,
        stage: str,
        generation: int | None,
        changed_sections: tuple[str, ...],
        warnings: tuple[str, ...],
        restart_required: tuple[str, ...],
        retirement_pending: bool,
        message: str,
    ) -> None:
        self.ok = ok
        self.stage = stage
        self.generation = generation
        self.changed_sections = changed_sections
        self.warnings = warnings
        self.restart_required = restart_required
        self.retirement_pending = retirement_pending
        self.message = message
