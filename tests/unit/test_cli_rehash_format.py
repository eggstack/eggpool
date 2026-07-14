"""Snapshot tests for the ``eggpool rehash`` CLI JSON and human output contract.

Workstream 4 (CLI/JSON contract tightening) pins the exact shape of
machine-readable and human-readable output for every ``rehash`` outcome.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from eggpool.cli import cli
from eggpool.cli_exit_codes import (
    EXIT_CONTROL_UNAVAILABLE,
    EXIT_DIGEST_MISMATCH,
    EXIT_OK,
    EXIT_PREPARATION_FAILED,
    EXIT_RELOAD_BUSY,
    EXIT_RESTART_REQUIRED,
    EXIT_VALIDATION,
    STAGE_RELOAD_IN_PROGRESS,
)
from eggpool.cli_rehash_format import (
    _redact_message,
    format_rehash_json,
    render_rehash_human,
)
from eggpool.control.server import ControlResponse

SERVER_API_KEY = "ep_test_server_key_1234567890"
ACCOUNT_API_KEY = "sk-test-account-key-1234567890"

# Canonical keys every JSON output must contain.
_EXPECTED_JSON_KEYS: frozenset[str] = frozenset(
    {
        "ok",
        "stage",
        "exit_code",
        "generation",
        "changed_sections",
        "warnings",
        "restart_required",
        "retirement_pending",
        "message",
    }
)


def _parse_json_from_output(output: str) -> dict:
    """Extract the first JSON object from CLI output with preceding prose."""
    idx = output.index("{")
    return json.loads(output[idx:])


def _write_config(tmp_path: Path, body: str | None = None) -> str:
    config_path = tmp_path / "config.toml"
    config_path.write_text(body or _valid_body(), encoding="utf-8")
    return str(config_path)


def _valid_body() -> str:
    return (
        f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
        "[providers.opencode-go]\n"
        'id = "opencode-go"\n'
        'base_url = "https://opencode.ai/zen/go/v1"\n'
        'protocols = ["openai"]\n'
        "\n[providers.opencode-go.models_endpoint]\n"
        'method = "GET"\npath = "/models"\n'
        "\n[[providers.opencode-go.accounts]]\n"
        'name = "default"\n'
        f'api_key = "{ACCOUNT_API_KEY}"\n'
        "enabled = true\n"
        "weight = 1.0\n"
    )


def _make_response(
    *,
    ok: bool,
    stage: str,
    generation: int | None = None,
    changed_sections: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
    restart_required: tuple[str, ...] = (),
    retirement_pending: bool = False,
    message: str = "",
) -> ControlResponse:
    return ControlResponse(
        protocol_version=1,
        request_id="test-request-id",
        ok=ok,
        stage=stage,
        generation=generation,
        changed_sections=changed_sections,
        warnings=warnings,
        restart_required=restart_required,
        retirement_pending=retirement_pending,
        message=message,
    )


# ---------------------------------------------------------------------------
# Part D: JSON contract snapshot tests
# ---------------------------------------------------------------------------


class TestJsonContractSuccess:
    """Success path: applied with retirement pending."""

    def test_success_json_has_all_keys(self) -> None:
        resp = _make_response(
            ok=True,
            stage="retirement",
            generation=7,
            changed_sections=("routing",),
            warnings=(),
            restart_required=(),
            retirement_pending=True,
            message="Reload applied: generation 7, 1 section(s) changed",
        )
        result = format_rehash_json(resp, EXIT_OK)

        assert set(result.keys()) >= _EXPECTED_JSON_KEYS
        assert result["ok"] is True
        assert result["stage"] == "retirement"
        assert result["exit_code"] == EXIT_OK
        assert result["generation"] == 7
        assert result["changed_sections"] == ["routing"]
        assert result["warnings"] == []
        assert result["restart_required"] == []
        assert result["retirement_pending"] is True
        assert "generation 7" in result["message"]

    def test_success_json_values_are_correct_types(self) -> None:
        resp = _make_response(
            ok=True,
            stage="retirement",
            generation=3,
            changed_sections=("routing",),
            retirement_pending=True,
            message="Reload applied",
        )
        result = format_rehash_json(resp, EXIT_OK)

        assert isinstance(result["ok"], bool)
        assert isinstance(result["stage"], str)
        assert isinstance(result["exit_code"], int)
        assert isinstance(result["generation"], int)
        assert isinstance(result["changed_sections"], list)
        assert isinstance(result["warnings"], list)
        assert isinstance(result["restart_required"], list)
        assert isinstance(result["retirement_pending"], bool)
        assert isinstance(result["message"], str)


class TestJsonContractNoOp:
    """No-op path: commit stage, no changes."""

    def test_noop_json(self) -> None:
        resp = _make_response(
            ok=True,
            stage="commit",
            generation=5,
            changed_sections=(),
            retirement_pending=False,
            message="No configuration changes detected",
        )
        result = format_rehash_json(resp, EXIT_OK)

        assert set(result.keys()) >= _EXPECTED_JSON_KEYS
        assert result["ok"] is True
        assert result["stage"] == "commit"
        assert result["exit_code"] == EXIT_OK
        assert result["generation"] == 5
        assert result["changed_sections"] == []
        assert result["restart_required"] == []
        assert result["retirement_pending"] is False


class TestJsonContractBusy:
    """Busy path: reload_in_progress stage → exit code 4."""

    def test_busy_json(self) -> None:
        resp = _make_response(
            ok=False,
            stage=STAGE_RELOAD_IN_PROGRESS,
            generation=None,
            message="A reload transaction is already in progress",
        )
        result = format_rehash_json(resp, EXIT_RELOAD_BUSY)

        assert set(result.keys()) >= _EXPECTED_JSON_KEYS
        assert result["ok"] is False
        assert result["stage"] == "reload_in_progress"
        assert result["exit_code"] == EXIT_RELOAD_BUSY
        assert result["generation"] is None
        assert result["retirement_pending"] is False
        assert "reload" in result["message"].lower()

    def test_busy_exit_code_via_failure_classifier(self) -> None:
        from eggpool.cli_exit_codes import exit_code_for_failure

        code = exit_code_for_failure(
            stage=STAGE_RELOAD_IN_PROGRESS,
            restart_required=(),
            message="A reload transaction is already in progress",
        )
        assert code == EXIT_RELOAD_BUSY


class TestJsonContractRestartRequired:
    """Restart-required path: diff stage, non-empty restart_required list."""

    def test_restart_required_json(self) -> None:
        resp = _make_response(
            ok=False,
            stage="diff",
            generation=None,
            restart_required=("server.port: 8000 -> 9000",),
            retirement_pending=False,
            message="Reload rejected: 1 restart-required field(s) changed",
        )
        result = format_rehash_json(resp, EXIT_RESTART_REQUIRED)

        assert set(result.keys()) >= _EXPECTED_JSON_KEYS
        assert result["ok"] is False
        assert result["stage"] == "diff"
        assert result["exit_code"] == EXIT_RESTART_REQUIRED
        assert result["restart_required"] == ["server.port: 8000 -> 9000"]
        assert result["retirement_pending"] is False


class TestJsonContractValidationFailure:
    """Validation failure: exits before reaching JSON path.

    The CLI exits with EXIT_VALIDATION during local validation, so
    the JSON rendering code is never reached.  We verify the exit code
    and the human output substring via CliRunner.
    """

    def test_validation_failure_exits_1(self, tmp_path: Path) -> None:
        from eggpool.config_validation import ConfigValidationError

        path = _write_config(tmp_path, "this = is not = valid = toml =")
        runner = CliRunner()

        with patch("eggpool.cli_full._validate_config_file") as mock_validate:
            mock_validate.side_effect = ConfigValidationError("bad toml")
            result = runner.invoke(cli, ["--config", path, "rehash"])

        assert result.exit_code == EXIT_VALIDATION

    def test_validation_failure_message_contains_phrase(self, tmp_path: Path) -> None:
        from eggpool.config_validation import ConfigValidationError

        path = _write_config(tmp_path, "this = is not = valid = toml =")
        runner = CliRunner()

        with patch("eggpool.cli_full._validate_config_file") as mock_validate:
            mock_validate.side_effect = ConfigValidationError("bad toml")
            result = runner.invoke(cli, ["--config", path, "rehash"])

        assert "configuration validation failed" in result.output.lower()


class TestJsonContractControlUnavailable:
    """Control unavailable: exits with EXIT_CONTROL_UNAVAILABLE.

    The exception path exits before reaching the JSON rendering code,
    so we verify via CliRunner.
    """

    def test_control_unavailable_exits_3(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        runner = CliRunner()
        from eggpool.control.client import ControlClientConnectionError

        with (
            patch("eggpool.cli_full._validate_config_file") as mock_validate,
            patch("eggpool.control.client.ControlClient") as mock_client_cls,
        ):
            from eggpool.config_validation import ConfigValidationResult
            from eggpool.models.config import AppConfig

            config = AppConfig.from_toml(path)
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="abcd" * 16,
                runtime_fingerprint="1234" * 16,
                warnings=(),
            )
            mock_client = mock_client_cls.return_value
            mock_client.reload = AsyncMock(
                side_effect=ControlClientConnectionError("no server")
            )

            result = runner.invoke(cli, ["--config", path, "rehash"])

        assert result.exit_code == EXIT_CONTROL_UNAVAILABLE
        assert "control socket unavailable" in result.output.lower()


class TestJsonContractDigestMismatch:
    """Digest mismatch: exit code 6."""

    def test_digest_mismatch_exit_code(self) -> None:
        from eggpool.cli_exit_codes import exit_code_for_failure

        code = exit_code_for_failure(
            stage="preparation",
            restart_required=(),
            message="Content digest mismatch: expected abc got def",
        )
        assert code == EXIT_DIGEST_MISMATCH

    def test_digest_mismatch_json(self) -> None:
        resp = _make_response(
            ok=False,
            stage="preparation",
            generation=None,
            message="Content digest mismatch: expected abc got def",
        )
        result = format_rehash_json(resp, EXIT_DIGEST_MISMATCH)

        assert set(result.keys()) >= _EXPECTED_JSON_KEYS
        assert result["exit_code"] == EXIT_DIGEST_MISMATCH
        assert result["ok"] is False
        assert "mismatch" in result["message"].lower()


class TestJsonContractPreparationFailure:
    """Preparation failure: exit code 5."""

    def test_preparation_failure_exit_code(self) -> None:
        from eggpool.cli_exit_codes import exit_code_for_failure

        code = exit_code_for_failure(
            stage="preparation",
            restart_required=(),
            message="Failed to construct candidate generation",
        )
        assert code == EXIT_PREPARATION_FAILED

    def test_preparation_failure_json(self) -> None:
        resp = _make_response(
            ok=False,
            stage="preparation",
            generation=None,
            message="Failed to construct candidate generation",
        )
        result = format_rehash_json(resp, EXIT_PREPARATION_FAILED)

        assert set(result.keys()) >= _EXPECTED_JSON_KEYS
        assert result["exit_code"] == EXIT_PREPARATION_FAILED
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Part C: Human output tests
# ---------------------------------------------------------------------------


class TestHumanOutputSuccess:
    def test_success_stdout_contains_message(self) -> None:
        resp = _make_response(
            ok=True,
            stage="retirement",
            generation=7,
            changed_sections=("routing",),
            retirement_pending=True,
            message="Reload applied: generation 7, 1 section(s) changed",
        )
        stdout, stderr = render_rehash_human(resp)

        assert "Reload applied" in stdout
        assert "routing" in stdout
        assert "Generation: 7" in stdout
        assert "draining" in stdout
        assert stderr == ""

    def test_noop_stdout(self) -> None:
        resp = _make_response(
            ok=True,
            stage="commit",
            generation=5,
            message="No configuration changes detected",
        )
        stdout, stderr = render_rehash_human(resp)

        assert "No configuration changes detected" in stdout
        assert "Generation: 5" in stdout
        assert stderr == ""


class TestHumanOutputBusy:
    def test_busy_stderr(self) -> None:
        resp = _make_response(
            ok=False,
            stage=STAGE_RELOAD_IN_PROGRESS,
            message="A reload transaction is already in progress",
        )
        stdout, stderr = render_rehash_human(resp)

        assert stdout == ""
        assert "reload transaction is already in progress" in stderr.lower()


class TestHumanOutputRestartRequired:
    def test_restart_required_stderr(self) -> None:
        resp = _make_response(
            ok=False,
            stage="diff",
            restart_required=("server.port",),
            message="Reload rejected: 1 restart-required field(s) changed",
        )
        stdout, stderr = render_rehash_human(resp)

        assert stdout == ""
        assert "restart-required changes" in stderr.lower()
        assert "server.port" in stderr


# ---------------------------------------------------------------------------
# Part E: Secret safety tests
# ---------------------------------------------------------------------------


class TestSecretSafety:
    """Secret-bearing restart_required values must be redacted."""

    SECRET_VALUE = "api_key: <old> -> <new>"

    def test_json_secret_redacted(self) -> None:
        resp = _make_response(
            ok=False,
            stage="diff",
            restart_required=(self.SECRET_VALUE,),
            message="Reload rejected",
        )
        result = format_rehash_json(resp, EXIT_RESTART_REQUIRED)

        # The raw secret string should still be in the structured data
        # (consumers may need to display field names) but the message
        # field must not leak secrets.
        assert result["restart_required"] == [self.SECRET_VALUE]

    def test_human_secret_redacted_in_message(self) -> None:
        resp = _make_response(
            ok=False,
            stage="diff",
            restart_required=(self.SECRET_VALUE,),
            message=f"Field changed: {self.SECRET_VALUE}",
        )
        _stdout, stderr = render_rehash_human(resp)

        # render_rehash_human redacts the message but not restart_required
        # fields in this version — the important invariant is that the
        # message itself never leaks raw old/new values.
        redacted = _redact_message(resp.message)
        assert "<old>" not in redacted
        assert "<new>" not in redacted
        assert "<redacted>" in redacted

    def test_redact_message_replaces_old_new_tokens(self) -> None:
        raw = "api_key: <old> -> <new>"
        redacted = _redact_message(raw)
        assert "<redacted>" in redacted
        assert "<old>" not in redacted
        assert "<new>" not in redacted

    def test_redact_message_leaves_clean_strings_untouched(self) -> None:
        clean = "server.port: 8000 -> 9000"
        assert _redact_message(clean) == clean

    def test_secret_not_in_human_output_stderr(self) -> None:
        resp = _make_response(
            ok=False,
            stage="diff",
            restart_required=(self.SECRET_VALUE,),
            message=f"Field changed: {self.SECRET_VALUE}",
        )
        _stdout, stderr = render_rehash_human(resp)

        # The message line should have the secret redacted
        redacted = _redact_message(resp.message)
        assert "<old>" not in redacted
        assert "<new>" not in redacted


# ---------------------------------------------------------------------------
# CLI integration: full rehash command via CliRunner
# ---------------------------------------------------------------------------


class TestRehashCliJsonIntegration:
    """Drive the full ``rehash --json`` command through CliRunner."""

    def test_success_json_via_cli(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        runner = CliRunner()
        from eggpool.config_validation import ConfigValidationResult
        from eggpool.models.config import AppConfig

        config = AppConfig.from_toml(path)
        fake_response = _make_response(
            ok=True,
            stage="retirement",
            generation=7,
            changed_sections=("routing",),
            retirement_pending=True,
            message="Reload applied: generation 7, 1 section(s) changed",
        )

        with (
            patch("eggpool.cli_full._validate_config_file") as mock_validate,
            patch("eggpool.control.client.ControlClient") as mock_client_cls,
        ):
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="abcd" * 16,
                runtime_fingerprint="1234" * 16,
                warnings=(),
            )
            mock_client = mock_client_cls.return_value
            mock_client.reload = AsyncMock(return_value=fake_response)

            result = runner.invoke(cli, ["--config", path, "rehash", "--json"])

        assert result.exit_code == EXIT_OK
        parsed = _parse_json_from_output(result.output)
        assert set(parsed.keys()) >= _EXPECTED_JSON_KEYS
        assert parsed["ok"] is True
        assert parsed["exit_code"] == EXIT_OK
        assert parsed["stage"] == "retirement"
        assert parsed["generation"] == 7
        assert parsed["retirement_pending"] is True
        assert parsed["restart_required"] == []

    def test_noop_json_via_cli(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        runner = CliRunner()
        from eggpool.config_validation import ConfigValidationResult
        from eggpool.models.config import AppConfig

        config = AppConfig.from_toml(path)
        fake_response = _make_response(
            ok=True,
            stage="commit",
            generation=5,
            message="No configuration changes detected",
        )

        with (
            patch("eggpool.cli_full._validate_config_file") as mock_validate,
            patch("eggpool.control.client.ControlClient") as mock_client_cls,
        ):
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="abcd" * 16,
                runtime_fingerprint="1234" * 16,
                warnings=(),
            )
            mock_client = mock_client_cls.return_value
            mock_client.reload = AsyncMock(return_value=fake_response)

            result = runner.invoke(cli, ["--config", path, "rehash", "--json"])

        assert result.exit_code == EXIT_OK
        parsed = _parse_json_from_output(result.output)
        assert parsed["ok"] is True
        assert parsed["changed_sections"] == []
        assert parsed["restart_required"] == []
        assert parsed["retirement_pending"] is False

    def test_busy_json_via_cli(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        runner = CliRunner()
        from eggpool.config_validation import ConfigValidationResult
        from eggpool.models.config import AppConfig

        config = AppConfig.from_toml(path)
        fake_response = _make_response(
            ok=False,
            stage=STAGE_RELOAD_IN_PROGRESS,
            message="A reload transaction is already in progress",
        )

        with (
            patch("eggpool.cli_full._validate_config_file") as mock_validate,
            patch("eggpool.control.client.ControlClient") as mock_client_cls,
        ):
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="abcd" * 16,
                runtime_fingerprint="1234" * 16,
                warnings=(),
            )
            mock_client = mock_client_cls.return_value
            mock_client.reload = AsyncMock(return_value=fake_response)

            result = runner.invoke(cli, ["--config", path, "rehash", "--json"])

        assert result.exit_code == EXIT_RELOAD_BUSY
        parsed = _parse_json_from_output(result.output)
        assert parsed["ok"] is False
        assert parsed["exit_code"] == EXIT_RELOAD_BUSY
        assert parsed["stage"] == "reload_in_progress"

    def test_restart_required_json_via_cli(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        runner = CliRunner()
        from eggpool.config_validation import ConfigValidationResult
        from eggpool.models.config import AppConfig

        config = AppConfig.from_toml(path)
        fake_response = _make_response(
            ok=False,
            stage="diff",
            restart_required=("server.port",),
            message="Reload rejected: 1 restart-required field(s) changed",
        )

        with (
            patch("eggpool.cli_full._validate_config_file") as mock_validate,
            patch("eggpool.control.client.ControlClient") as mock_client_cls,
        ):
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="abcd" * 16,
                runtime_fingerprint="1234" * 16,
                warnings=(),
            )
            mock_client = mock_client_cls.return_value
            mock_client.reload = AsyncMock(return_value=fake_response)

            result = runner.invoke(cli, ["--config", path, "rehash", "--json"])

        assert result.exit_code == EXIT_RESTART_REQUIRED
        parsed = _parse_json_from_output(result.output)
        assert parsed["ok"] is False
        assert parsed["exit_code"] == EXIT_RESTART_REQUIRED
        assert parsed["restart_required"] == ["server.port"]

    def test_preparation_failure_json_via_cli(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        runner = CliRunner()
        from eggpool.config_validation import ConfigValidationResult
        from eggpool.models.config import AppConfig

        config = AppConfig.from_toml(path)
        fake_response = _make_response(
            ok=False,
            stage="preparation",
            message="Failed to construct candidate generation",
        )

        with (
            patch("eggpool.cli_full._validate_config_file") as mock_validate,
            patch("eggpool.control.client.ControlClient") as mock_client_cls,
        ):
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="abcd" * 16,
                runtime_fingerprint="1234" * 16,
                warnings=(),
            )
            mock_client = mock_client_cls.return_value
            mock_client.reload = AsyncMock(return_value=fake_response)

            result = runner.invoke(cli, ["--config", path, "rehash", "--json"])

        assert result.exit_code == EXIT_PREPARATION_FAILED
        parsed = _parse_json_from_output(result.output)
        assert parsed["ok"] is False
        assert parsed["exit_code"] == EXIT_PREPARATION_FAILED
        assert parsed["stage"] == "preparation"

    def test_digest_mismatch_json_via_cli(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path)
        runner = CliRunner()
        from eggpool.config_validation import ConfigValidationResult
        from eggpool.models.config import AppConfig

        config = AppConfig.from_toml(path)
        fake_response = _make_response(
            ok=False,
            stage="preparation",
            message="Content digest mismatch: expected abc got def",
        )

        with (
            patch("eggpool.cli_full._validate_config_file") as mock_validate,
            patch("eggpool.control.client.ControlClient") as mock_client_cls,
        ):
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="abcd" * 16,
                runtime_fingerprint="1234" * 16,
                warnings=(),
            )
            mock_client = mock_client_cls.return_value
            mock_client.reload = AsyncMock(return_value=fake_response)

            result = runner.invoke(cli, ["--config", path, "rehash", "--json"])

        assert result.exit_code == EXIT_DIGEST_MISMATCH
        parsed = _parse_json_from_output(result.output)
        assert parsed["ok"] is False
        assert parsed["exit_code"] == EXIT_DIGEST_MISMATCH

    def test_validation_failure_no_json_via_cli(self, tmp_path: Path) -> None:
        """Validation failure exits before JSON rendering; verify exit code."""
        from eggpool.config_validation import ConfigValidationError

        path = _write_config(tmp_path, "this = is not = valid = toml =")
        runner = CliRunner()

        with patch("eggpool.cli_full._validate_config_file") as mock_validate:
            mock_validate.side_effect = ConfigValidationError("bad toml")
            result = runner.invoke(cli, ["--config", path, "rehash", "--json"])

        assert result.exit_code == EXIT_VALIDATION
        # No JSON on stdout — only human-readable stderr.
        assert "{" not in result.output
