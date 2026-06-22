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
