"""Tests for coordinator provider-aware changes."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.catalog.protocols import ProtocolMismatchError
from eggpool.errors import ModelUnavailableError, UpstreamError
from eggpool.models.config import AppConfig, ProviderConfig
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
    SelectedAttempt,
)


def _make_context(**overrides: Any) -> ProxyRequestContext:
    defaults = dict(
        request_id="req-1",
        protocol="openai",
        model_id="gpt-4",
        streaming=False,
        original_body=b'{"model":"gpt-4"}',
        incoming_headers={"content-type": "application/json"},
    )
    defaults.update(overrides)
    return ProxyRequestContext(**defaults)


class TestSelectedAttemptProviderId:
    def test_has_provider_id_field(self) -> None:
        attempt = SelectedAttempt(
            proxy_request_id="p-1",
            db_request_id="db-1",
            attempt_id=1,
            reservation_id="r-1",
            account_id=1,
            account_name="test",
            api_key="sk-test",
            model_id="gpt-4",
            estimated_tokens=100,
            estimated_microdollars=50,
            attempt_number=1,
        )
        assert attempt.provider_id == "opencode-go"

    def test_provider_id_is_frozen(self) -> None:
        attempt = SelectedAttempt(
            proxy_request_id="p-1",
            db_request_id="db-1",
            attempt_id=1,
            reservation_id="r-1",
            account_id=1,
            account_name="test",
            api_key="sk-test",
            model_id="gpt-4",
            estimated_tokens=100,
            estimated_microdollars=50,
            attempt_number=1,
        )
        with pytest.raises(FrozenInstanceError):
            attempt.provider_id = "other"  # type: ignore[misc]

    def test_custom_provider_id(self) -> None:
        attempt = SelectedAttempt(
            proxy_request_id="p-1",
            db_request_id="db-1",
            attempt_id=1,
            reservation_id="r-1",
            account_id=1,
            account_name="test",
            api_key="sk-test",
            model_id="gpt-4",
            estimated_tokens=100,
            estimated_microdollars=50,
            attempt_number=1,
            provider_id="custom-provider",
        )
        assert attempt.provider_id == "custom-provider"


class TestGetClientProviderAware:
    def _make_coordinator(
        self,
        *,
        client_pool: ProviderClientPool | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> RequestCoordinator:
        registry = MagicMock()
        catalog = MagicMock()
        router = MagicMock()
        db = MagicMock()
        if client_pool is not None:
            pool_or_client: Any = client_pool
        elif client is not None:
            pool_or_client = client
        else:
            pool_or_client = httpx.AsyncClient()
        coordinator = RequestCoordinator(
            registry=registry,
            catalog=catalog,
            router=router,
            db=db,
            client_pool=pool_or_client,
        )
        return coordinator

    def test_returns_provider_specific_client(self) -> None:
        pool = ProviderClientPool()
        provider_client = httpx.AsyncClient()
        pool.register("custom-provider", provider_client)
        coordinator = self._make_coordinator(client_pool=pool)
        result = coordinator._get_client("custom-provider")
        assert result is provider_client

    def test_falls_back_to_default_when_not_in_pool(self) -> None:
        pool = ProviderClientPool()
        default_client = httpx.AsyncClient()
        pool.register("other-provider", default_client)
        coordinator = self._make_coordinator(client_pool=pool)
        with pytest.raises(UpstreamError):
            coordinator._get_client("unknown-provider")

    def test_missing_provider_never_uses_opencode_default(self) -> None:
        pool = ProviderClientPool()
        pool.register("opencode-go", httpx.AsyncClient())
        coordinator = self._make_coordinator(client_pool=pool)

        with pytest.raises(UpstreamError, match="missing-provider"):
            coordinator._get_client("missing-provider", "selected-account")

    def test_returns_default_when_no_provider_id(self) -> None:
        client = httpx.AsyncClient()
        coordinator = self._make_coordinator(client=client)
        result = coordinator._get_client()
        assert result is client

    def test_raises_when_no_client_available(self) -> None:
        pool = ProviderClientPool()
        coordinator = self._make_coordinator(client_pool=pool)
        with pytest.raises(UpstreamError):
            coordinator._get_client()


class TestGetUpstreamUrlProviderAware:
    def _make_coordinator(self, config: AppConfig | None = None) -> RequestCoordinator:
        registry = MagicMock()
        catalog = MagicMock()
        router = MagicMock()
        db = MagicMock()
        client = httpx.AsyncClient()
        coordinator = RequestCoordinator(
            registry=registry,
            catalog=catalog,
            router=router,
            db=db,
            client_pool=client,
            config=config,
        )
        return coordinator

    def test_uses_provider_config_openai_path(self) -> None:
        config = AppConfig(
            providers={
                "custom": ProviderConfig(
                    id="custom",
                    base_url="https://custom.example.com",
                    openai_path="/v1/chat",
                    anthropic_path="/v1/messages",
                )
            }
        )
        coordinator = self._make_coordinator(config)
        result = coordinator._get_upstream_url("openai", "custom")
        assert result == "https://custom.example.com/v1/chat"

    def test_uses_provider_config_anthropic_path(self) -> None:
        config = AppConfig(
            providers={
                "custom": ProviderConfig(
                    id="custom",
                    base_url="https://custom.example.com",
                    openai_path="/v1/chat",
                    anthropic_path="/v1/messages",
                )
            }
        )
        coordinator = self._make_coordinator(config)
        result = coordinator._get_upstream_url("anthropic", "custom")
        assert result == "https://custom.example.com/v1/messages"

    def test_falls_back_to_defaults_when_no_config(self) -> None:
        coordinator = self._make_coordinator(config=None)
        assert coordinator._get_upstream_url("openai") == "/chat/completions"
        assert coordinator._get_upstream_url("anthropic") == "/messages"

    def test_falls_back_to_defaults_when_unknown_provider(self) -> None:
        config = AppConfig(providers={})
        coordinator = self._make_coordinator(config)
        assert coordinator._get_upstream_url("openai", "unknown") == "/chat/completions"

    def test_falls_back_when_provider_id_none(self) -> None:
        config = AppConfig(
            providers={
                "custom": ProviderConfig(
                    id="custom",
                    base_url="https://custom.example.com",
                    openai_path="/v1/chat",
                )
            }
        )
        coordinator = self._make_coordinator(config)
        assert coordinator._get_upstream_url("openai", None) == "/chat/completions"

    @pytest.mark.parametrize(
        ("base_url", "path", "expected"),
        [
            (
                "https://api.minimax.io/v1",
                "/chat/completions",
                "https://api.minimax.io/v1/chat/completions",
            ),
            (
                "https://api.minimaxi.com/v1",
                "/chat/completions",
                "https://api.minimaxi.com/v1/chat/completions",
            ),
            (
                "https://opencode.ai/zen/go/v1",
                "/chat/completions",
                "https://opencode.ai/zen/go/v1/chat/completions",
            ),
        ],
    )
    def test_chat_dispatch_uses_absolute_url_composition(
        self, base_url: str, path: str, expected: str
    ) -> None:
        cfg = AppConfig(
            providers={"p": ProviderConfig(id="p", base_url=base_url, openai_path=path)}
        )
        coordinator = self._make_coordinator(cfg)
        assert coordinator._get_upstream_url("openai", "p") == expected

    def test_minimax_international_default_dispatch(self) -> None:
        cfg = AppConfig(
            providers={
                "minimax": ProviderConfig(
                    id="minimax",
                    base_url="https://api.minimax.io/v1",
                    protocols=["openai"],
                    openai_path="/chat/completions",
                    models_path="/models",
                )
            }
        )
        coordinator = self._make_coordinator(cfg)
        assert (
            coordinator._get_upstream_url("openai", "minimax")
            == "https://api.minimax.io/v1/chat/completions"
        )

    def test_minimax_china_default_dispatch(self) -> None:
        cfg = AppConfig(
            providers={
                "minimax-cn": ProviderConfig(
                    id="minimax-cn",
                    base_url="https://api.minimaxi.com/v1",
                    protocols=["openai"],
                    openai_path="/chat/completions",
                    models_path="/models",
                )
            }
        )
        coordinator = self._make_coordinator(cfg)
        assert (
            coordinator._get_upstream_url("openai", "minimax-cn")
            == "https://api.minimaxi.com/v1/chat/completions"
        )


class TestCoordinatorInitConfig:
    def test_stores_config(self) -> None:
        registry = MagicMock()
        catalog = MagicMock()
        router = MagicMock()
        db = MagicMock()
        client = httpx.AsyncClient()
        config = AppConfig()
        coordinator = RequestCoordinator(
            registry=registry,
            catalog=catalog,
            router=router,
            db=db,
            client_pool=client,
            config=config,
        )
        assert coordinator._config is config

    def test_default_config_is_none(self) -> None:
        registry = MagicMock()
        catalog = MagicMock()
        router = MagicMock()
        db = MagicMock()
        client = httpx.AsyncClient()
        coordinator = RequestCoordinator(
            registry=registry,
            catalog=catalog,
            router=router,
            db=db,
            client_pool=client,
        )
        assert coordinator._config is None


class TestValidateEndpointProviderAware:
    def _make_coordinator(self, cache: ModelCatalogCache) -> RequestCoordinator:
        catalog = MagicMock()
        catalog.cache = cache
        return RequestCoordinator(
            registry=MagicMock(),
            catalog=catalog,
            router=MagicMock(),
            db=MagicMock(),
            client_pool=httpx.AsyncClient(),
        )

    def test_accepts_unsuffixed_provider_specific_protocol(self) -> None:
        cache = ModelCatalogCache()
        cache.load_model(
            model_id="shared-model",
            display_name="Shared Model",
            protocol="",
            capabilities={},
            source_metadata={},
        )
        cache.set_account_provider("acct1", "provider-a")
        cache.set_provider_model_entry(
            "shared-model",
            "provider-a",
            {
                "model_id": "shared-model",
                "protocol": "openai",
                "capabilities": {},
                "source_metadata": {},
            },
        )
        cache.add_account_support("shared-model", "acct1")
        coordinator = self._make_coordinator(cache)

        coordinator._validate_endpoint(_make_context(model_id="shared-model"))

    def test_rejects_when_only_other_protocol_is_available(self) -> None:
        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct1",
            "provider-a",
            [{"model_id": "shared-model", "protocol": "anthropic"}],
        )
        coordinator = self._make_coordinator(cache)

        with pytest.raises(ProtocolMismatchError):
            coordinator._validate_endpoint(_make_context(model_id="shared-model"))

    def test_rejects_unresolved_provider_specific_protocol(self) -> None:
        cache = ModelCatalogCache()
        cache.update_from_account(
            "acct1",
            "provider-a",
            [{"model_id": "shared-model", "protocol": None}],
        )
        coordinator = self._make_coordinator(cache)

        with pytest.raises(ModelUnavailableError):
            coordinator._validate_endpoint(_make_context(model_id="shared-model"))


class TestDispatchUrlMatchesComposeProviderUrl:
    """Confirm coordinator dispatch URL composition matches the contract
    renderer used by the catalog fetcher.
    """

    def test_get_upstream_url_matches_compose_provider_url(self) -> None:
        from eggpool.providers.contract import compose_provider_url

        cfg = AppConfig(
            providers={
                "minimax": ProviderConfig(
                    id="minimax",
                    base_url="https://api.minimax.io/v1",
                    protocols=["openai"],
                    openai_path="/chat/completions",
                    models_path="/models",
                ),
                "minimax-cn": ProviderConfig(
                    id="minimax-cn",
                    base_url="https://api.minimaxi.com/v1",
                    protocols=["openai"],
                    openai_path="/chat/completions",
                    models_path="/models",
                ),
            }
        )

        registry = MagicMock()
        catalog = MagicMock()
        router = MagicMock()
        db = MagicMock()
        client = httpx.AsyncClient()
        coordinator = RequestCoordinator(
            registry=registry,
            catalog=catalog,
            router=router,
            db=db,
            client_pool=client,
            config=cfg,
        )

        for provider_id, base in (
            ("minimax", "https://api.minimax.io/v1"),
            ("minimax-cn", "https://api.minimaxi.com/v1"),
        ):
            provider_cfg = cfg.providers[provider_id]
            composed = compose_provider_url(provider_cfg, provider_cfg.openai_path)
            assert coordinator._get_upstream_url("openai", provider_id) == composed
            assert composed == f"{base}/chat/completions"
