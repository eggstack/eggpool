"""Tests for stale-contract detection warnings emitted by ``check-config``.

Each scenario constructs a TOML config that exercises one stale-contract
rule and asserts that the warning fires (or, for the clean-provider case,
does not). Some scenarios that conflict with structural validators
(duplicate ``/v1`` segments) construct an :class:`AppConfig` directly so
the warning logic can be exercised independently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from click.testing import CliRunner

from eggpool.cli import _check_stale_contracts, cli
from eggpool.models.config import (
    AppConfig,
    ProviderConfig,
    ProviderModelsEndpointConfig,
)

if TYPE_CHECKING:
    from pathlib import Path


SERVER_API_KEY = "ep_test_server_key_1234567890"
ACCOUNT_API_KEY = "sk-test-account-key-1234567890"


def _write_config(tmp_path: Path, body: str) -> str:
    """Write a TOML config under ``tmp_path`` and return its path string."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(body, encoding="utf-8")
    return str(config_path)


def _run_check_config(tmp_path: Path, body: str) -> tuple[int, str]:
    """Run ``check-config`` for ``body`` and return ``(exit_code, output)``."""
    config_path = _write_config(tmp_path, body)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["--config", config_path, "check-config"],
    )
    return result.exit_code, result.output


class TestStaleContractWarnings:
    """Verify each stale-contract rule emits the expected advisory."""

    def test_disabled_endpoint_with_no_static_seeds(self, tmp_path: Path) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.minimax]\n"
            'id = "minimax"\n'
            'base_url = "https://api.minimax.io/anthropic"\n'
            'protocols = ["openai", "anthropic"]\n'
            "\n[providers.minimax.models_endpoint]\n"
            'method = "DISABLED"\n'
            "required = false\n"
            "\n[providers.minimax.verify]\n"
            "require_models = false\n"
            "\n[[providers.minimax.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "models_endpoint is DISABLED but static_models is empty" in output
        assert "1 contract warning(s)" in output

    def test_disabled_endpoint_with_require_models_true(
        self,
        tmp_path: Path,
    ) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.minimax]\n"
            'id = "minimax"\n'
            'base_url = "https://api.minimax.io/anthropic"\n'
            'protocols = ["openai", "anthropic"]\n'
            "\n[providers.minimax.models_endpoint]\n"
            'method = "DISABLED"\n'
            "required = false\n"
            "\n[[providers.minimax.static_models]]\n"
            'id = "minimax-m2.7"\n'
            'protocol = "anthropic"\n'
            "\n[providers.minimax.verify]\n"
            "require_models = true\n"
            "\n[[providers.minimax.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "verify.require_models is true" in output

    def test_anthropic_path_without_anthropic_protocol(
        self,
        tmp_path: Path,
    ) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.p1]\n"
            'id = "p1"\n'
            'base_url = "https://api.example.com"\n'
            'protocols = ["openai"]\n'
            'anthropic_path = "/v1/messages"\n'
            "\n[[providers.p1.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "anthropic_path is set but 'anthropic' is not in protocols" in output

    def test_openai_path_without_openai_protocol(self, tmp_path: Path) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.p1]\n"
            'id = "p1"\n'
            'base_url = "https://api.example.com"\n'
            'protocols = ["anthropic"]\n'
            'openai_path = "/chat/completions"\n'
            "\n[[providers.p1.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "openai_path is set but 'openai' is not in protocols" in output

    def test_default_openai_path_does_not_warn_for_anthropic_provider(
        self,
        tmp_path: Path,
    ) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.minimax]\n"
            'id = "minimax"\n'
            'base_url = "https://api.minimax.io/anthropic"\n'
            'protocols = ["anthropic"]\n'
            'anthropic_path = "/v1/messages"\n'
            "\n[providers.minimax.models_endpoint]\n"
            'method = "GET"\n'
            'path = "/v1/models"\n'
            "\n[providers.minimax.auth]\n"
            'mode = "api_key"\n'
            'header = "x-api-key"\n'
            "\n[[providers.minimax.headers]]\n"
            'name = "anthropic-version"\n'
            'value = "2023-06-01"\n'
            "\n[[providers.minimax.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "openai_path is set but 'openai' is not in protocols" not in output

    def test_authorization_static_header_with_non_default_auth_header(
        self,
        tmp_path: Path,
    ) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.p1]\n"
            'id = "p1"\n'
            'base_url = "https://api.example.com"\n'
            'protocols = ["openai"]\n'
            "\n[providers.p1.auth]\n"
            'mode = "api_key"\n'
            'header = "X-Auth-Token"\n'
            "\n[[providers.p1.headers]]\n"
            'name = "Authorization"\n'
            'value = "shadowed-static-token"\n'
            "\n[[providers.p1.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "static header 'Authorization' is set" in output
        assert "auth.mode is 'api_key'" in output

    def test_anthropic_provider_with_authorization_auth_header(
        self,
        tmp_path: Path,
    ) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.p1]\n"
            'id = "p1"\n'
            'base_url = "https://api.example.com/v1"\n'
            'protocols = ["anthropic"]\n'
            'anthropic_path = "/messages"\n'
            "\n[providers.p1.auth]\n"
            'mode = "api_key"\n'
            'header = "Authorization"\n'
            "\n[[providers.p1.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "auth.mode='api_key' with header='Authorization'" in output
        assert "header='x-api-key'" in output

    def test_legacy_models_method_key_without_models_endpoint_table(
        self,
        tmp_path: Path,
    ) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.p1]\n"
            'id = "p1"\n'
            'base_url = "https://api.example.com"\n'
            'protocols = ["openai"]\n'
            'models_method = "POST"\n'
            "\n[[providers.p1.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "legacy models_method/models_path" in output
        assert "[[providers.p1.models_endpoint]]" in output


