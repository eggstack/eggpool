"""Tests for static provider model seed merging.

Static model seeds (``[[providers.<id>.static_models]]``) populate the
catalog cache for providers whose upstream does not advertise a usable
``/models`` listing. Live refreshes may augment static rows but must
not erase explicit static protocol or capability fields.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.catalog.service import CatalogService
from eggpool.errors import ConfigError
from eggpool.models.config import (
    AppConfig,
    ProviderStaticModelConfig,
)


def _make_service(config: AppConfig) -> CatalogService:
    return CatalogService(
        config=config,
        registry=MagicMock(),
        db=MagicMock(),
        client_pool=MagicMock(),
    )


# ---------------------------------------------------------------------------
# ProviderStaticModelConfig validation
# ---------------------------------------------------------------------------


class TestProviderStaticModelConfig:
    def test_validates_with_required_fields_only(self) -> None:
        static = ProviderStaticModelConfig(id="m1")
        assert static.id == "m1"
        assert static.display_name is None
        assert static.protocol is None
        assert static.max_context_tokens is None
        assert static.max_input_tokens is None
        assert static.max_output_tokens is None
        assert static.supports_tools is None
        assert static.supports_vision is None
        assert static.source_metadata == {}

    def test_validates_with_all_fields(self) -> None:
        static = ProviderStaticModelConfig(
            id="m1",
            display_name="M1",
            protocol="anthropic",
            max_context_tokens=100000,
            max_input_tokens=80000,
            max_output_tokens=16000,
            supports_tools=True,
            supports_vision=False,
            source_metadata={"family": "minimax"},
        )
        assert static.display_name == "M1"
        assert static.protocol == "anthropic"
        assert static.max_context_tokens == 100000
        assert static.supports_tools is True
        assert static.source_metadata == {"family": "minimax"}

    def test_rejects_unknown_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            ProviderStaticModelConfig(id="m1", unknown_field="x")

    def test_rejects_zero_or_negative_limit(self) -> None:
        with pytest.raises(ValidationError):
            ProviderStaticModelConfig(id="m1", max_context_tokens=0)
        with pytest.raises(ValidationError):
            ProviderStaticModelConfig(id="m1", max_context_tokens=-1)


# ---------------------------------------------------------------------------
# ProviderConfig rejects duplicate static model IDs
# ---------------------------------------------------------------------------


class TestProviderConfigDuplicateStaticModels:
    def test_rejects_duplicate_static_model_ids(self) -> None:
        with pytest.raises(ConfigError, match="duplicate static model id"):
            AppConfig.model_validate(
                {
                    "providers": {
                        "p1": {
                            "id": "p1",
                            "base_url": "https://p1.example",
                            "protocols": ["openai"],
                            "accounts": [{"name": "a1", "api_key": "sk-a"}],
                            "static_models": [
                                {"id": "m1"},
                                {"id": "m1"},
                            ],
                        }
                    }
                }
            )

    def test_allows_unique_static_model_ids(self) -> None:
        config = AppConfig.model_validate(
            {
                "providers": {
                    "p1": {
                        "id": "p1",
                        "base_url": "https://p1.example",
                        "protocols": ["openai"],
                        "accounts": [{"name": "a1", "api_key": "sk-a"}],
                        "static_models": [
                            {"id": "m1"},
                            {"id": "m2"},
                        ],
                    }
                }
            }
        )
        assert len(config.providers["p1"].static_models) == 2


# ---------------------------------------------------------------------------
# _build_static_models output shape
# ---------------------------------------------------------------------------


def _static_provider_config() -> AppConfig:
    return AppConfig.model_validate(
        {
            "providers": {
                "p1": {
                    "id": "p1",
                    "base_url": "https://p1.example",
                    "protocols": ["anthropic"],
                    "accounts": [{"name": "a1", "api_key": "sk-a"}],
                    "static_models": [
                        {
                            "id": "m1",
                            "display_name": "M1",
                            "protocol": "anthropic",
                            "max_context_tokens": 100000,
                            "max_input_tokens": 80000,
                            "max_output_tokens": 16000,
                            "supports_tools": True,
                            "supports_vision": False,
                            "source_metadata": {"family": "minimax"},
                        },
                        {
                            "id": "m2",
                        },
                    ],
                }
            }
        }
    )


class TestBuildStaticModels:
    def test_produces_expected_normalized_shape(self) -> None:
        config = _static_provider_config()
        provider_cfg = config.providers["p1"]
        service = _make_service(config)

        models = service._build_static_models(provider_cfg, "a1")

        assert len(models) == 2
        m1, m2 = models

        assert m1["model_id"] == "m1"
        assert m1["display_name"] == "M1"
        assert m1["protocol"] == "anthropic"
        assert m1["protocol_source"] == "static_config"
        assert m1["capabilities"] == {
            "supports_tools": True,
            "supports_vision": False,
            "max_context_tokens": 100000,
            "max_input_tokens": 80000,
            "max_output_tokens": 16000,
        }
        assert m1["source_metadata"] == {
            "family": "minimax",
            "source": "static_config",
        }
        assert m1["discovered_limits"] == {
            "context_tokens": 100000,
            "input_tokens": 80000,
            "output_tokens": 16000,
        }
        assert m1["effective_limits"]["context_tokens"] == 100000
        assert m1["effective_limits"]["input_tokens"] == 80000
        assert m1["effective_limits"]["output_tokens"] == 16000
        assert m1["effective_limits"]["context_source"] == "upstream_metadata"

    def test_minimal_entry_uses_id_as_display_name(self) -> None:
        config = _static_provider_config()
        provider_cfg = config.providers["p1"]
        service = _make_service(config)

        models = service._build_static_models(provider_cfg, "a1")
        m2 = next(model for model in models if model["model_id"] == "m2")
        assert m2["display_name"] == "m2"
        assert m2["protocol"] is None
        assert m2["protocol_source"] is None
        assert m2["capabilities"] == {}
        assert m2["source_metadata"] == {"source": "static_config"}
        assert m2["discovered_limits"] == {
            "context_tokens": None,
            "input_tokens": None,
            "output_tokens": None,
        }

    def test_no_static_models_returns_empty_list(self) -> None:
        config = AppConfig.model_validate(
            {
                "providers": {
                    "p1": {
                        "id": "p1",
                        "base_url": "https://p1.example",
                        "accounts": [{"name": "a1", "api_key": "sk-a"}],
                    }
                }
            }
        )
        service = _make_service(config)
        assert service._build_static_models(config.providers["p1"], "a1") == []

    def test_global_model_override_takes_precedence(self) -> None:
        config = AppConfig.model_validate(
            {
                "model_overrides": {
                    "m1": {
                        "max_context_tokens": 500000,
                        "max_output_tokens": 32000,
                    }
                },
                "providers": {
                    "p1": {
                        "id": "p1",
                        "base_url": "https://p1.example",
                        "protocols": ["openai"],
                        "accounts": [{"name": "a1", "api_key": "sk-a"}],
                        "static_models": [
                            {
                                "id": "m1",
                                "protocol": "openai",
                                "max_context_tokens": 100000,
                            }
                        ],
                    }
                },
            }
        )
        service = _make_service(config)
        models = service._build_static_models(config.providers["p1"], "a1")
        assert models[0]["effective_limits"]["context_tokens"] == 500000
        assert models[0]["effective_limits"]["output_tokens"] == 32000
        assert models[0]["effective_limits"]["context_source"] == "global_override"


# ---------------------------------------------------------------------------
# _seed_static_models populates cache (covers DISABLED scenarios)
# ---------------------------------------------------------------------------


class TestSeedStaticModels:
    def test_populates_cache_for_disabled_models_endpoint(self) -> None:
        config = AppConfig.model_validate(
            {
                "providers": {
                    "p1": {
                        "id": "p1",
                        "base_url": "https://p1.example",
                        "protocols": ["anthropic"],
                        "models_endpoint": {"method": "DISABLED", "required": False},
                        "accounts": [{"name": "a1", "api_key": "sk-a"}],
                        "static_models": [
                            {
                                "id": "m1",
                                "display_name": "M1",
                                "protocol": "anthropic",
                                "max_context_tokens": 204800,
                                "max_output_tokens": 32000,
                                "supports_tools": True,
                                "supports_vision": False,
                            }
                        ],
                    }
                }
            }
        )
        provider_cfg = config.providers["p1"]
        cache = ModelCatalogCache()
        service = _make_service(config)
        service._cache = cache

        asyncio.run(service._seed_static_models("a1", "p1", provider_cfg))

        assert cache.get_supporting_accounts("m1") == {"a1"}
        entry = cache.get_provider_model_entry("m1", "p1")
        assert entry is not None
        assert entry["protocol"] == "anthropic"
        assert entry["protocol_source"] == "static_config"
        assert entry["capabilities"]["supports_tools"] is True
        assert entry["capabilities"]["supports_vision"] is False
        assert entry["discovered_limits"]["context_tokens"] == 204800
        assert entry["discovered_limits"]["output_tokens"] == 32000
        assert entry["effective_limits"]["context_tokens"] == 204800
        assert cache.get_provider_for_account("a1") == "p1"

    def test_no_op_when_provider_has_no_static_models(self) -> None:
        config = AppConfig.model_validate(
            {
                "providers": {
                    "p1": {
                        "id": "p1",
                        "base_url": "https://p1.example",
                        "accounts": [{"name": "a1", "api_key": "sk-a"}],
                    }
                }
            }
        )
        provider_cfg = config.providers["p1"]
        cache = ModelCatalogCache()
        service = _make_service(config)
        service._cache = cache

        asyncio.run(service._seed_static_models("a1", "p1", provider_cfg))

        assert cache.model_count == 0
        assert cache.get_provider_model_entry("a1", "p1") is None

    def test_minimax_cn_static_seeds_pin_openai_protocol(self) -> None:
        """MiniMax China seeds pin protocol = openai so the family-mapping
        anthropic fallback cannot clear the protocol at provider constraint
        check time.
        """
        config = AppConfig.model_validate(
            {
                "providers": {
                    "minimax-cn": {
                        "id": "minimax-cn",
                        "base_url": "https://api.minimaxi.com/v1",
                        "protocols": ["openai"],
                        "accounts": [{"name": "cn-1", "api_key": "sk-cn"}],
                        "static_models": [
                            {"id": "MiniMax-M3", "protocol": "openai"},
                            {"id": "MiniMax-M2.7", "protocol": "openai"},
                            {"id": "MiniMax-M2.5", "protocol": "openai"},
                        ],
                    }
                }
            }
        )
        provider_cfg = config.providers["minimax-cn"]
        cache = ModelCatalogCache()
        service = _make_service(config)
        service._cache = cache

        asyncio.run(service._seed_static_models("cn-1", "minimax-cn", provider_cfg))

        for model_id in ("MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"):
            entry = cache.get_provider_model_entry(model_id, "minimax-cn")
            assert entry is not None, f"missing static seed for {model_id}"
            assert entry["protocol"] == "openai"
            assert entry["protocol_source"] == "static_config"

    def test_static_seed_protocol_survives_family_mapping_live_merge(self) -> None:
        """Live fetch from /v1/models resolves MiniMax-M* to anthropic via the
        family map; the static_config pin must survive via
        ``_preserve_static_fields``.
        """
        cache = ModelCatalogCache()
        cache.update_from_account(
            "cn-1",
            "minimax-cn",
            [
                {
                    "model_id": "MiniMax-M3",
                    "display_name": "MiniMax-M3",
                    "protocol": "openai",
                    "protocol_source": "static_config",
                    "capabilities": {"supports_tools": True},
                }
            ],
        )
        cache.update_from_account(
            "cn-1",
            "minimax-cn",
            [
                {
                    "model_id": "MiniMax-M3",
                    "display_name": "MiniMax-M3",
                    "protocol": "anthropic",
                    "protocol_source": "family_mapping",
                    "capabilities": {},
                }
            ],
        )

        entry = cache.get_provider_model_entry("MiniMax-M3", "minimax-cn")
        assert entry is not None
        assert entry["protocol"] == "openai"
        assert entry["protocol_source"] == "static_config"


# ---------------------------------------------------------------------------
# Live merge preserves explicit static fields
# ---------------------------------------------------------------------------


class TestStaticLiveMerge:
    def test_static_protocol_preserved_when_live_has_no_protocol(self) -> None:
        cache = ModelCatalogCache()
        cache.update_from_account(
            "a1",
            "p1",
            [
                {
                    "model_id": "m1",
                    "display_name": "M1",
                    "protocol": "anthropic",
                    "protocol_source": "static_config",
                    "capabilities": {
                        "supports_tools": True,
                        "supports_vision": False,
                    },
                }
            ],
        )
        cache.update_from_account(
            "a1",
            "p1",
            [
                {
                    "model_id": "m1",
                    "display_name": "M1",
                    "protocol": None,
                    "protocol_source": "unresolved",
                    "capabilities": {},
                }
            ],
        )

        entry = cache.get_provider_model_entry("m1", "p1")
        assert entry is not None
        assert entry["protocol"] == "anthropic"
        assert entry["protocol_source"] == "static_config"
        assert entry["capabilities"]["supports_tools"] is True
        assert entry["capabilities"]["supports_vision"] is False

    def test_config_override_wins_over_static_protocol(self) -> None:
        cache = ModelCatalogCache()
        cache.update_from_account(
            "a1",
            "p1",
            [
                {
                    "model_id": "m1",
                    "protocol": "anthropic",
                    "protocol_source": "static_config",
                }
            ],
        )
        cache.update_from_account(
            "a1",
            "p1",
            [
                {
                    "model_id": "m1",
                    "protocol": "openai",
                    "protocol_source": "config",
                }
            ],
        )

        entry = cache.get_provider_model_entry("m1", "p1")
        assert entry is not None
        assert entry["protocol"] == "openai"
        assert entry["protocol_source"] == "config"

    def test_static_capability_preserved_when_live_has_none(self) -> None:
        cache = ModelCatalogCache()
        cache.update_from_account(
            "a1",
            "p1",
            [
                {
                    "model_id": "m1",
                    "protocol": "openai",
                    "protocol_source": "static_config",
                    "capabilities": {"supports_tools": True},
                }
            ],
        )
        cache.update_from_account(
            "a1",
            "p1",
            [
                {
                    "model_id": "m1",
                    "protocol": "openai",
                    "protocol_source": "upstream_metadata",
                    "capabilities": {"supports_vision": True},
                }
            ],
        )

        entry = cache.get_provider_model_entry("m1", "p1")
        assert entry is not None
        assert entry["capabilities"]["supports_tools"] is True
        assert entry["capabilities"]["supports_vision"] is True

    def test_no_merge_when_no_static_entry_exists(self) -> None:
        cache = ModelCatalogCache()
        cache.update_from_account(
            "a1",
            "p1",
            [
                {
                    "model_id": "m1",
                    "protocol": "openai",
                    "protocol_source": "upstream_metadata",
                    "capabilities": {"supports_tools": True},
                }
            ],
        )

        entry = cache.get_provider_model_entry("m1", "p1")
        assert entry is not None
        assert entry["protocol"] == "openai"
        assert entry["protocol_source"] == "upstream_metadata"
        assert entry["capabilities"]["supports_tools"] is True
