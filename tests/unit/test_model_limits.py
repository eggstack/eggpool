"""Tests for model limit configuration and resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from eggpool.errors import ConfigError
from eggpool.models.config import (
    AppConfig,
    ModelLimitOverrideConfig,
    ModelOverrideConfig,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# ModelLimitOverrideConfig parsing
# ---------------------------------------------------------------------------


class TestModelLimitOverrideConfig:
    def test_defaults(self) -> None:
        cfg = ModelLimitOverrideConfig()
        assert cfg.max_context_tokens is None
        assert cfg.max_input_tokens is None
        assert cfg.max_output_tokens is None
        assert cfg.enforce_context_limit is True

    def test_all_fields(self) -> None:
        cfg = ModelLimitOverrideConfig(
            max_context_tokens=200000,
            max_input_tokens=180000,
            max_output_tokens=16384,
            enforce_context_limit=False,
        )
        assert cfg.max_context_tokens == 200000
        assert cfg.max_input_tokens == 180000
        assert cfg.max_output_tokens == 16384
        assert cfg.enforce_context_limit is False

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError, match="greater than"):
            ModelLimitOverrideConfig(max_context_tokens=0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="greater than"):
            ModelLimitOverrideConfig(max_output_tokens=-100)

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            ModelLimitOverrideConfig(max_context_tokens=1000, bogus=True)  # type: ignore[call-arg]

    def test_output_exceeding_context_is_rejected_at_base_type(self) -> None:
        with pytest.raises(ConfigError, match="max_output_tokens.*exceeds"):
            ModelLimitOverrideConfig(
                max_context_tokens=1000,
                max_output_tokens=1001,
            )


# ---------------------------------------------------------------------------
# ModelOverrideConfig cross-field validation
# ---------------------------------------------------------------------------


class TestModelOverrideCrossField:
    def test_output_exceeds_context_rejected(self) -> None:
        with pytest.raises(ConfigError, match="max_output_tokens.*exceeds"):
            ModelOverrideConfig(
                max_context_tokens=100000,
                max_output_tokens=200000,
            )

    def test_input_exceeds_context_rejected(self) -> None:
        with pytest.raises(ConfigError, match="max_input_tokens.*exceeds"):
            ModelOverrideConfig(
                max_context_tokens=100000,
                max_input_tokens=200000,
            )

    def test_valid_within_context(self) -> None:
        cfg = ModelOverrideConfig(
            max_context_tokens=200000,
            max_input_tokens=180000,
            max_output_tokens=16384,
        )
        assert cfg.max_context_tokens == 200000

    def test_no_context_allows_any_input_output(self) -> None:
        cfg = ModelOverrideConfig(
            max_input_tokens=180000,
            max_output_tokens=16384,
        )
        assert cfg.max_input_tokens == 180000


# ---------------------------------------------------------------------------
# Provider-scoped model overrides
# ---------------------------------------------------------------------------


class TestProviderModelOverrides:
    def test_provider_override_parses(self, tmp_path: Path) -> None:
        config_file = tmp_path / "prov_override.toml"
        config_file.write_text(
            """
[providers.opencode-go]
id = "opencode-go"
base_url = "https://example.com/v1"

[providers.opencode-go.model_overrides."MiniMax-M3"]
max_context_tokens = 220000
max_output_tokens = 16384
enforce_context_limit = true
"""
        )
        config = AppConfig.from_toml(str(config_file))
        prov = config.providers["opencode-go"]
        override = prov.model_overrides["MiniMax-M3"]
        assert override.max_context_tokens == 220000
        assert override.max_output_tokens == 16384
        assert override.enforce_context_limit is True

    def test_provider_override_output_exceeds_context_rejected(
        self, tmp_path: Path
    ) -> None:
        config_file = tmp_path / "bad_prov.toml"
        config_file.write_text(
            """
[providers.p1]
id = "p1"
base_url = "https://example.com"

[providers.p1.model_overrides."m1"]
max_context_tokens = 100000
max_output_tokens = 200000
"""
        )
        with pytest.raises(ConfigError, match="max_output_tokens.*exceeds"):
            AppConfig.from_toml(str(config_file))

    def test_global_override_parses(self, tmp_path: Path) -> None:
        config_file = tmp_path / "global_override.toml"
        config_file.write_text(
            """
[model_overrides."gpt-4"]
max_context_tokens = 128000
max_input_tokens = 100000
max_output_tokens = 4096
"""
        )
        config = AppConfig.from_toml(str(config_file))
        override = config.model_overrides["gpt-4"]
        assert override.max_context_tokens == 128000
        assert override.max_input_tokens == 100000
        assert override.max_output_tokens == 4096

    def test_global_override_output_exceeds_context_rejected(
        self, tmp_path: Path
    ) -> None:
        config_file = tmp_path / "bad_global.toml"
        config_file.write_text(
            """
