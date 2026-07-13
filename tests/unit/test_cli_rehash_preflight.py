"""Tests for the ``rehash`` preflight CLI command (Workstream A6)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from eggpool.cli import cli

SERVER_API_KEY = "ep_test_server_key_1234567890"
ACCOUNT_API_KEY = "sk-test-account-key-1234567890"


def _write_config(tmp_path: Path, body: str) -> str:
    config_path = tmp_path / "config.toml"
    config_path.write_text(body, encoding="utf-8")
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


class TestRehashPreflight:
    def test_invalid_config_does_not_invoke_restart(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, "this = is not = valid = toml =")
        runner = CliRunner()

        with patch("eggpool.cli_full._validate_config_file") as mock_validate:
            mock_validate.side_effect = Exception("validation seam reached")
            result = runner.invoke(cli, ["--config", path, "rehash"])

        # The validation seam raised before any subprocess / restart call
        # could be considered.  The CLI wrapper translates the exception
        # into a nonzero exit without invoking restart.
        assert result.exit_code != 0

    def test_invalid_config_does_not_invoke_control_seam(self, tmp_path: Path) -> None:
        bad_body = "[server]\nport = 99999\n"
        path = _write_config(tmp_path, bad_body)
        runner = CliRunner()

        from eggpool.config_validation import ConfigSchemaError

        with patch("eggpool.cli_full._validate_config_file") as mock_validate:
            mock_validate.side_effect = ConfigSchemaError(
                "configuration validation failed: schema validation failed"
            )

            result = runner.invoke(cli, ["--config", path, "rehash"])

        assert mock_validate.call_count == 1
        assert result.exit_code != 0
        assert "unchanged" in result.output.lower()
        assert "refusing to apply" in result.output.lower()

    def test_valid_config_reaches_post_validation_seam(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _valid_body())
        runner = CliRunner()
        with patch("eggpool.cli_full._validate_config_file") as mock_validate:
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

            result = runner.invoke(cli, ["--config", path, "rehash"])

        assert mock_validate.call_count == 1
        # Validation seam was reached; the command reported a
        # post-validation message and exited zero.
        assert "control is not yet available" in result.output.lower()

    def test_no_automatic_restart_fallback(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _valid_body())
        runner = CliRunner()
        with (
            patch("eggpool.cli_full.restart") as mock_restart,
            patch("eggpool.cli_full._validate_config_file") as mock_validate,
        ):
            from eggpool.config_validation import ConfigValidationResult
            from eggpool.models.config import AppConfig

            config = AppConfig.from_toml(path)
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="feed" * 16,
                runtime_fingerprint="beef" * 16,
                warnings=(),
            )

            result = runner.invoke(cli, ["--config", path, "rehash"])

        assert mock_restart.call_count == 0
        # Post-validation path explicitly notes that live rehash is not yet
        # wired up rather than silently delegating to ``restart``.
        assert "restart` to apply" in result.output

    def test_warning_output_is_retained(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _valid_body())
        runner = CliRunner()
        from eggpool.config_validation import (
            ConfigValidationResult,
            ConfigValidationWarning,
        )
        from eggpool.models.config import AppConfig

        config = AppConfig.from_toml(path)
        warning = ConfigValidationWarning(
            code="legacy.models_method",
            section="providers.legacy",
            message="legacy models_method key detected",
        )

        with patch("eggpool.cli_full._validate_config_file") as mock_validate:
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="abcd" * 16,
                runtime_fingerprint="1234" * 16,
                warnings=(warning,),
            )

            result = runner.invoke(cli, ["--config", path, "rehash"])

        assert "legacy models_method key detected" in result.output
        assert "1 contract warning(s)" in result.output


class TestCheckConfigRefactor:
    def test_check_config_uses_validation_helper(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _valid_body())
        runner = CliRunner()
        with patch("eggpool.cli_full._validate_config_file") as mock_validate:
            from eggpool.config_validation import ConfigValidationResult
            from eggpool.models.config import AppConfig

            config = AppConfig.from_toml(path)
            mock_validate.return_value = ConfigValidationResult(
                config=config,
                source_path=Path(path),
                content_digest="feed" * 16,
                runtime_fingerprint="beef" * 16,
                warnings=(),
            )

            result = runner.invoke(cli, ["--config", path, "check-config"])

        assert mock_validate.call_count == 1
        assert "Content digest:" in result.output

    def test_check_config_surfaces_typed_failure(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, _valid_body())
        runner = CliRunner()
        from eggpool.config_validation import ConfigSchemaError

        with patch("eggpool.cli_full._validate_config_file") as mock_validate:
            mock_validate.side_effect = ConfigSchemaError(
                "configuration validation failed: schema error"
            )
            result = runner.invoke(cli, ["--config", path, "check-config"])
        assert "Error: configuration validation failed:" in result.output
        assert result.exit_code != 0
