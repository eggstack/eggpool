"""Tests for the catalog-complete Models page (Phase D).

Phase D of the model-info corrective plan requires the Models page
to list every catalog model, not only those with traffic in the
selected time window.  The handler must:

* Use ``catalog.cache.get_provider_model_entries`` to enumerate
  every ``(model_id, provider_id)`` pair the catalog knows about.
* Build a sparse row (request_count=0, cost=0, etc.) for each pair.
* Merge sparse rows onto ``stats.get_model_stats`` output, with
  active rows winning on numeric columns and sparse rows sorted to
  the bottom of the table.
* Respect the ``account`` filter — only models the account actually
  supports should appear when the filter is set.
* Render an empty page gracefully when the catalog is unavailable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eggpool.catalog.cache import ModelCatalogCache


def _make_cache() -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    for model_id, provider_id in [
        ("gpt-4o", "openai"),
        ("gpt-4o", "azure"),
        ("llama-3-8b", "meta"),
        ("claude-opus-4", "anthropic"),
    ]:
        entry: dict[str, Any] = {
            "model_id": model_id,
            "display_name": model_id.title(),
            "protocol": "openai",
            "capabilities": {"supports_tools": True},
            "source_metadata": {},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {
                "context_tokens": 128000,
                "input_tokens": 128000,
                "output_tokens": 16384,
                "enforce": True,
            },
        }
        cache._provider_models[(model_id, provider_id)] = entry
        if model_id not in cache._models:
            cache._models[model_id] = entry
    # Mark accounts so the supporting-account filter has data.
    cache._account_support["gpt-4o"] = frozenset({"acct-a"})
    cache._account_support["llama-3-8b"] = frozenset({"acct-b"})
    return cache


class _FakeCatalogService:
    """Minimal stand-in for ``CatalogService`` exposing only ``cache``."""

    def __init__(self, cache: ModelCatalogCache) -> None:
        self.cache = cache


class TestGetCatalogRows:
    @pytest.mark.asyncio()
    async def test_one_row_per_provider_pair(self) -> None:
        from eggpool.dashboard.routes import _get_catalog_rows

        catalog = _FakeCatalogService(_make_cache())
        rows = await _get_catalog_rows(catalog)
        # gpt-4o appears on two providers → two rows.
        ids = sorted((r["model_id"], r["provider_id"]) for r in rows)
        assert ids == [
            ("claude-opus-4", "anthropic"),
            ("gpt-4o", "azure"),
            ("gpt-4o", "openai"),
            ("llama-3-8b", "meta"),
        ]

    @pytest.mark.asyncio()
    async def test_sparse_rows_have_zero_activity(self) -> None:
        from eggpool.dashboard.routes import _get_catalog_rows

        catalog = _FakeCatalogService(_make_cache())
        rows = await _get_catalog_rows(catalog)
        for row in rows:
            assert row["request_count"] == 0
            assert row["cost_microdollars"] == 0
            assert row["input_tokens"] == 0
            assert row["output_tokens"] == 0
            assert row["_sparse"] is True

    @pytest.mark.asyncio()
    async def test_account_filter_excludes_unsupported_models(self) -> None:
        from eggpool.dashboard.routes import _get_catalog_rows

        catalog = _FakeCatalogService(_make_cache())
        rows = await _get_catalog_rows(catalog, account="acct-a")
        ids = sorted((r["model_id"], r["provider_id"]) for r in rows)
        # acct-a supports gpt-4o only.
        assert ids == [
            ("gpt-4o", "azure"),
            ("gpt-4o", "openai"),
        ]

    @pytest.mark.asyncio()
    async def test_none_catalog_returns_empty_list(self) -> None:
        from eggpool.dashboard.routes import _get_catalog_rows

        rows = await _get_catalog_rows(None)
        assert rows == []


class TestMergeModelsWithCatalog:
    def test_active_rows_win_on_numeric_columns(self) -> None:
        from eggpool.dashboard.routes import _merge_models_with_catalog

        stats_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 100,
                "cost_microdollars": 5000,
            },
            {
                "model_id": "llama-3-8b",
                "provider_id": "meta",
                "request_count": 7,
                "cost_microdollars": 100,
            },
        ]
        catalog_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 0,
                "cost_microdollars": 0,
                "_sparse": True,
            },
            {
                "model_id": "claude-opus-4",
                "provider_id": "anthropic",
                "request_count": 0,
                "cost_microdollars": 0,
                "_sparse": True,
            },
        ]
        merged = _merge_models_with_catalog(stats_rows, catalog_rows)
        # Active rows sorted by request_count desc; sparse appended.
        assert [r["model_id"] for r in merged] == [
            "gpt-4o",
            "llama-3-8b",
            "claude-opus-4",
        ]
        # Active row keeps its count.
        assert merged[0]["request_count"] == 100
        # Sparse row keeps its _sparse marker and zero count.
        assert merged[2]["_sparse"] is True
        assert merged[2]["request_count"] == 0

    def test_empty_stats_with_catalog_only(self) -> None:
        from eggpool.dashboard.routes import _merge_models_with_catalog

        catalog_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 0,
                "_sparse": True,
            },
            {
                "model_id": "claude-opus-4",
                "provider_id": "anthropic",
                "request_count": 0,
                "_sparse": True,
            },
        ]
        merged = _merge_models_with_catalog([], catalog_rows)
        assert len(merged) == 2
        assert all(r["_sparse"] for r in merged)

    def test_dedup_when_stats_and_catalog_agree_on_key(self) -> None:
        from eggpool.dashboard.routes import _merge_models_with_catalog

        stats_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 5,
            },
        ]
        catalog_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 0,
                "_sparse": True,
            },
        ]
        merged = _merge_models_with_catalog(stats_rows, catalog_rows)
        assert len(merged) == 1
        assert merged[0]["request_count"] == 5
        # Stats row's _sparse marker was popped during merge.
        assert "_sparse" not in merged[0]


class TestHandleModelsRendersCatalogComplete:
    """End-to-end smoke: the handler merges stats + catalog and
    delegates to ``render_models``."""

    @pytest.mark.asyncio()
    async def test_handle_models_returns_html_with_catalog_rows(self) -> None:
        from fastapi import FastAPI
        from starlette.requests import Request

        from eggpool.dashboard.routes import handle_models

        app = FastAPI()
        # Stats stub — one active model, no traffic for the catalog-only one.
        active_row = {
            "model_id": "gpt-4o",
            "provider_id": "openai",
            "request_count": 10,
            "cost_microdollars": 100,
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "tokens_per_second": 0.0,
            "avg_latency_ms": 250.0,
            "avg_ttft_ms": 100.0,
            "error_count": 0,
            "exact_count": 5,
            "derived_count": 5,
            "partial_count": 0,
            "estimated_count": 0,
            "unknown_count": 0,
            "provider_reported_count": 10,
            "estimated_cost_fraction": 0.1,
            "cache_read_ratio": 0.0,
            "cache_write_ratio": 0.0,
            "reasoning_output_ratio": 0.0,
            "avg_cost_per_request": 10,
            "avg_cost_per_1k_tokens": 6,
        }

        class _StubStats:
            async def get_model_stats(
                self,
                _range: Any,
                *,
                account_name: str | None = None,
                use_cache: bool = True,
            ) -> list[dict[str, Any]]:
                return [active_row]

        catalog = _FakeCatalogService(_make_cache())

        class _StubDashboardConfig:
            enabled = True
            themes_dir = ""
            theme = "default"

        class _StubConfig:
            dashboard = _StubDashboardConfig()

        app.state.stats = _StubStats()
        app.state.config = _StubConfig()
        app.state.model_info = None
        app.state.catalog = catalog
        app.state.stats_db = None
        app.state.dashboard_db = None

        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "path": "/models",
            "headers": [],
            "query_string": b"period=24h",
            "app": app,
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive)
        response = await handle_models(request)
        assert response.status_code == 200
        body = response.body.decode("utf-8")
        # Active row appears.
        assert "gpt-4o" in body
        # Catalog-only rows appear even with no traffic.
        assert "claude-opus-4" in body
        assert "llama-3-8b" in body
