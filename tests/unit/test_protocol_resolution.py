"""Section 11: Per-model protocol resolution."""

from __future__ import annotations

import pytest

from go_aggregator.catalog.protocols import (
    ModelProtocolResolver,
    ProtocolMismatchError,
)
from go_aggregator.models.config import AppConfig


@pytest.fixture()
def resolver_no_config() -> ModelProtocolResolver:
    return ModelProtocolResolver()


@pytest.fixture()
def resolver_with_config() -> ModelProtocolResolver:
    config = AppConfig.from_dict(
        {
            "accounts": [],
            "model_overrides": {
                "claude-3-5-sonnet-20241022": {"protocol": "openai"},
            },
        }
    )
    return ModelProtocolResolver(config=config)


class TestExactMapping:
    def test_gpt4_maps_to_openai(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        result = resolver_no_config.resolve_from_catalog("gpt-4")
        assert result.protocol == "openai"
        assert result.source == "exact_mapping"

    def test_claude3_opus_maps_to_anthropic(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        result = resolver_no_config.resolve_from_catalog("claude-3-opus-20240229")
        assert result.protocol == "anthropic"
        assert result.source == "exact_mapping"

    def test_unknown_model_falls_through(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        result = resolver_no_config.resolve_from_catalog("unknown-model-xyz")
        assert result.source == "unresolved"


class TestFamilyMapping:
    def test_gpt_family(self, resolver_no_config: ModelProtocolResolver) -> None:
        result = resolver_no_config.resolve_from_catalog("gpt-4-turbo-2024")
        assert result.protocol == "openai"
        assert result.source == "family_mapping"

    def test_claude_family(self, resolver_no_config: ModelProtocolResolver) -> None:
        result = resolver_no_config.resolve_from_catalog("claude-4-opus")
        assert result.protocol == "anthropic"
        assert result.source == "family_mapping"

    def test_o1_family(self, resolver_no_config: ModelProtocolResolver) -> None:
        result = resolver_no_config.resolve_from_catalog("o1-preview")
        assert result.protocol == "openai"
        assert result.source == "family_mapping"


class TestConfigOverride:
    def test_config_override_wins(
        self, resolver_with_config: ModelProtocolResolver
    ) -> None:
        result = resolver_with_config.resolve_from_catalog("claude-3-5-sonnet-20241022")
        assert result.protocol == "openai"
        assert result.source == "config_override"

    def test_config_override_via_metadata(
        self, resolver_with_config: ModelProtocolResolver
    ) -> None:
        result = resolver_with_config.resolve_from_metadata(
            "claude-3-5-sonnet-20241022", {}
        )
        assert result.protocol == "openai"
        assert result.source == "config_override"


class TestUpstreamMetadata:
    def test_api_type_openai(self, resolver_no_config: ModelProtocolResolver) -> None:
        result = resolver_no_config.resolve_from_metadata(
            "some-model", {"api_type": "openai"}
        )
        assert result.protocol == "openai"
        assert result.source == "upstream_metadata"

    def test_api_type_anthropic(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        result = resolver_no_config.resolve_from_metadata(
            "some-model", {"api_type": "anthropic"}
        )
        assert result.protocol == "anthropic"
        assert result.source == "upstream_metadata"

    def test_protocol_field(self, resolver_no_config: ModelProtocolResolver) -> None:
        result = resolver_no_config.resolve_from_metadata(
            "some-model", {"protocol": "anthropic"}
        )
        assert result.protocol == "anthropic"
        assert result.source == "upstream_metadata"


class TestPersistedProtocol:
    def test_persisted_openai(self, resolver_no_config: ModelProtocolResolver) -> None:
        result = resolver_no_config.resolve_from_catalog(
            "unknown-model", persisted_protocol="openai"
        )
        assert result.protocol == "openai"
        assert result.source == "persisted"

    def test_persisted_anthropic(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        result = resolver_no_config.resolve_from_catalog(
            "unknown-model", persisted_protocol="anthropic"
        )
        assert result.protocol == "anthropic"
        assert result.source == "persisted"


class TestEndpointValidation:
    def test_correct_endpoint_passes(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        # Should not raise
        resolver_no_config.validate_endpoint("openai", "openai", "gpt-4")

    def test_wrong_endpoint_raises(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        with pytest.raises(ProtocolMismatchError) as exc_info:
            resolver_no_config.validate_endpoint("anthropic", "openai", "claude-3")
        assert "Anthropic" in str(exc_info.value)
        assert "/v1/messages" in str(exc_info.value)

    def test_anthropic_on_chat_completions(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        with pytest.raises(ProtocolMismatchError) as exc_info:
            resolver_no_config.validate_endpoint("anthropic", "openai", "claude-3")
        assert "Anthropic" in str(exc_info.value)

    def test_openai_on_messages(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        with pytest.raises(ProtocolMismatchError) as exc_info:
            resolver_no_config.validate_endpoint("openai", "anthropic", "gpt-4")
        assert "OpenAI" in str(exc_info.value)
        assert "/v1/chat/completions" in str(exc_info.value)


class TestResolutionOrder:
    def test_exact_mapping_over_family(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        """Exact mapping should take precedence over family mapping."""
        result = resolver_no_config.resolve_from_catalog("gpt-4")
        assert result.source == "exact_mapping"

    def test_family_mapping_for_non_exact(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        """Family mapping used when no exact match exists."""
        result = resolver_no_config.resolve_from_catalog("gpt-4-turbo-2024-0409")
        assert result.source == "family_mapping"

    def test_persisted_only_for_unknown(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        """Persisted used only when no other source matches."""
        result = resolver_no_config.resolve_from_catalog(
            "custom-model-xyz", persisted_protocol="openai"
        )
        assert result.source == "persisted"

    def test_unresolved_for_unknown_no_persisted(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        result = resolver_no_config.resolve_from_catalog("custom-model-xyz")
        assert result.source == "unresolved"


class TestMixedCatalog:
    def test_both_protocols_from_same_catalog(
        self, resolver_no_config: ModelProtocolResolver
    ) -> None:
        """One catalog can produce both protocol families."""
        openai_result = resolver_no_config.resolve_from_catalog("gpt-4o")
        anthropic_result = resolver_no_config.resolve_from_catalog(
            "claude-3-5-sonnet-20241022"
        )
        assert openai_result.protocol == "openai"
        assert anthropic_result.protocol == "anthropic"
