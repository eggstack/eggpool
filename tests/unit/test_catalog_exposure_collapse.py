"""Tests for the collapse_models flag and the dispatch exposure path."""

from __future__ import annotations

from unittest.mock import MagicMock

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.service import CatalogService
from eggpool.models.config import AppConfig


def _build_registry(accounts: list[tuple[str, str]]) -> AccountRegistry:
    """Build a registry from ``[(account_name, provider_id), ...]``."""
    config = AppConfig.model_validate(
        {
            "providers": {
                pid: {
                    "id": pid,
                    "base_url": f"https://{pid}.example",
                    "protocols": ["openai"],
                    "accounts": [{"name": acct, "api_key": "sk-test"}],
                }
                for acct, pid in accounts
            }
        }
    )
    return AccountRegistry(config)


class TestCatalogServiceExposureCollapse:
    """Verify ``get_models_for_exposure`` honors ``collapse_models``."""

    def test_default_returns_provider_suffixed_entries(self) -> None:
        config = AppConfig.model_validate(
            {
                "models": {"collapse_models": False},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode-go.example",
                        "protocols": ["openai"],
                        "accounts": [{"name": "a1", "api_key": "sk-a"}],
                    },
                    "minimax": {
                        "id": "minimax",
                        "base_url": "https://minimax.example",
                        "protocols": ["openai"],
                        "accounts": [{"name": "b1", "api_key": "sk-b"}],
                    },
                },
            }
        )
        registry = AccountRegistry(config)
        catalog = CatalogService(
            config=config,
            registry=registry,
            db=MagicMock(),
            client_pool=MagicMock(),
        )
        catalog.cache.update_from_account(
            "a1", "opencode-go", [{"model_id": "shared", "protocol": "openai"}]
        )
        catalog.cache.update_from_account(
            "b1", "minimax", [{"model_id": "shared", "protocol": "openai"}]
        )

        models = catalog.get_models_for_exposure()
        ids = {m["model_id"] for m in models}
        assert ids == {"shared/opencode-go", "shared/minimax"}

    def test_collapse_models_returns_unsuffixed_entry(self) -> None:
        config = AppConfig.model_validate(
            {
                "models": {"collapse_models": True},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode-go.example",
                        "protocols": ["openai"],
                        "accounts": [{"name": "a1", "api_key": "sk-a"}],
                    },
                    "minimax": {
                        "id": "minimax",
                        "base_url": "https://minimax.example",
                        "protocols": ["openai"],
                        "accounts": [{"name": "b1", "api_key": "sk-b"}],
                    },
                },
            }
        )
        registry = AccountRegistry(config)
        catalog = CatalogService(
            config=config,
            registry=registry,
            db=MagicMock(),
            client_pool=MagicMock(),
        )
        catalog.cache.update_from_account(
            "a1", "opencode-go", [{"model_id": "shared", "protocol": "openai"}]
        )
        catalog.cache.update_from_account(
            "b1", "minimax", [{"model_id": "shared", "protocol": "openai"}]
        )

        models = catalog.get_models_for_exposure()
        ids = {m["model_id"] for m in models}
        # Collapsed: a single unsuffixed entry per base model.
        assert ids == {"shared"}

    def test_dispatch_path_always_suffixed(self) -> None:
        """``get_models_for_dispatch`` ignores ``collapse_models`` and
        always returns provider-suffixed entries."""
        config = AppConfig.model_validate(
            {
                "models": {"collapse_models": True},
                "providers": {
                    "opencode-go": {
                        "id": "opencode-go",
                        "base_url": "https://opencode-go.example",
                        "protocols": ["openai"],
                        "accounts": [{"name": "a1", "api_key": "sk-a"}],
                    },
                    "minimax": {
                        "id": "minimax",
                        "base_url": "https://minimax.example",
                        "protocols": ["openai"],
                        "accounts": [{"name": "b1", "api_key": "sk-b"}],
                    },
                },
            }
        )
        registry = AccountRegistry(config)
        catalog = CatalogService(
            config=config,
            registry=registry,
            db=MagicMock(),
            client_pool=MagicMock(),
        )
        catalog.cache.update_from_account(
            "a1", "opencode-go", [{"model_id": "shared", "protocol": "openai"}]
        )
        catalog.cache.update_from_account(
            "b1", "minimax", [{"model_id": "shared", "protocol": "openai"}]
        )

        models = catalog.get_models_for_dispatch()
        ids = {m["model_id"] for m in models}
        assert ids == {"shared/opencode-go", "shared/minimax"}