[model_overrides."m1"]
max_context_tokens = 100000
max_output_tokens = 200000
"""
        )
        with pytest.raises(ConfigError, match="max_output_tokens.*exceeds"):
            AppConfig.from_toml(str(config_file))

    def test_legacy_config_without_limits_valid(self, tmp_path: Path) -> None:
        config_file = tmp_path / "legacy.toml"
        config_file.write_text(
            """
[model_overrides."gpt-4"]
protocol = "openai"
input_price_per_1k = "$3 / 1M"
"""
        )
        config = AppConfig.from_toml(str(config_file))
        override = config.model_overrides["gpt-4"]
        assert override.protocol == "openai"
        assert override.max_context_tokens is None

    def test_model_ids_with_dots_and_slashes(self, tmp_path: Path) -> None:
        config_file = tmp_path / "dots.toml"
        config_file.write_text(
            """
[providers.p1]
id = "p1"
base_url = "https://example.com"

[providers.p1.model_overrides."openai/gpt-4.1"]
max_context_tokens = 1000000
"""
        )
        config = AppConfig.from_toml(str(config_file))
        override = config.providers["p1"].model_overrides["openai/gpt-4.1"]
        assert override.max_context_tokens == 1000000


# ---------------------------------------------------------------------------
# AppConfig provider override cross-field validation
# ---------------------------------------------------------------------------


class TestAppConfigProviderOverrideValidation:
    def test_provider_input_exceeds_context_rejected(self, tmp_path: Path) -> None:
        config_file = tmp_path / "prov_bad_input.toml"
        config_file.write_text(
            """
[providers.p1]
id = "p1"
base_url = "https://example.com"

[providers.p1.model_overrides."m1"]
max_context_tokens = 100000
max_input_tokens = 200000
"""
        )
        with pytest.raises(ConfigError, match="max_input_tokens.*exceeds"):
            AppConfig.from_toml(str(config_file))

    def test_multiple_providers_independent(self, tmp_path: Path) -> None:
        config_file = tmp_path / "multi_prov.toml"
        config_file.write_text(
            """
[providers.p1]
id = "p1"
base_url = "https://p1.example.com"

[providers.p1.model_overrides."m1"]
max_context_tokens = 100000

[providers.p2]
id = "p2"
base_url = "https://p2.example.com"

[providers.p2.model_overrides."m1"]
max_context_tokens = 500000
"""
        )
        config = AppConfig.from_toml(str(config_file))
        assert config.providers["p1"].model_overrides["m1"].max_context_tokens == 100000
        assert config.providers["p2"].model_overrides["m1"].max_context_tokens == 500000


# ---------------------------------------------------------------------------
# Cross-layer merge: provider override for one field + global for another
# ---------------------------------------------------------------------------


class TestCrossLayerMerge:
    def test_provider_context_global_output_merge(self, tmp_path: Path) -> None:
        """Provider sets context, global sets output; effective is the merge."""
        config_file = tmp_path / "cross_layer.toml"
        config_file.write_text(
            """
[model_overrides."m1"]
max_output_tokens = 16384

[providers.p1]
id = "p1"
base_url = "https://example.com"

[providers.p1.model_overrides."m1"]
max_context_tokens = 220000
"""
        )
        config = AppConfig.from_toml(str(config_file))
        provider_override = config.providers["p1"].model_overrides["m1"]
        global_override = config.model_overrides["m1"]
        assert provider_override.max_context_tokens == 220000
        assert provider_override.max_output_tokens is None
        assert global_override.max_output_tokens == 16384
        assert global_override.max_context_tokens is None


class TestMixedCaseModelIds:
    def test_mixed_case_model_id_parsed(self, tmp_path: Path) -> None:
        """Mixed-case model IDs in TOML quoted keys are handled."""
        config_file = tmp_path / "mixed_case.toml"
        config_file.write_text(
            """
[providers.p1]
id = "p1"
base_url = "https://example.com"

[providers.p1.model_overrides."MiniMax-M3"]
max_context_tokens = 220000
max_output_tokens = 16384
"""
        )
        config = AppConfig.from_toml(str(config_file))
        override = config.providers["p1"].model_overrides["MiniMax-M3"]
        assert override.max_context_tokens == 220000
        assert override.max_output_tokens == 16384

    def test_mixed_case_global_and_provider(self, tmp_path: Path) -> None:
        """Mixed-case model IDs work in both global and provider sections."""
        config_file = tmp_path / "mixed_case2.toml"
        config_file.write_text(
            """
[model_overrides."Claude-3-Opus"]
max_output_tokens = 4096

[providers.p1]
id = "p1"
base_url = "https://example.com"

[providers.p1.model_overrides."Claude-3-Opus"]
max_context_tokens = 200000
"""
        )
        config = AppConfig.from_toml(str(config_file))
        assert config.model_overrides["Claude-3-Opus"].max_output_tokens == 4096
        assert (
            config.providers["p1"].model_overrides["Claude-3-Opus"].max_context_tokens
            == 200000
        )
