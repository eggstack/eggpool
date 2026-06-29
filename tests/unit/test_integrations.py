"""Tests for integration renderers, config utilities, and configsetup CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from eggpool.cli import cli
from eggpool.config_utils import (
    detect_lan_ip,
    generate_api_key,
    read_server_api_key,
    read_server_port,
    write_server_api_key,
)
from eggpool.integrations.aider import build_aider_env_snippet
from eggpool.integrations.cline import build_cline_profile_snippet
from eggpool.integrations.codex import build_codex_toml_snippet
from eggpool.integrations.common import (
    IntegrationContext,
    list_catalog_model_ids,
    require_model_for_target,
    select_default_model,
)
from eggpool.integrations.continue_dev import build_continue_yaml_snippet
from eggpool.integrations.goose import build_goose_env_snippet
from eggpool.integrations.kilo import build_kilo_openai_compatible_snippet
from eggpool.integrations.openhands import build_openhands_env_snippet
from eggpool.integrations.qwen_code import build_qwen_code_provider_snippet
from eggpool.integrations.roo_code import build_roo_code_profile_snippet

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_CONFIG_TOML = """\
[server]
api_key = "ep_test_key_123"
port = 11300
"""


@pytest.fixture()
def ctx_empty() -> IntegrationContext:
    """A context with no models."""
    return IntegrationContext(
        config_path="/dev/null",
        api_key="ep_test_key_123",
        base_url="http://192.168.1.100:11300/v1",
        base_url_root="http://192.168.1.100:11300",
        host="192.168.1.100",
        port=11300,
    )


@pytest.fixture()
def ctx_one_model() -> IntegrationContext:
    """A context with exactly one model."""
    return IntegrationContext(
        config_path="/dev/null",
        api_key="ep_test_key_123",
        base_url="http://192.168.1.100:11300/v1",
        base_url_root="http://192.168.1.100:11300",
        host="192.168.1.100",
        port=11300,
        models=[
            {
                "model_id": "gpt-4o/openai",
                "display_name": "GPT-4o",
                "capabilities": {},
                "source_metadata": {},
                "effective_limits": {"context_tokens": 128000},
            }
        ],
    )


@pytest.fixture()
def ctx_many_models() -> IntegrationContext:
    """A context with multiple models."""
    return IntegrationContext(
        config_path="/dev/null",
        api_key="ep_test_key_123",
        base_url="http://192.168.1.100:11300/v1",
        base_url_root="http://192.168.1.100:11300",
        host="192.168.1.100",
        port=11300,
        models=[
            {
                "model_id": "gpt-4o/openai",
                "display_name": "GPT-4o",
                "capabilities": {},
                "source_metadata": {},
                "effective_limits": {},
            },
            {
                "model_id": "claude-sonnet/anthropic",
                "display_name": "Claude Sonnet",
                "capabilities": {},
                "source_metadata": {},
                "effective_limits": {},
            },
            {
                "model_id": "gemini-pro/google",
                "display_name": "Gemini Pro",
                "capabilities": {},
                "source_metadata": {},
                "effective_limits": {},
            },
        ],
    )


@pytest.fixture()
def minimal_config(tmp_path: Path) -> Path:
    """Create a minimal config file with api_key and port."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(MINIMAL_CONFIG_TOML, encoding="utf-8")
    return config_file


# ---------------------------------------------------------------------------
# IntegrationContext unit tests
# ---------------------------------------------------------------------------


