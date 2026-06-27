"""Tests for get_transcodable_protocols on ModelCatalogCache."""

from __future__ import annotations

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.models.config import AppConfig


def _build_cache_with_two_providers() -> tuple[ModelCatalogCache, AppConfig]:
    """Build a cache with two providers serving one model under different protocols.

    - provider "openai-prov" → protocols: ["openai"]  → serves "gpt-4"
    - provider "anthropic-prov" → protocols: ["anthropic"] → serves "gpt-4"
    """
    config = AppConfig.model_validate(
        {
            "providers": {
                "openai-prov": {
                    "id": "openai-prov",
                    "base_url": "https://api.openai.example/v1",
                    "protocols": ["openai"],
                    "accounts": [{"name": "acct_oa", "api_key": "sk-test"}],
                },
                "anthropic-prov": {
                    "id": "anthropic-prov",
                    "base_url": "https://api.anthropic.example/v1",
                    "protocols": ["anthropic"],
                    "accounts": [{"name": "acct_an", "api_key": "sk-test"}],
                },
            }
        }
    )
    cache = ModelCatalogCache()
    cache.set_config(config)
    cache.update_from_account(
        "acct_oa", "openai-prov", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    cache.update_from_account(
        "acct_an",
        "anthropic-prov",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )
    return cache, config


class TestGetTranscodableProtocols:
    """Tests for ModelCatalogCache.get_transcodable_protocols."""

    def test_returns_other_protocols(self) -> None:
        cache, _config = _build_cache_with_two_providers()
        result = cache.get_transcodable_protocols("gpt-4", client_protocol="openai")
        assert result == {"anthropic"}

    def test_client_protocol_excluded(self) -> None:
        cache, _config = _build_cache_with_two_providers()
        result = cache.get_transcodable_protocols("gpt-4", client_protocol="anthropic")
        assert result == {"openai"}

    def test_unknown_model_returns_empty(self) -> None:
        cache, _config = _build_cache_with_two_providers()
        result = cache.get_transcodable_protocols(
            "nonexistent", client_protocol="openai"
        )
        assert result == set()

    def test_no_config_returns_empty(self) -> None:
        cache = ModelCatalogCache()
        # No set_config called — _config is None
        cache.update_from_account(
            "acct_oa", "openai-prov", [{"model_id": "gpt-4", "protocol": "openai"}]
        )
        result = cache.get_transcodable_protocols("gpt-4", client_protocol="openai")
        assert result == set()

    def test_single_provider_returns_empty(self) -> None:
        """When only one provider serves the model, client_protocol discards it."""
        config = AppConfig.model_validate(
            {
                "providers": {
                    "openai-prov": {
                        "id": "openai-prov",
                        "base_url": "https://api.openai.example/v1",
                        "protocols": ["openai"],
                        "accounts": [{"name": "acct_oa", "api_key": "sk-test"}],
                    },
                }
            }
        )
        cache = ModelCatalogCache()
        cache.set_config(config)
        cache.update_from_account(
            "acct_oa", "openai-prov", [{"model_id": "gpt-4", "protocol": "openai"}]
        )
        result = cache.get_transcodable_protocols("gpt-4", client_protocol="openai")
        assert result == set()

    def test_multiple_protocols_on_single_provider(self) -> None:
        """A provider with both protocols still yields the other one."""
        config = AppConfig.model_validate(
            {
                "providers": {
                    "both-proto": {
                        "id": "both-proto",
                        "base_url": "https://api.both.example/v1",
                        "protocols": ["openai", "anthropic"],
                        "accounts": [{"name": "acct_both", "api_key": "sk-test"}],
                    },
                }
            }
        )
        cache = ModelCatalogCache()
        cache.set_config(config)
        cache.update_from_account(
            "acct_both",
            "both-proto",
            [{"model_id": "gpt-4", "protocol": "openai"}],
        )
        result = cache.get_transcodable_protocols("gpt-4", client_protocol="openai")
        assert result == {"anthropic"}
