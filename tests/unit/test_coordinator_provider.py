"""Tests for coordinator provider-aware changes."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from go_aggregator.errors import UpstreamError
from go_aggregator.models.config import AppConfig, ProviderConfig
from go_aggregator.providers.client_pool import ProviderClientPool
from go_aggregator.request.coordinator import (
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
        # The coordinator will have _client=None when opencode-go not in pool
        # so _get_client with unknown provider falls through to _client check
        with pytest.raises(UpstreamError):
            coordinator._get_client("unknown-provider")

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


class TestGetUpstreamPathProviderAware:
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
        result = coordinator._get_upstream_path("openai", "custom")
        assert result == "/v1/chat"

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
        result = coordinator._get_upstream_path("anthropic", "custom")
        assert result == "/v1/messages"

    def test_falls_back_to_defaults_when_no_config(self) -> None:
        coordinator = self._make_coordinator(config=None)
        assert coordinator._get_upstream_path("openai") == "/chat/completions"
        assert coordinator._get_upstream_path("anthropic") == "/messages"

    def test_falls_back_to_defaults_when_unknown_provider(self) -> None:
        config = AppConfig(providers={})
        coordinator = self._make_coordinator(config)
        assert (
            coordinator._get_upstream_path("openai", "unknown") == "/chat/completions"
        )

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
        assert coordinator._get_upstream_path("openai", None) == "/chat/completions"


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
