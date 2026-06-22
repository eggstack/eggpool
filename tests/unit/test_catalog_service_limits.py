"""Tests for catalog service limit resolution during refresh and hydration."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from eggpool.catalog.fetcher import FetchResult
from eggpool.catalog.limits import ModelLimitResolver
from eggpool.catalog.service import CatalogService
from eggpool.models.config import AppConfig, ProviderConfig


def _make_config(
    provider_overrides: dict[str, dict[str, Any]] | None = None,
    global_overrides: dict[str, dict[str, Any]] | None = None,
) -> MagicMock:
    config = MagicMock(spec=AppConfig)
    providers: dict[str, MagicMock] = {}
    if provider_overrides:
        for pid, overrides in provider_overrides.items():
            prov = MagicMock(spec=ProviderConfig)
            prov.protocols = ["openai"]
            prov.models_method = "GET"
            prov.models_path = "/models"
            model_overrides: dict[str, MagicMock] = {}
            for mid, ovr in overrides.items():
                mo = MagicMock()
                mo.max_context_tokens = ovr.get("max_context_tokens")
                mo.max_input_tokens = ovr.get("max_input_tokens")
                mo.max_output_tokens = ovr.get("max_output_tokens")
                mo.enforce_context_limit = ovr.get("enforce_context_limit", True)
                mo.protocol = ovr.get("protocol")
                mo.input_price_per_1k = ovr.get("input_price_per_1k")
                mo.output_price_per_1k = ovr.get("output_price_per_1k")
                mo.cache_read_per_million_microdollars = ovr.get(
                    "cache_read_per_million_microdollars"
                )
                mo.cache_write_per_million_microdollars = ovr.get(
                    "cache_write_per_million_microdollars"
                )
                model_overrides[mid] = mo
            prov.model_overrides = model_overrides
            providers[pid] = prov
    config.providers = providers

    mo_global: dict[str, MagicMock] = {}
    if global_overrides:
        for mid, ovr in global_overrides.items():
            mo = MagicMock()
            mo.max_context_tokens = ovr.get("max_context_tokens")
            mo.max_input_tokens = ovr.get("max_input_tokens")
            mo.max_output_tokens = ovr.get("max_output_tokens")
            mo.enforce_context_limit = ovr.get("enforce_context_limit", True)
            mo.protocol = ovr.get("protocol")
            mo.input_price_per_1k = ovr.get("input_price_per_1k")
            mo.output_price_per_1k = ovr.get("output_price_per_1k")
            mo.cache_read_per_million_microdollars = ovr.get(
                "cache_read_per_million_microdollars"
            )
            mo.cache_write_per_million_microdollars = ovr.get(
                "cache_write_per_million_microdollars"
            )
            mo_global[mid] = mo
    config.model_overrides = mo_global

    config.models = MagicMock()
    config.models.expose_mode = "union"
    return config


def _make_service(
    config: MagicMock | None = None,
) -> tuple[CatalogService, AsyncMock]:
    if config is None:
        config = _make_config()
    mock_db = AsyncMock()
    mock_db.fetch_all = AsyncMock(return_value=[])
    mock_client = MagicMock(spec=httpx.AsyncClient)
    service = CatalogService(
        config=config,
        registry=MagicMock(),
        db=mock_db,
        client_pool=mock_client,
    )
    return service, mock_db


def _upstream_response(*models: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(models)}


@pytest.mark.asyncio
async def test_upstream_metadata_extracted_into_discovered_limits() -> None:
    """discovered_limits is populated from response metadata."""
    upstream = _upstream_response(
        {
            "id": "m1",
            "name": "Model One",
            "context_window": 200000,
            "max_output_tokens": 8192,
        }
    )
    fetch_result = FetchResult(
        response=upstream, latency_ms=10, status_code=200, error=None, model_count=1
    )

    service, _ = _make_service()

    with patch(
        "eggpool.catalog.service.fetch_models_for_account",
        new_callable=AsyncMock,
        return_value=fetch_result,
    ):
        await service._fetch_and_process_account(
            "acct1", "key1", "p1", MagicMock(spec=httpx.AsyncClient)
        )

    cached = service.cache.get_model("m1")
    assert cached is not None
    disc = cached["discovered_limits"]
    assert disc["context_tokens"] == 200000
    assert disc["output_tokens"] == 8192
    assert disc["input_tokens"] is None


@pytest.mark.asyncio
async def test_provider_override_applied_during_refresh() -> None:
    """Provider-specific config overrides produce expected effective_limits."""
    config = _make_config(
        provider_overrides={
            "p1": {
                "m1": {
                    "max_context_tokens": 100000,
                    "max_output_tokens": 4096,
                }
            }
        }
    )
    upstream = _upstream_response(
        {
            "id": "m1",
            "name": "Model One",
            "context_window": 200000,
            "max_output_tokens": 8192,
        }
    )
    fetch_result = FetchResult(
        response=upstream, latency_ms=10, status_code=200, error=None, model_count=1
    )

    service, _ = _make_service(config)

    with patch(
        "eggpool.catalog.service.fetch_models_for_account",
        new_callable=AsyncMock,
        return_value=fetch_result,
    ):
        await service._fetch_and_process_account(
            "acct1", "key1", "p1", MagicMock(spec=httpx.AsyncClient)
        )

    cached = service.cache.get_model("m1")
    assert cached is not None
    eff = cached["effective_limits"]
    assert eff["context_tokens"] == 100000
    assert eff["context_source"] == "provider_override"
    assert eff["output_tokens"] == 4096
    assert eff["output_source"] == "provider_override"


@pytest.mark.asyncio
async def test_provider_protocol_override_applied_during_refresh() -> None:
    config = AppConfig.from_dict(
        {
            "providers": {
                "p1": {
                    "id": "p1",
                    "base_url": "https://provider.example.com",
                    "protocols": ["anthropic"],
                    "model_overrides": {"m1": {"protocol": "anthropic"}},
                }
            }
        }
    )
    fetch_result = FetchResult(
        response=_upstream_response({"id": "m1", "name": "Model One"}),
        latency_ms=10,
        status_code=200,
        error=None,
        model_count=1,
    )
    service, _ = _make_service(config)

    with patch(
        "eggpool.catalog.service.fetch_models_for_account",
        new_callable=AsyncMock,
        return_value=fetch_result,
    ):
        await service._fetch_and_process_account(
            "acct1", "key1", "p1", MagicMock(spec=httpx.AsyncClient)
        )

    cached = service.cache.get_provider_model_entry("m1", "p1")
    assert cached is not None
    assert cached["protocol"] == "anthropic"
    assert cached["protocol_source"] == "config"


@pytest.mark.asyncio
async def test_global_protocol_fills_missing_provider_protocol_field() -> None:
    config = AppConfig.from_dict(
        {
            "model_overrides": {"m1": {"protocol": "anthropic"}},
            "providers": {
                "p1": {
                    "id": "p1",
                    "base_url": "https://provider.example.com",
                    "protocols": ["anthropic"],
                    "model_overrides": {"m1": {"max_context_tokens": 100_000}},
                }
            },
        }
    )
    fetch_result = FetchResult(
        response=_upstream_response({"id": "m1"}),
        latency_ms=10,
        status_code=200,
        error=None,
        model_count=1,
    )
    service, _ = _make_service(config)

    with patch(
        "eggpool.catalog.service.fetch_models_for_account",
        new_callable=AsyncMock,
        return_value=fetch_result,
    ):
        await service._fetch_and_process_account(
            "acct1", "key1", "p1", MagicMock(spec=httpx.AsyncClient)
        )

    cached = service.cache.get_provider_model_entry("m1", "p1")
    assert cached is not None
    assert cached["protocol"] == "anthropic"


@pytest.mark.asyncio
async def test_provider_pricing_override_takes_per_field_precedence() -> None:
    config = AppConfig.from_dict(
        {
            "model_overrides": {
                "m1": {
                    "input_price_per_1k": 0.001,
                    "output_price_per_1k": 0.002,
                    "cache_read_per_million_microdollars": 300_000,
                }
            },
            "providers": {
                "p1": {
                    "id": "p1",
                    "base_url": "https://provider.example.com",
                    "model_overrides": {
                        "m1": {
                            "output_price_per_1k": 0.009,
                            "cache_write_per_million_microdollars": 900_000,
                        }
                    },
                }
            },
        }
    )
    service, mock_db = _make_service(config)

    await service._maybe_insert_price_snapshot(
        "m1", {"source_metadata": {}}, provider_id="p1"
    )

    values = mock_db.execute_write.await_args.args[1]
    assert values[0:3] == ("m1", 0.001, 0.009)
    assert values[5:9] == (300_000, 900_000, "config", "p1")


@pytest.mark.asyncio
async def test_source_metadata_not_mutated_by_resolution() -> None:
    """source_metadata is not mutated by limit resolution."""
    raw_item = {
        "id": "m1",
        "name": "Model One",
        "context_window": 128000,
        "custom_key": "value",
    }
    upstream = _upstream_response(raw_item)
    fetch_result = FetchResult(
        response=upstream, latency_ms=10, status_code=200, error=None, model_count=1
    )

    service, _ = _make_service()

    with patch(
        "eggpool.catalog.service.fetch_models_for_account",
        new_callable=AsyncMock,
        return_value=fetch_result,
    ):
        await service._fetch_and_process_account(
            "acct1", "key1", "p1", MagicMock(spec=httpx.AsyncClient)
        )

    cached = service.cache.get_model("m1")
    assert cached is not None
    meta = cached["source_metadata"]
    assert meta["custom_key"] == "value"
    assert meta["context_window"] == 128000
    assert meta == {"context_window": 128000, "custom_key": "value"}


@pytest.mark.asyncio
async def test_persisted_catalog_hydration_reapplies_configuration() -> None:
    """Load from mock DB; _load_cached_models produces correct effective limits."""
    config = _make_config(
        provider_overrides={
            "p1": {
                "m1": {
                    "max_context_tokens": 150000,
                }
            }
        }
    )
    service, mock_db = _make_service(config)

    model_row = {
        "model_id": "m1",
        "display_name": "Model One",
        "protocol": "openai",
        "capabilities": json.dumps({"context_window": 200000}),
        "source_metadata": json.dumps({"context_window": 200000}),
        "first_seen_at": "2025-01-01 00:00:00",
        "last_seen_at": "2025-01-01 00:00:00",
        "protocol_source": "config",
    }
    acct_row = {"id": 1, "name": "acct1", "provider_id": "p1"}
    am_row = {"account_id": 1, "model_id": "m1"}

    mock_db.fetch_all = AsyncMock(side_effect=[[model_row], [], [am_row], [acct_row]])

    await service._load_cached_models()

    provider_entries = service.cache.get_provider_model_entries()
    entry = provider_entries.get(("m1", "p1"))
    assert entry is not None
    eff = entry["effective_limits"]
    assert eff["context_tokens"] == 150000
    assert eff["context_source"] == "provider_override"


@pytest.mark.asyncio
async def test_changed_limit_reflected_after_reload() -> None:
    """Changing config and re-calling _load_cached_models reflects the new limit."""
    config_v1 = _make_config(
        provider_overrides={
            "p1": {
                "m1": {
                    "max_context_tokens": 100000,
                }
            }
        }
    )
    service, mock_db = _make_service(config_v1)

    model_row = {
        "model_id": "m1",
        "display_name": "Model One",
        "protocol": "openai",
        "capabilities": json.dumps({"context_window": 200000}),
        "source_metadata": json.dumps({"context_window": 200000}),
        "first_seen_at": "2025-01-01 00:00:00",
        "last_seen_at": "2025-01-01 00:00:00",
        "protocol_source": "config",
    }
    acct_row = {"id": 1, "name": "acct1", "provider_id": "p1"}
    am_row = {"account_id": 1, "model_id": "m1"}

    mock_db.fetch_all = AsyncMock(side_effect=[[model_row], [], [am_row], [acct_row]])
    await service._load_cached_models()

    entry_v1 = service.cache.get_provider_model_entries().get(("m1", "p1"))
    assert entry_v1 is not None
    assert entry_v1["effective_limits"]["context_tokens"] == 100000

    config_v2 = _make_config(
        provider_overrides={
            "p1": {
                "m1": {
                    "max_context_tokens": 200000,
                }
            }
        }
    )
    service._config = config_v2
    service._limit_resolver = ModelLimitResolver(config_v2)
    service._cache = type(service._cache)()

    mock_db.fetch_all = AsyncMock(side_effect=[[model_row], [], [am_row], [acct_row]])
    await service._load_cached_models()

    entry_v2 = service.cache.get_provider_model_entries().get(("m1", "p1"))
    assert entry_v2 is not None
    assert entry_v2["effective_limits"]["context_tokens"] == 200000
    assert entry_v2["effective_limits"]["context_source"] == "provider_override"