class TestLocalProviderOnboarding:
    """Tests for local provider onboarding edge cases."""

    def test_empty_model_list_preserves_prior_support(self) -> None:
        """An empty model list from a local provider must not produce a
        false provider failure.  Prior model support must be preserved
        so the provider remains visible in the catalog."""
        config = AppConfig.model_validate(
            {
                "models": {"collapse_models": True},
                "providers": {
                    "ollama-local": {
                        "id": "ollama-local",
                        "base_url": "http://localhost:11434/v1",
                        "protocols": ["openai"],
                        "auth": {"mode": "none"},
                        "accounts": [{"name": "a1"}],
                    },
                },
            }
        )
        registry = AccountRegistry(config)
        catalog = CatalogService(
            config=config,
            registry=registry,
            db=MagicMock(),
            client_pool=MagicMock(),
        )
        # Simulate a successful initial model discovery.
        catalog.cache.update_from_account(
            "a1",
            "ollama-local",
            [{"model_id": "llama3.2", "protocol": "openai"}],
        )
        assert catalog.cache.model_count == 1

        # Simulate an empty refresh (e.g. all models unloaded).
        result = catalog.cache.update_from_account("a1", "ollama-local", [])

        # Non-destructive: prior support is preserved.
        assert result.preserved_support == 1
        assert result.withdrawn_support == 0
        assert catalog.cache.get_supporting_accounts("llama3.2") == {"a1"}
        # The model is still visible in the catalog.
        models = catalog.get_models_for_exposure()
        assert len(models) == 1
        assert models[0]["model_id"] == "llama3.2"

    def test_two_local_instances_same_model_collapse(self) -> None:
        """Two local provider instances exposing the same model must
        participate in normal collapsed-model selection."""
        config = AppConfig.model_validate(
            {
                "models": {"collapse_models": True},
                "providers": {
                    "ollama-mac": {
                        "id": "ollama-mac",
                        "base_url": "http://macbook.local:11434/v1",
                        "protocols": ["openai"],
                        "auth": {"mode": "none"},
                        "accounts": [{"name": "a1"}],
                    },
                    "ollama-rpi5": {
                        "id": "ollama-rpi5",
                        "base_url": "http://rpi5.local:11434/v1",
                        "protocols": ["openai"],
                        "auth": {"mode": "none"},
                        "accounts": [{"name": "a2"}],
                    },
                },
            }
        )
        registry = AccountRegistry(config)
        catalog = CatalogService(
            config=config,
            registry=registry,
            db=MagicMock(),
            client_pool=MagicMock(),
        )
        catalog.cache.update_from_account(
            "a1",
            "ollama-mac",
            [{"model_id": "llama3.2", "protocol": "openai"}],
        )
        catalog.cache.update_from_account(
            "a2",
            "ollama-rpi5",
            [{"model_id": "llama3.2", "protocol": "openai"}],
        )

        models = catalog.get_models_for_exposure()
        assert len(models) == 1
        assert models[0]["model_id"] == "llama3.2"
        assert set(models[0]["providers"]) == {"ollama-mac", "ollama-rpi5"}

        # Dispatch path always returns suffixed entries.
        dispatch = catalog.get_models_for_dispatch()
        ids = {m["model_id"] for m in dispatch}
        assert ids == {"llama3.2/ollama-mac", "llama3.2/ollama-rpi5"}