class TestCheckConfigExitBehavior:
    """Warnings do not flip the exit code; only real errors do."""

    def test_clean_provider_emits_no_warnings(self, tmp_path: Path) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.opencode-go]\n"
            'id = "opencode-go"\n'
            'base_url = "https://opencode.ai/zen/go/v1"\n'
            'protocols = ["openai", "anthropic"]\n'
            "\n[providers.opencode-go.models_endpoint]\n"
            'method = "GET"\n'
            'path = "/models"\n'
            "\n[[providers.opencode-go.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "warning:" not in output
        assert "contract warning(s)" not in output

    def test_warnings_alone_exit_zero(self, tmp_path: Path) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.p1]\n"
            'id = "p1"\n'
            'base_url = "https://api.example.com"\n'
            'protocols = ["openai"]\n'
            'anthropic_path = "/v1/messages"\n'
            "\n[[providers.p1.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        exit_code, output = _run_check_config(tmp_path, body)

        assert exit_code == 0, output
        assert "warning:" in output
        assert "1 contract warning(s)" in output


class TestCheckStaleContractsDirect:
    """Direct unit tests for ``_check_stale_contracts`` edge cases.

    Some stale-contract shapes (notably duplicate ``/v1`` segments) are
    rejected by the structural validators before ``check-config`` ever
    calls ``_check_stale_contracts``. To still exercise the warning
    logic, these tests construct an :class:`AppConfig` directly via
    ``model_construct`` so the validators are skipped.
    """

    def test_duplicate_v1_segment_emits_warning(self) -> None:
        provider = ProviderConfig.model_construct(
            id="bad",
            base_url="https://api.example.com/v1",
            protocols=["openai"],
            openai_path="/chat/completions",
            anthropic_path="/messages",
            models_method="GET",
            models_path="/models",
            models_endpoint=ProviderModelsEndpointConfig.model_construct(
                method="GET",
                path="/v1/models",
                body=None,
                query={},
                required=True,
            ),
            auth=ProviderConfig.model_fields["auth"].default_factory(),
            headers=[],
            static_models=[],
            verify=ProviderConfig.model_fields["verify"].default_factory(),
            accounts=[],
        )
        config = AppConfig.model_construct(
            providers={"bad": provider},
        )

        warnings = _check_stale_contracts(config, "/nonexistent/path.toml")

        assert any("duplicate /v1 segment" in message for message in warnings), warnings


class TestCheckConfigParseErrors:
    """Check-config still surfaces structural errors before warning logic."""

    def test_duplicate_v1_reports_error(self, tmp_path: Path) -> None:
        body = (
            f'[server]\napi_key = "{SERVER_API_KEY}"\n\n'
            "[providers.bad]\n"
            'id = "bad"\n'
            'base_url = "https://api.example.com/v1"\n'
            'protocols = ["openai"]\n'
            'models_method = "GET"\n'
            'models_path = "/v1/models"\n'
            "\n[[providers.bad.accounts]]\n"
            'name = "default"\n'
            f'api_key = "{ACCOUNT_API_KEY}"\n'
            "enabled = true\n"
            "weight = 1.0\n"
        )
        config_path = _write_config(tmp_path, body)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["--config", config_path, "check-config"],
        )

        assert result.exit_code != 0
        assert "Error:" in result.output
        assert "duplicate version prefix" in result.output
