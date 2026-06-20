"""Tests for CatalogService ping recording during catalog refresh."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from eggpool.catalog.service import CatalogService
from eggpool.models.config import AppConfig, ProviderConfig


def _make_config() -> MagicMock:
    config = MagicMock()
    config.providers = {}
    config.model_overrides = {}
    config.models = MagicMock()
    config.models.expose_mode = "union"
    return config


def _make_registry(enabled_accounts: list[str] | None = None) -> MagicMock:
    registry = MagicMock()
    if enabled_accounts is None:
        enabled_accounts = ["acct1"]
    states = []
    for name in enabled_accounts:
        state = MagicMock()
        state.name = name
        state.enabled = True
        states.append(state)
    registry.get_enabled_states.return_value = states
    registry.get_api_key.return_value = "test-key"
    registry.get_provider_for_account.return_value = "opencode-go"
    return registry


class TestCatalogServicePingRecording:
    """Verify CatalogService records pings via PingRepository."""

    @pytest.mark.asyncio
    async def test_record_ping_on_success(self) -> None:
        """Ping is recorded with correct fields on successful fetch."""
        mock_ping_repo = AsyncMock()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [{"id": "gpt-4"}]}
        mock_response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=MagicMock(),
            client_pool=mock_client,
            ping_repo=mock_ping_repo,
        )

        await service._fetch_and_process_account(
            "acct1", "test-key", "opencode-go", mock_client
        )

        mock_ping_repo.record_ping.assert_awaited_once()
        call_kwargs = mock_ping_repo.record_ping.call_args[1]
        assert call_kwargs["provider_id"] == "opencode-go"
        assert call_kwargs["account_name"] == "acct1"
        assert call_kwargs["status_code"] == 200
        assert call_kwargs["error"] is None
        assert call_kwargs["model_count"] == 1
        assert isinstance(call_kwargs["latency_ms"], int)
        assert call_kwargs["latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_record_ping_on_http_error(self) -> None:
        """Ping is recorded even when HTTP request fails."""
        mock_ping_repo = AsyncMock()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = "Forbidden"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=mock_response
        )
        mock_client.get = AsyncMock(return_value=mock_response)

        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=MagicMock(),
            client_pool=mock_client,
            ping_repo=mock_ping_repo,
        )

        await service._fetch_and_process_account(
            "acct1", "test-key", "opencode-go", mock_client
        )

        mock_ping_repo.record_ping.assert_awaited_once()
        call_kwargs = mock_ping_repo.record_ping.call_args[1]
        assert call_kwargs["status_code"] == 403
        assert call_kwargs["error"] == "HTTP 403"
        assert call_kwargs["model_count"] == 0

    @pytest.mark.asyncio
    async def test_record_ping_on_connection_error(self) -> None:
        """Ping is recorded when connection fails."""
        mock_ping_repo = AsyncMock()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=MagicMock(),
            client_pool=mock_client,
            ping_repo=mock_ping_repo,
        )

        await service._fetch_and_process_account(
            "acct1", "test-key", "opencode-go", mock_client
        )

        mock_ping_repo.record_ping.assert_awaited_once()
        call_kwargs = mock_ping_repo.record_ping.call_args[1]
        assert call_kwargs["status_code"] is None
        assert call_kwargs["error"] is not None
        assert call_kwargs["model_count"] == 0

    @pytest.mark.asyncio
    async def test_no_ping_when_repo_is_none(self) -> None:
        """No ping recorded when ping_repo is None."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [{"id": "gpt-4"}]}
        mock_response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=MagicMock(),
            client_pool=mock_client,
            ping_repo=None,
        )

        # Should not raise - just skip ping recording
        await service._fetch_and_process_account(
            "acct1", "test-key", "opencode-go", mock_client
        )
        # No assertion needed - just verify no error

    @pytest.mark.asyncio
    async def test_model_count_reflects_actual_models(self) -> None:
        """model_count in ping matches number of models returned."""
        mock_ping_repo = AsyncMock()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [{"id": "gpt-4"}, {"id": "claude-3"}, {"id": "gemini-pro"}]
        }
        mock_response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        service = CatalogService(
            config=_make_config(),
            registry=_make_registry(),
            db=MagicMock(),
            client_pool=mock_client,
            ping_repo=mock_ping_repo,
        )

        await service._fetch_and_process_account(
            "acct1", "test-key", "opencode-go", mock_client
        )

        call_kwargs = mock_ping_repo.record_ping.call_args[1]
        assert call_kwargs["model_count"] == 3

    @pytest.mark.asyncio
    async def test_unresolved_model_does_not_inherit_another_provider_protocol(
        self,
    ) -> None:
        """Provider-local fallback must not borrow shared-model metadata."""
        config = AppConfig(
            providers={
                provider_id: ProviderConfig(
                    id=provider_id,
                    base_url=f"https://{provider_id}.example",
                    protocols=["openai", "anthropic"],
                )
                for provider_id in ("provider-a", "provider-b")
            }
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [{"id": "shared-model"}]}
        mock_response.raise_for_status = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        service = CatalogService(
            config=config,
            registry=_make_registry(),
            db=MagicMock(),
            client_pool=mock_client,
        )
        service.cache.update_from_account(
            "acct-a",
            "provider-a",
            [{"model_id": "shared-model", "protocol": "openai"}],
        )

        await service._fetch_and_process_account(
            "acct-b", "test-key", "provider-b", mock_client
        )

        provider_b_model = service.cache.get_provider_model_entry(
            "shared-model", "provider-b"
        )
        assert provider_b_model is not None
        assert provider_b_model["protocol"] is None
