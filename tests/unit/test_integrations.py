"""Tests for integration renderers, config utilities, and configsetup CLI."""

from __future__ import annotations

import json
import os
import tomllib
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from eggpool.cli import cli
from eggpool.config_utils import (
    ServerKeyResolution,
    detect_lan_ip,
    generate_api_key,
    read_server_api_key,
    read_server_port,
    resolve_server_api_key,
    write_server_api_key,
)
from eggpool.integrations.aider import build_aider_env_snippet
from eggpool.integrations.cline import build_cline_profile_snippet
from eggpool.integrations.codex import build_codex_toml_snippet
from eggpool.integrations.common import (
    TARGET_SPECS,
    ConfigsetupTargetSpec,
    IntegrationContext,
    _openai_client_needs_transcoder,
    _persist_transcoder_enabled,
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
from eggpool.models.config import (
    AccountConfig,
    AppConfig,
    ProviderConfig,
)
from eggpool.transcoder.policy import TranscoderPolicy

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
        assert '[provider.eggpool.models."gpt-4o/openai"]' in snippet
        assert "context_window = 128000" in snippet

    def test_toml_snippet_bare_model_id(
        self, ctx_many_models: IntegrationContext
    ) -> None:
        ctx_many_models.models[0]["model_id"] = "gpt-4o"
        snippet = build_codex_toml_snippet(ctx_many_models)
        assert "[provider.eggpool.models.gpt-4o]" in snippet


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
        # model key should be omitted entirely when ambiguous
        assert "model:" not in snippet

    def test_yaml_snippet_produces_valid_structure(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        snippet = build_continue_yaml_snippet(ctx_one_model)
        lines = snippet.split("\n")
        assert lines[0] == "models:"
        assert lines[1].startswith("  - title:")
        assert lines[2].startswith("    provider:")
        assert lines[3].startswith("    model:")
        assert lines[4].startswith("    apiBase:")
        assert lines[5].startswith("    apiKey:")


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


# ---------------------------------------------------------------------------
# resolve_server_api_key tests
# ---------------------------------------------------------------------------

INLINE_KEY_CONFIG = """\
[server]
api_key = "ep_inline_key_123"
port = 11300
"""

ENV_KEY_CONFIG = """\
[server]
api_key_env = "TEST_EGGPOOL_KEY"
port = 11300
"""

NO_KEY_CONFIG = """\
[server]
port = 11300
"""


class TestResolveServerApiKey:
    def test_inline_key_reused(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(INLINE_KEY_CONFIG, encoding="utf-8")
        result = resolve_server_api_key(str(config_file))
        assert result.api_key == "ep_inline_key_123"
        assert result.source == "inline"
        assert result.config_mutated is False

    def test_env_key_used_when_present(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(ENV_KEY_CONFIG, encoding="utf-8")
        with patch.dict(os.environ, {"TEST_EGGPOOL_KEY": "ep_env_key_456"}):
            result = resolve_server_api_key(str(config_file))
        assert result.api_key == "ep_env_key_456"
        assert result.source == "env"
        assert result.env_var == "TEST_EGGPOOL_KEY"
        assert result.config_mutated is False

    def test_env_key_absent_exits(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(ENV_KEY_CONFIG, encoding="utf-8")
        with (
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(SystemExit, match="api_key_env is set to"),
        ):
            resolve_server_api_key(str(config_file))

    def test_generates_and_persists_when_nothing_configured(
        self, tmp_path: Path
    ) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(NO_KEY_CONFIG, encoding="utf-8")
        result = resolve_server_api_key(str(config_file))
        assert result.api_key.startswith("ep_")
        assert result.source == "generated"
        assert result.config_mutated is True
        # Verify the key was actually persisted
        assert read_server_api_key(str(config_file)) == result.api_key

    def test_server_key_resolution_frozen(self) -> None:
        r = ServerKeyResolution(api_key="k", source="inline")
        with pytest.raises(AttributeError):
            r.api_key = "new"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Transcoder persistence tests
# ---------------------------------------------------------------------------


def _make_appconfig(
    *,
    transcoder_enabled: bool = False,
    providers: dict[str, ProviderConfig] | None = None,
) -> AppConfig:
    """Build a minimal AppConfig for testing."""
    return AppConfig(
        transcoder=TranscoderPolicy(enabled=transcoder_enabled),
        providers=providers or {},
    )


def _make_provider(
    provider_id: str,
    protocols: list[str],
    accounts: list[AccountConfig] | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        id=provider_id,
        base_url=f"https://{provider_id}.example/v1",
        protocols=protocols,  # type: ignore[arg-type]
        accounts=accounts or [AccountConfig(name="acct", api_key_env="K")],
    )


class TestTranscoderPersistence:
    def test_needs_transcoder_when_anthropic_only(self) -> None:
        provider = _make_provider("anthropic", ["anthropic"])
        config = _make_appconfig(providers={"anthropic": provider})
        assert _openai_client_needs_transcoder(config) is True

    def test_no_transcoder_when_openai_only(self) -> None:
        provider = _make_provider("openai", ["openai"])
        config = _make_appconfig(providers={"openai": provider})
        assert _openai_client_needs_transcoder(config) is False

    def test_no_transcoder_when_dual_protocol(self) -> None:
        provider = _make_provider("both", ["openai", "anthropic"])
        config = _make_appconfig(providers={"both": provider})
        assert _openai_client_needs_transcoder(config) is False

    def test_no_transcoder_when_already_enabled(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[transcoder]\nenabled = true\n", encoding="utf-8")
        provider = _make_provider("anthropic", ["anthropic"])
        config = _make_appconfig(
            transcoder_enabled=True, providers={"anthropic": provider}
        )
        assert _persist_transcoder_enabled(str(config_file), config) is False

    def test_persist_writes_to_toml(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[server]\nport = 11300\n", encoding="utf-8")
        provider = _make_provider("anthropic", ["anthropic"])
        config = _make_appconfig(
            transcoder_enabled=False, providers={"anthropic": provider}
        )
        assert _persist_transcoder_enabled(str(config_file), config) is True
        content = config_file.read_text(encoding="utf-8")
        assert "enabled = true" in content
        # Verify it's valid TOML
        parsed = tomllib.loads(content)
        assert parsed["transcoder"]["enabled"] is True

    def test_no_persist_when_no_transcoder_needed(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text("[server]\nport = 11300\n", encoding="utf-8")
        provider = _make_provider("openai", ["openai"])
        config = _make_appconfig(
            transcoder_enabled=False, providers={"openai": provider}
        )
        assert _persist_transcoder_enabled(str(config_file), config) is False


# ---------------------------------------------------------------------------
# _restart_after_integration_context_mutation tests
# ---------------------------------------------------------------------------


class TestRestartAfterMutation:
    def test_restarts_when_config_mutated(self) -> None:
        from eggpool.cli_full import _restart_after_integration_context_mutation

        ctx = IntegrationContext(
            config_path="/dev/null",
            api_key="k",
            base_url="http://h:1/v1",
            base_url_root="http://h:1",
            host="h",
            port=1,
            config_mutated=True,
        )
        with patch("eggpool.cli_full._restart_after_configsetup_mutation") as mock:
            _restart_after_integration_context_mutation("/dev/null", ctx)
            mock.assert_called_once_with("/dev/null")

    def test_restarts_when_transcoder_mutated(self) -> None:
        from eggpool.cli_full import _restart_after_integration_context_mutation

        ctx = IntegrationContext(
            config_path="/dev/null",
            api_key="k",
            base_url="http://h:1/v1",
            base_url_root="http://h:1",
            host="h",
            port=1,
            transcoder_mutated=True,
        )
        with patch("eggpool.cli_full._restart_after_configsetup_mutation") as mock:
            _restart_after_integration_context_mutation("/dev/null", ctx)
            mock.assert_called_once_with("/dev/null")

    def test_no_restart_when_neither_mutated(self) -> None:
        from eggpool.cli_full import _restart_after_integration_context_mutation

        ctx = IntegrationContext(
            config_path="/dev/null",
            api_key="k",
            base_url="http://h:1/v1",
            base_url_root="http://h:1",
            host="h",
            port=1,
        )
        with patch("eggpool.cli_full._restart_after_configsetup_mutation") as mock:
            _restart_after_integration_context_mutation("/dev/null", ctx)
            mock.assert_not_called()


# ---------------------------------------------------------------------------
# Base URL validation and backup tests
# ---------------------------------------------------------------------------


class TestBaseUrlValidation:
    def test_valid_url_accepted(self, tmp_path: Path) -> None:
        from eggpool.cli_full import _build_ctx_with_overrides

        config_file = _make_config_for_cli(tmp_path)
        ctx = _build_ctx_with_overrides(str(config_file), None, "http://myhost:9999/v1")
        assert ctx.base_url == "http://myhost:9999/v1"
        assert ctx.base_url_root == "http://myhost:9999"

    def test_invalid_url_no_scheme_exits(self, tmp_path: Path) -> None:
        from eggpool.cli_full import _build_ctx_with_overrides

        config_file = _make_config_for_cli(tmp_path)
        with pytest.raises(SystemExit):
            _build_ctx_with_overrides(str(config_file), None, "not-a-url")

    def test_invalid_url_no_netloc_exits(self, tmp_path: Path) -> None:
        from eggpool.cli_full import _build_ctx_with_overrides

        config_file = _make_config_for_cli(tmp_path)
        with pytest.raises(SystemExit):
            _build_ctx_with_overrides(str(config_file), None, "http://")

    def test_trailing_slash_normalized(self, tmp_path: Path) -> None:
        from eggpool.cli_full import _build_ctx_with_overrides

        config_file = _make_config_for_cli(tmp_path)
        ctx = _build_ctx_with_overrides(str(config_file), None, "http://host:11300/v1/")
        assert ctx.base_url == "http://host:11300/v1"
        assert ctx.base_url_root == "http://host:11300"

    def test_host_override(self, tmp_path: Path) -> None:
        from eggpool.cli_full import _build_ctx_with_overrides

        config_file = _make_config_for_cli(tmp_path)
        ctx = _build_ctx_with_overrides(str(config_file), "10.0.0.1", None)
        assert ctx.host == "10.0.0.1"
        assert "10.0.0.1" in ctx.base_url


class TestOutputSnippetBackup:
    def test_backup_created_on_force(self, tmp_path: Path) -> None:
        from eggpool.cli_full import _output_snippet

        target = tmp_path / "config.toml"
        target.write_text("old content\n", encoding="utf-8")
        _output_snippet(
            "new content",
            do_write=True,
            output=str(target),
            force=True,
            no_clipboard=True,
            print_secret=True,
        )
        assert target.read_text(encoding="utf-8") == "new content\n"
        backups = list(tmp_path.glob("config.eggpool.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "old content\n"

    def test_no_backup_when_content_identical(self, tmp_path: Path) -> None:
        from eggpool.cli_full import _output_snippet

        target = tmp_path / "config.toml"
        target.write_text("same content\n", encoding="utf-8")
        _output_snippet(
            "same content",
            do_write=True,
            output=str(target),
            force=True,
            no_clipboard=True,
            print_secret=True,
        )
        backups = list(tmp_path.glob("config.toml.eggpool.bak.*"))
        assert len(backups) == 0

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        from eggpool.cli_full import _output_snippet

        target = tmp_path / "config.toml"
        target.write_text("old\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            _output_snippet(
                "new",
                do_write=True,
                output=str(target),
                force=False,
                no_clipboard=True,
                print_secret=True,
            )


# ---------------------------------------------------------------------------
# Codex TOML parseability tests
# ---------------------------------------------------------------------------


class TestCodexTomlParseability:
    def _ctx_with_model(self, model_id: str) -> IntegrationContext:
        return IntegrationContext(
            config_path="/dev/null",
            api_key="ep_test_key",
            base_url="http://host:11300/v1",
            base_url_root="http://host:11300",
            host="host",
            port=11300,
            models=[
                {
                    "model_id": model_id,
                    "display_name": model_id,
                    "capabilities": {},
                    "source_metadata": {},
                    "effective_limits": {"context_tokens": 128000},
                }
            ],
        )

    def test_parses_with_slash(self) -> None:
        ctx = self._ctx_with_model("gpt-4o/openai")
        snippet = build_codex_toml_snippet(ctx)
        parsed = tomllib.loads(snippet)
        assert "gpt-4o/openai" in parsed["provider"]["eggpool"]["models"]

    def test_parses_with_dot(self) -> None:
        ctx = self._ctx_with_model("gpt-4.1-mini")
        snippet = build_codex_toml_snippet(ctx)
        parsed = tomllib.loads(snippet)
        assert "gpt-4.1-mini" in parsed["provider"]["eggpool"]["models"]

    def test_parses_with_colon(self) -> None:
        ctx = self._ctx_with_model("provider:model")
        snippet = build_codex_toml_snippet(ctx)
        parsed = tomllib.loads(snippet)
        assert "provider:model" in parsed["provider"]["eggpool"]["models"]

    def test_parses_with_space(self) -> None:
        ctx = self._ctx_with_model("my model name")
        snippet = build_codex_toml_snippet(ctx)
        parsed = tomllib.loads(snippet)
        assert "my model name" in parsed["provider"]["eggpool"]["models"]

    def test_parses_bare_model_id(self) -> None:
        ctx = self._ctx_with_model("gpt-4o")
        snippet = build_codex_toml_snippet(ctx)
        parsed = tomllib.loads(snippet)
        assert "gpt-4o" in parsed["provider"]["eggpool"]["models"]


# ---------------------------------------------------------------------------
# Extended CLI tests
# ---------------------------------------------------------------------------

ENV_KEY_CONFIG_MISSING = """\
[server]
api_key_env = "MISSING_ENV_VAR_FOR_TEST"
port = 11300
"""


class TestConfigSetupCLIExtended:
    def test_refuses_absent_api_key_env(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.toml"
        config_file.write_text(ENV_KEY_CONFIG_MISSING, encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "aider",
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code != 0
        assert "api_key_env" in (result.output + (result.stderr or ""))

    def test_base_url_honored(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "aider",
                "--base-url",
                "http://custom:9999/v1",
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "http://custom:9999/v1" in result.output

    def test_base_url_invalid_exits(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "aider",
                "--base-url",
                "not-a-url",
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code != 0

    def test_print_secret_gates_stdout(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        # Without --print-secret, secret should not appear in stdout
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "aider",
                "--no-clipboard",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "ep_test_key_123" not in result.output

    def test_print_secret_shows_stdout(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "aider",
                "--no-clipboard",
                "--print-secret",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "ep_test_key_123" in result.output

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
    def test_no_clipboard_still_produces_output(
        self, tmp_path: Path, subcommand: str
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
        assert result.exit_code == 0, result.output
        assert len(result.output) > 0

    def test_write_mode_creates_file(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        output_file = tmp_path / "output.env"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "aider",
                "--write",
                "--output",
                str(output_file),
                "--print-secret",
            ],
        )
        assert result.exit_code == 0, result.output
        assert output_file.exists()
        content = output_file.read_text(encoding="utf-8")
        assert "ep_test_key_123" in content

    def test_write_mode_force_creates_backup(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        output_file = tmp_path / "output.env"
        output_file.write_text("old content\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "aider",
                "--write",
                "--output",
                str(output_file),
                "--force",
                "--print-secret",
            ],
        )
        assert result.exit_code == 0, result.output
        backups = list(tmp_path.glob("output.eggpool.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "old content\n"

    def test_write_mode_refuses_without_force(self, tmp_path: Path) -> None:
        config_file = _make_config_for_cli(tmp_path)
        output_file = tmp_path / "output.env"
        output_file.write_text("existing\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                "aider",
                "--write",
                "--output",
                str(output_file),
                "--print-secret",
            ],
        )
        assert result.exit_code != 0
        assert "already exists" in (result.output + (result.stderr or ""))

    @pytest.mark.parametrize("subcommand", ["continue", "goose", "openhands"])
    def test_write_mode_model_required_fails_without_model(
        self, tmp_path: Path, subcommand: str
    ) -> None:
        config_file = _make_config_for_cli(tmp_path)
        output_file = tmp_path / "output.tmp"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--config",
                str(config_file),
                "configsetup",
                subcommand,
                "--write",
                "--output",
                str(output_file),
                "--print-secret",
            ],
        )
        assert result.exit_code != 0
        output = result.output + (result.stderr or "")
        assert "--model" in output


# ---------------------------------------------------------------------------
# ConfigsetupTargetSpec tests
# ---------------------------------------------------------------------------


class TestConfigsetupTargetSpec:
    def test_all_targets_have_specs(self) -> None:
        expected = {
            "aider",
            "codex",
            "qwen-code",
            "kilo",
            "continue",
            "cline",
            "roo-code",
            "goose",
            "openhands",
        }
        assert set(TARGET_SPECS.keys()) == expected

    def test_model_required_targets(self) -> None:
        required = {name for name, spec in TARGET_SPECS.items() if spec.requires_model}
        assert required == {"continue", "goose", "openhands"}

    def test_model_optional_targets(self) -> None:
        optional = {
            name for name, spec in TARGET_SPECS.items() if not spec.requires_model
        }
        assert optional == {"aider", "codex", "qwen-code", "kilo", "cline", "roo-code"}

    def test_all_targets_have_mode(self) -> None:
        for name, spec in TARGET_SPECS.items():
            assert spec.mode in ("env", "json", "toml", "yaml", "instructions"), (
                f"{name} has invalid mode: {spec.mode}"
            )

    def test_mode_matches_renderer(self) -> None:
        assert TARGET_SPECS["aider"].mode == "env"
        assert TARGET_SPECS["codex"].mode == "toml"
        assert TARGET_SPECS["qwen-code"].mode == "json"
        assert TARGET_SPECS["kilo"].mode == "json"
        assert TARGET_SPECS["continue"].mode == "yaml"
        assert TARGET_SPECS["cline"].mode == "json"
        assert TARGET_SPECS["roo-code"].mode == "json"
        assert TARGET_SPECS["goose"].mode == "env"
        assert TARGET_SPECS["openhands"].mode == "env"

    def test_spec_fields(self) -> None:
        spec = ConfigsetupTargetSpec(
            name="test",
            requires_model=True,
            mode="yaml",
            supports_dynamic_models=False,
            supports_direct_write=False,
            default_write_path=None,
        )
        assert spec.name == "test"
        assert spec.requires_model is True
        assert spec.mode == "yaml"


# ---------------------------------------------------------------------------
# Model enforcement tests
# ---------------------------------------------------------------------------


class TestModelEnforcement:
    def test_require_model_write_mode_fails_when_ambiguous_model_required(
        self, ctx_many_models: IntegrationContext
    ) -> None:
        import click

        with pytest.raises(click.ClickException, match="--model is required"):
            require_model_for_target("continue", None, ctx_many_models, write_mode=True)

    def test_require_model_write_mode_ok_when_explicit_model(
        self, ctx_many_models: IntegrationContext
    ) -> None:
        result = require_model_for_target(
            "continue", "gpt-4o/openai", ctx_many_models, write_mode=True
        )
        assert result == "gpt-4o/openai"

    def test_require_model_write_mode_ok_when_single_model(
        self, ctx_one_model: IntegrationContext
    ) -> None:
        result = require_model_for_target(
            "continue", None, ctx_one_model, write_mode=True
        )
        assert result == "gpt-4o/openai"

    def test_require_model_write_mode_optional_target_returns_none(
        self, ctx_many_models: IntegrationContext
    ) -> None:
        result = require_model_for_target(
            "aider", None, ctx_many_models, write_mode=True
        )
        assert result is None

    def test_require_model_snippet_mode_returns_none_when_ambiguous(
        self, ctx_many_models: IntegrationContext
    ) -> None:
        result = require_model_for_target(
            "continue", None, ctx_many_models, write_mode=False
        )
        assert result is None

    def test_require_model_empty_catalog_fails_write_mode(
        self, ctx_empty: IntegrationContext
    ) -> None:
        import click

        with pytest.raises(click.ClickException, match="--model is required"):
            require_model_for_target("continue", None, ctx_empty, write_mode=True)