class TestIntegrationContext:
    def test_list_catalog_model_ids_empty(self, ctx_empty: IntegrationContext) -> None:
        result = list_catalog_model_ids(ctx_empty)
        assert result == []

    def test_list_catalog_model_ids_sorted(
        self, ctx_many_models: IntegrationContext
    ) -> None:
        result = list_catalog_model_ids(ctx_many_models)
        assert result == sorted(result)
        assert "claude-sonnet/anthropic" in result
        assert "gpt-4o/openai" in result
        assert "gemini-pro/google" in result

    def test_select_default_model_none_when_empty(
        self, ctx_empty: IntegrationContext
    ) -> None:
        assert select_default_model(ctx_empty) is None

    def test_select_default_model_none_when_many(
        self, ctx_many_models: IntegrationContext
    ) -> None:
        assert select_default_model(ctx_many_models) is None

    def test_select_default_model_returns_when_one(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        assert select_default_model(ctx_one_model) == "gpt-4o/openai"

    def test_require_model_explicit_passthrough(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        result = require_model_for_target("aider", "some-model", ctx_one_model)
        assert result == "some-model"

    def test_require_model_falls_back_to_default(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        result = require_model_for_target("aider", None, ctx_one_model)
        assert result == "gpt-4o/openai"

    def test_require_model_none_when_ambiguous(
        self, ctx_many_models: IntegrationContext
    ) -> None:
        result = require_model_for_target("aider", None, ctx_many_models)
        assert result is None

    def test_integration_context_frozen(self, ctx_empty: IntegrationContext) -> None:
        with pytest.raises(AttributeError):
            ctx_empty.api_key = "new_key"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------


class TestAiderRenderer:
    def test_env_snippet_contains_key_and_base(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_aider_env_snippet(ctx_one_model)
        assert "OPENAI_API_KEY=ep_test_key_123" in snippet
        assert "OPENAI_API_BASE=http://192.168.1.100:11300/v1" in snippet

    def test_env_snippet_with_model(self, ctx_one_model: IntegrationContext) -> None:
        snippet = build_aider_env_snippet(ctx_one_model, model="gpt-4o/openai")
        assert "--model gpt-4o/openai" in snippet

    def test_env_snippet_without_model(self, ctx_one_model: IntegrationContext) -> None:
        snippet = build_aider_env_snippet(ctx_one_model, model=None)
        assert "--model" not in snippet


class TestCodexRenderer:
    def test_toml_snippet_structure(self, ctx_empty: IntegrationContext) -> None:
        snippet = build_codex_toml_snippet(ctx_empty)
        assert "[provider.eggpool]" in snippet
        assert "base_url" in snippet
        assert "api_key" in snippet

    def test_toml_snippet_with_model(self, ctx_empty: IntegrationContext) -> None:
        snippet = build_codex_toml_snippet(ctx_empty, model="gpt-4o")
        assert 'default_model = "gpt-4o"' in snippet

    def test_toml_snippet_model_sections(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_codex_toml_snippet(ctx_one_model)
        assert "[provider.eggpool.models.gpt-4o/openai]" in snippet
        assert "context_window = 128000" in snippet


class TestQwenCodeRenderer:
    def test_produces_valid_json(self, ctx_empty: IntegrationContext) -> None:
        snippet = build_qwen_code_provider_snippet(ctx_empty)
        data = json.loads(snippet)
        assert data["name"] == "EggPool"
        assert data["type"] == "openai"
        assert data["base_url"] == ctx_empty.base_url
        assert data["api_key"] == ctx_empty.api_key

    def test_produces_valid_json_with_model(
        self, ctx_empty: IntegrationContext
    ) -> None:
        snippet = build_qwen_code_provider_snippet(ctx_empty, model="gpt-4o")
        data = json.loads(snippet)
        assert data["model"] == "gpt-4o"


class TestKiloRenderer:
    def test_produces_valid_json(self, ctx_empty: IntegrationContext) -> None:
        snippet = build_kilo_openai_compatible_snippet(ctx_empty)
        data = json.loads(snippet)
        assert "openai_compatible" in data
        provider = data["openai_compatible"]
        assert provider["name"] == "EggPool"
        assert provider["apiBase"] == ctx_empty.base_url
        assert provider["apiKey"] == ctx_empty.api_key

    def test_model_entries_populated(self, ctx_one_model: IntegrationContext) -> None:
        snippet = build_kilo_openai_compatible_snippet(ctx_one_model)
        data = json.loads(snippet)
        assert "gpt-4o/openai" in data["openai_compatible"]["models"]
        assert (
            data["openai_compatible"]["models"]["gpt-4o/openai"]["context_length"]
            == 128000
        )

    def test_explicit_model_added_when_not_in_catalog(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_kilo_openai_compatible_snippet(
            ctx_one_model, model="extra-model"
        )
        data = json.loads(snippet)
        assert "extra-model" in data["openai_compatible"]["models"]


class TestContinueDevRenderer:
    def test_yaml_snippet_contains_required_keys(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_continue_yaml_snippet(ctx_one_model)
        assert "models:" in snippet
        assert "provider:" in snippet
        assert "apiBase:" in snippet
        assert "apiKey:" in snippet
        assert "ep_test_key_123" in snippet

    def test_yaml_snippet_with_explicit_model(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_continue_yaml_snippet(ctx_one_model, model="custom-model")
        assert "custom-model" in snippet

    def test_yaml_snippet_falls_back_to_single_model(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_continue_yaml_snippet(ctx_one_model)
        assert "gpt-4o/openai" in snippet

    def test_yaml_snippet_empty_model_when_ambiguous(
        self, ctx_many_models: IntegrationContext
    ) -> None:
        snippet = build_continue_yaml_snippet(ctx_many_models)
        assert "model:" in snippet
        # model should be empty when ambiguous
        for line in snippet.split("\n"):
            if "model:" in line:
                assert line.strip().endswith('model: ""') or line.strip().endswith(
                    "model: "
                )


class TestClineRenderer:
    def test_produces_valid_json(self, ctx_empty: IntegrationContext) -> None:
        snippet = build_cline_profile_snippet(ctx_empty)
        data = json.loads(snippet)
        assert data["apiProvider"] == "openai-compatible"
        assert data["openAiBaseUrl"] == ctx_empty.base_url
        assert data["openAiApiKey"] == ctx_empty.api_key

    def test_includes_model_when_one(self, ctx_one_model: IntegrationContext) -> None:
        snippet = build_cline_profile_snippet(ctx_one_model)
        data = json.loads(snippet)
        assert data["openAiModelId"] == "gpt-4o/openai"

    def test_no_model_when_ambiguous(self, ctx_many_models: IntegrationContext) -> None:
        snippet = build_cline_profile_snippet(ctx_many_models)
        data = json.loads(snippet)
        assert "openAiModelId" not in data


class TestRooCodeRenderer:
    def test_produces_valid_json(self, ctx_empty: IntegrationContext) -> None:
        snippet = build_roo_code_profile_snippet(ctx_empty)
        data = json.loads(snippet)
        assert data["apiProvider"] == "openai-compatible"
        assert data["openAiBaseUrl"] == ctx_empty.base_url
        assert data["openAiApiKey"] == ctx_empty.api_key

    def test_includes_model_when_one(self, ctx_one_model: IntegrationContext) -> None:
        snippet = build_roo_code_profile_snippet(ctx_one_model)
        data = json.loads(snippet)
        assert data["openAiModelId"] == "gpt-4o/openai"

    def test_no_model_when_ambiguous(self, ctx_many_models: IntegrationContext) -> None:
        snippet = build_roo_code_profile_snippet(ctx_many_models)
        data = json.loads(snippet)
        assert "openAiModelId" not in data


class TestGooseRenderer:
    def test_env_snippet_contains_expected_vars(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_goose_env_snippet(ctx_one_model)
        assert "GOOSE_PROVIDER__BASE_URL" in snippet
        assert "GOOSE_PROVIDER__API_KEY" in snippet

    def test_env_snippet_with_model(self, ctx_one_model: IntegrationContext) -> None:
        snippet = build_goose_env_snippet(ctx_one_model, model="gpt-4o/openai")
        assert "GOOSE_PROVIDER__MODEL=gpt-4o/openai" in snippet

    def test_env_snippet_without_model(self, ctx_one_model: IntegrationContext) -> None:
        snippet = build_goose_env_snippet(ctx_one_model, model=None)
        assert "GOOSE_PROVIDER__MODEL" not in snippet


class TestOpenHandsRenderer:
    def test_env_snippet_contains_expected_vars(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_openhands_env_snippet(ctx_one_model)
        assert "LLM_BASE_URL" in snippet
        assert "LLM_API_KEY" in snippet

    def test_env_snippet_with_model(self, ctx_one_model: IntegrationContext) -> None:
        snippet = build_openhands_env_snippet(ctx_one_model, model="gpt-4o/openai")
        assert "LLM_MODEL=gpt-4o/openai" in snippet

    def test_env_snippet_falls_back_to_single_model(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_openhands_env_snippet(ctx_one_model, model=None)
        assert "LLM_MODEL=gpt-4o/openai" in snippet


# ---------------------------------------------------------------------------
# Config utils tests
# ---------------------------------------------------------------------------


class TestConfigUtils:
    def test_generate_api_key_prefix(self) -> None:
        key = generate_api_key()
        assert key.startswith("ep_")
        assert len(key) > 3

    def test_generate_api_key_unique(self) -> None:
        keys = {generate_api_key() for _ in range(10)}
        assert len(keys) == 10

    def test_write_and_read_server_api_key(self, minimal_config: Path) -> None:
        success, warning = write_server_api_key(str(minimal_config), "ep_new_key_456")
        assert success is True
        assert warning is None
        assert read_server_api_key(str(minimal_config)) == "ep_new_key_456"

    def test_read_server_api_key_missing_section(self, tmp_path: Path) -> None:
        config_file = tmp_path / "empty.toml"
        config_file.write_text("", encoding="utf-8")
        assert read_server_api_key(str(config_file)) == ""

    def test_read_server_port(self, minimal_config: Path) -> None:
        port = read_server_port(str(minimal_config))
        assert port == 11300

    def test_read_server_port_defaults_when_missing(self, tmp_path: Path) -> None:
        config_file = tmp_path / "no_port.toml"
        config_file.write_text("[server]\n", encoding="utf-8")
        port = read_server_port(str(config_file))
        assert port == 11300  # DEFAULT_PORT

    def test_detect_lan_ip_returns_valid_ip(self) -> None:
        ip = detect_lan_ip()
        parts = ip.split(".")
        assert len(parts) == 4
        assert all(p.isdigit() for p in parts)
        assert all(0 <= int(p) <= 255 for p in parts)


# ---------------------------------------------------------------------------
# CLI configsetup command tests
# ---------------------------------------------------------------------------


def _make_config_for_cli(tmp_path: Path, content: str = MINIMAL_CONFIG_TOML) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text(content, encoding="utf-8")
    return config_file


class TestConfigSetupCLI:
    @pytest.mark.parametrize(
        "subcommand",
        [
            "aider",
            "codex",
            "qwen-code",
            "kilo",
            "continue",
            "cline",
            "roo-code",
            "goose",
            "openhands",
        ],
    )
    def test_configsetup_exits_zero(self, tmp_path: Path, subcommand: str) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                subcommand,
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        "subcommand",
        [
            "aider",
            "codex",
            "qwen-code",
            "kilo",
            "continue",
            "cline",
            "roo-code",
            "goose",
            "openhands",
        ],
    )
    def test_configsetup_produces_output(self, tmp_path: Path, subcommand: str) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                subcommand,
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code == 0
        assert len(result.output) > 0

    @pytest.mark.parametrize(
        "subcommand,expected_fragment",
        [
            ("aider", "OPENAI_API_KEY"),
            ("codex", "[provider.eggpool]"),
            ("qwen-code", '"name": "EggPool"'),
            ("kilo", "openai_compatible"),
            ("continue", "models:"),
            ("cline", '"apiProvider"'),
            ("roo-code", '"apiProvider"'),
            ("goose", "GOOSE_PROVIDER__BASE_URL"),
            ("openhands", "LLM_BASE_URL"),
        ],
    )
    def test_configsetup_output_contains_expected_content(
        self, tmp_path: Path, subcommand: str, expected_fragment: str
    ) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                subcommand,
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code == 0
        assert expected_fragment in result.output

    def test_configsetup_aider_model_option(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "aider",
                "--model",
                "gpt-4o",
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code == 0
        assert "gpt-4o" in result.output
        assert "--model" in result.output

    def test_configsetup_codex_model_option(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "codex",
                "--model",
                "gpt-4o",
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code == 0
        assert "gpt-4o" in result.output
        assert "default_model" in result.output

    def test_configsetup_qwen_code_model_option(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "qwen-code",
                "--model",
                "gpt-4o",
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["model"] == "gpt-4o"
