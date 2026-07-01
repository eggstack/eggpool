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
from typing import Any, cast

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

    def get_models_for_exposure(
        self, health_manager: Any = None
    ) -> list[dict[str, Any]]:
        """Compute the collapsed exposure view from the cache.

        Groups ``_provider_models`` entries by ``model_id``, attaches
        a sorted ``providers`` list, and surfaces ``protocol`` /
        ``display_name`` from the per-provider entry.  Matches the
        shape ``catalog.service.CatalogService.get_models_for_exposure``
        returns in collapse mode so the dashboard's collapsed-row
        builder can be exercised in unit tests.
        """
        collapsed: dict[str, dict[str, Any]] = {}
        for (model_id, provider_id), entry in self.cache._provider_models.items():
            bucket = collapsed.setdefault(
                model_id,
                {
                    "model_id": model_id,
                    "providers": [],
                    "protocol": None,
                    "display_name": None,
                },
            )
            providers_list = bucket["providers"]
            assert isinstance(providers_list, list)
            providers_list.append(provider_id)
            entry_dict = cast("dict[str, Any] | None", entry)
            if not isinstance(entry_dict, dict):
                continue
            if entry_dict.get("protocol") and not bucket["protocol"]:
                bucket["protocol"] = entry_dict["protocol"]
            display_name = entry_dict.get("display_name")
            if display_name and not bucket["display_name"]:
                bucket["display_name"] = display_name
        for bucket in collapsed.values():
            providers_list = bucket["providers"]
            assert isinstance(providers_list, list)
            providers_list.sort()
        return sorted(collapsed.values(), key=lambda m: m["model_id"])


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


class TestApplyModelFilters:
    def test_used_used_keeps_only_active_rows(self) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        rows = [
            {"model_id": "gpt-4o", "request_count": 10},
            {"model_id": "llama-3-8b", "request_count": 0},
        ]
        result = _apply_model_filters(rows, used="used")
        assert [r["model_id"] for r in result] == ["gpt-4o"]

    def test_used_unused_keeps_only_zero_request_rows(self) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        rows = [
            {"model_id": "gpt-4o", "request_count": 10},
            {"model_id": "llama-3-8b", "request_count": 0},
        ]
        result = _apply_model_filters(rows, used="unused")
        assert [r["model_id"] for r in result] == ["llama-3-8b"]

    def test_used_all_keeps_everything(self) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        rows = [
            {"model_id": "gpt-4o", "request_count": 10},
            {"model_id": "llama-3-8b", "request_count": 0},
        ]
        result = _apply_model_filters(rows, used="all")
        assert len(result) == 2

    def test_info_status_filters_by_model_info_status(self) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        rows = [
            {"model_id": "gpt-4o", "request_count": 10},
            {"model_id": "llama-3-8b", "request_count": 5},
            {"model_id": "claude-opus-4", "request_count": 0},
        ]
        mi_map = {
            "gpt-4o": {"status": "fresh"},
            "llama-3-8b": {"status": "sparse_new"},
            "claude-opus-4": {"status": "unmatched"},
        }
        result = _apply_model_filters(rows, info_status="fresh", model_info_map=mi_map)
        assert [r["model_id"] for r in result] == ["gpt-4o"]

    def test_availability_available_keeps_catalog_models(self) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        rows = [
            {"model_id": "gpt-4o", "request_count": 10, "_in_catalog": True},
            {"model_id": "llama-3-8b", "request_count": 0, "_in_catalog": True},
            {
                "model_id": "mystery-model",
                "request_count": 3,
            },
        ]
        result = _apply_model_filters(rows, availability="available")
        assert [r["model_id"] for r in result] == [
            "gpt-4o",
            "llama-3-8b",
        ]

    def test_availability_unavailable_keeps_non_catalog_models(
        self,
    ) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        rows = [
            {"model_id": "gpt-4o", "request_count": 10, "_in_catalog": True},
            {
                "model_id": "mystery-model",
                "request_count": 3,
            },
        ]
        result = _apply_model_filters(rows, availability="unavailable")
        assert [r["model_id"] for r in result] == ["mystery-model"]

    def test_combined_filters_narrow_results(self) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        rows = [
            {"model_id": "gpt-4o", "request_count": 10, "_in_catalog": True},
            {"model_id": "llama-3-8b", "request_count": 0, "_in_catalog": True},
            {"model_id": "claude-opus-4", "request_count": 0},
        ]
        mi_map = {
            "gpt-4o": {"status": "fresh"},
            "llama-3-8b": {"status": "sparse_new"},
            "claude-opus-4": {"status": "unmatched"},
        }
        # used=unused + info_status=sparse_new
        result = _apply_model_filters(
            rows,
            used="unused",
            info_status="sparse_new",
            model_info_map=mi_map,
        )
        assert [r["model_id"] for r in result] == ["llama-3-8b"]

    def test_no_filters_returns_all_rows(self) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        rows = [
            {"model_id": "gpt-4o", "request_count": 10},
            {"model_id": "llama-3-8b", "request_count": 0},
        ]
        result = _apply_model_filters(rows)
        assert len(result) == 2

    def test_empty_rows_returns_empty_list(self) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        result = _apply_model_filters([], used="used")
        assert result == []


class TestModelsPageFilterIntegration:
    """End-to-end tests for the Models page filter parameters."""

    @pytest.mark.asyncio()
    async def test_used_filter_excludes_zero_usage_models(self) -> None:
        from fastapi import FastAPI
        from starlette.requests import Request

        from eggpool.dashboard.routes import handle_models

        app = FastAPI()
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
            "query_string": b"period=24h&used=used",
            "app": app,
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive)
        response = await handle_models(request, used="used")
        assert response.status_code == 200
        body = response.body.decode("utf-8")
        # Active row with traffic appears.
        assert "gpt-4o" in body
        # Zero-usage catalog-only models are filtered out.
        assert "claude-opus-4" not in body
        assert "llama-3-8b" not in body

    @pytest.mark.asyncio()
    async def test_used_unused_filter_shows_only_zero_usage(self) -> None:
        from fastapi import FastAPI
        from starlette.requests import Request

        from eggpool.dashboard.routes import handle_models

        app = FastAPI()
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
            "query_string": b"period=24h&used=unused",
            "app": app,
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive)
        response = await handle_models(request, used="unused")
        assert response.status_code == 200
        body = response.body.decode("utf-8")
        # Active row with traffic is filtered out.
        assert "gpt-4o" not in body
        # Zero-usage catalog-only models appear.
        assert "claude-opus-4" in body
        assert "llama-3-8b" in body

    @pytest.mark.asyncio()
    async def test_filter_form_preserves_filter_values(self) -> None:
        from fastapi import FastAPI
        from starlette.requests import Request

        from eggpool.dashboard.routes import handle_models

        app = FastAPI()

        class _StubStats:
            async def get_model_stats(
                self,
                _range: Any,
                *,
                account_name: str | None = None,
                use_cache: bool = True,
            ) -> list[dict[str, Any]]:
                return []

        class _StubDashboardConfig:
            enabled = True
            themes_dir = ""
            theme = "default"

        class _StubConfig:
            dashboard = _StubDashboardConfig()

        app.state.stats = _StubStats()
        app.state.config = _StubConfig()
        app.state.model_info = None
        app.state.catalog = _FakeCatalogService(_make_cache())
        app.state.stats_db = None
        app.state.dashboard_db = None

        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "path": "/models",
            "headers": [],
            "query_string": b"period=24h&used=unused&info_status=fresh",
            "app": app,
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive)
        response = await handle_models(request, used="unused", info_status="fresh")
        assert response.status_code == 200
        body = response.body.decode("utf-8")
        # Filter values are preserved in the form as selected options.
        assert 'value="unused" selected' in body
        assert 'value="fresh" selected' in body

    @pytest.mark.asyncio()
    async def test_empty_filter_result_shows_filter_message(self) -> None:
        from fastapi import FastAPI
        from starlette.requests import Request

        from eggpool.dashboard.routes import handle_models

        app = FastAPI()

        class _StubStats:
            async def get_model_stats(
                self,
                _range: Any,
                *,
                account_name: str | None = None,
                use_cache: bool = True,
            ) -> list[dict[str, Any]]:
                return []

        class _StubDashboardConfig:
            enabled = True
            themes_dir = ""
            theme = "default"

        class _StubConfig:
            dashboard = _StubDashboardConfig()

        app.state.stats = _StubStats()
        app.state.config = _StubConfig()
        app.state.model_info = None
        app.state.catalog = None
        app.state.stats_db = None
        app.state.dashboard_db = None

        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "path": "/models",
            "headers": [],
            "query_string": b"period=24h&used=used",
            "app": app,
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive)
        response = await handle_models(request, used="used")
        assert response.status_code == 200
        body = response.body.decode("utf-8")
        assert "No models match the selected filters." in body

    @pytest.mark.asyncio()
    async def test_no_filter_shows_no_models_message(self) -> None:
        from fastapi import FastAPI
        from starlette.requests import Request

        from eggpool.dashboard.routes import handle_models

        app = FastAPI()

        class _StubStats:
            async def get_model_stats(
                self,
                _range: Any,
                *,
                account_name: str | None = None,
                use_cache: bool = True,
            ) -> list[dict[str, Any]]:
                return []

        class _StubDashboardConfig:
            enabled = True
            themes_dir = ""
            theme = "default"

        class _StubConfig:
            dashboard = _StubDashboardConfig()

        app.state.stats = _StubStats()
        app.state.config = _StubConfig()
        app.state.model_info = None
        app.state.catalog = None
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
        assert "No models discovered from configured providers." in body


class TestCatalogRowsDiagnostics:
    """Diagnostic field tests: ``base_model_id``, ``providers``,
    ``available``/``catalog_status``, ``routing_priority``."""

    @pytest.mark.asyncio()
    async def test_sparse_rows_carry_base_model_id_and_routing_priority(
        self,
    ) -> None:
        from eggpool.dashboard.routes import _get_catalog_rows
        from eggpool.models.config import AppConfig, ProviderConfig

        catalog = _FakeCatalogService(_make_cache())
        # Build a minimal AppConfig with provider priorities for
        # openai=5, meta=3, anthropic=7.
        providers_cfg = {
            "openai": ProviderConfig.model_construct(routing_priority=5),
            "meta": ProviderConfig.model_construct(routing_priority=3),
            "anthropic": ProviderConfig.model_construct(routing_priority=7),
        }
        app_config = AppConfig.model_construct(providers=providers_cfg)
        rows = await _get_catalog_rows(catalog, config=app_config)
        by_id = {(r["model_id"], r["provider_id"]): r for r in rows}
        gpt = by_id[("gpt-4o", "openai")]
        assert gpt["base_model_id"] == "gpt-4o"
        assert gpt["available"] is True
        assert gpt["catalog_status"] == "available"
        assert gpt["routing_priority"] == 5
        meta = by_id[("llama-3-8b", "meta")]
        assert meta["routing_priority"] == 3

    @pytest.mark.asyncio()
    async def test_unresolved_protocol_marks_row_unavailable(self) -> None:
        """When a per-provider entry has ``protocol=None`` the row is
        marked ``available=False`` / ``catalog_status=unavailable``."""
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.dashboard.routes import _get_catalog_rows

        cache = ModelCatalogCache()
        now_ts = datetime.now(UTC).timestamp()
        # Simulate a stale sibling-protocol wipe: per-provider entry
        # has protocol=None.
        entry: dict[str, Any] = {
            "model_id": "gpt-4o",
            "display_name": "GPT-4o",
            "protocol": None,
            "capabilities": {},
            "source_metadata": {},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {"context_tokens": 128000},
        }
        cache._provider_models[("gpt-4o", "openai")] = entry
        catalog = _FakeCatalogService(cache)
        rows = await _get_catalog_rows(catalog)
        assert len(rows) == 1
        assert rows[0]["available"] is False
        assert rows[0]["catalog_status"] == "unavailable"

    def test_merge_lifts_diagnostic_fields_onto_active_rows(self) -> None:
        """When a stats row and a catalog row share the same
        ``(model_id, provider_id)`` key, the merge lifts diagnostic
        fields (base_model_id, available, catalog_status,
        routing_priority) onto the stats row."""
        from eggpool.dashboard.routes import _merge_models_with_catalog

        stats_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 10,
                "cost_microdollars": 100,
            },
        ]
        catalog_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "base_model_id": "gpt-4o",
                "available": True,
                "catalog_status": "available",
                "routing_priority": 5,
                "providers": ["openai"],
                "protocol": "openai",
                "display_name": "GPT-4o",
                "request_count": 0,
                "_sparse": True,
            },
        ]
        merged = _merge_models_with_catalog(stats_rows, catalog_rows)
        assert len(merged) == 1
        assert merged[0]["available"] is True
        assert merged[0]["catalog_status"] == "available"
        assert merged[0]["routing_priority"] == 5
        assert merged[0]["base_model_id"] == "gpt-4o"


class TestRenderAvailabilityPill:
    """Tests for the availability pill renderer."""

    def test_available_row_renders_available_pill(self) -> None:
        from eggpool.dashboard.render import _render_availability_pill

        html = _render_availability_pill(
            {"catalog_status": "available", "available": True}
        )
        assert "pill-available" in html
        assert "available" in html

    def test_unavailable_row_renders_unavailable_pill(self) -> None:
        from eggpool.dashboard.render import _render_availability_pill

        html = _render_availability_pill(
            {"catalog_status": "unavailable", "available": False}
        )
        assert "pill-unavailable" in html
        assert "unavailable" in html

    def test_row_without_catalog_status_renders_dash(self) -> None:
        from eggpool.dashboard.render import _render_availability_pill

        html = _render_availability_pill({})
        assert "pill-unknown" in html
        assert "—" in html


class TestRenderModelsAvailabilityAndPriority:
    """End-to-end tests: the rendered Models page surfaces the
    availability pill and routing priority column."""

    @pytest.mark.asyncio()
    async def test_rendered_table_includes_availability_pill(self) -> None:
        """An active row's availability badge is rendered."""
        from eggpool.dashboard.render import render_models

        rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "base_model_id": "gpt-4o",
                "available": True,
                "catalog_status": "available",
                "routing_priority": 5,
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
            },
        ]
        html = render_models(rows, period="24h")
        assert "pill-available" in html
        assert "available" in html
        # Routing priority column header is rendered.
        assert "Priority" in html
        # Priority value rendered for this row.
        assert ">5<" in html

    @pytest.mark.asyncio()
    async def test_rendered_table_renders_unavailable_pill_for_no_protocol(
        self,
    ) -> None:
        from eggpool.dashboard.render import render_models

        rows = [
            {
                "model_id": "stale-model",
                "provider_id": "openai",
                "available": False,
                "catalog_status": "unavailable",
                "request_count": 0,
                "cost_microdollars": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "tokens_per_second": 0.0,
                "avg_latency_ms": 0.0,
                "avg_ttft_ms": 0.0,
                "error_count": 0,
                "exact_count": 0,
                "derived_count": 0,
                "partial_count": 0,
                "estimated_count": 0,
                "unknown_count": 0,
                "provider_reported_count": 0,
                "estimated_cost_fraction": None,
                "cache_read_ratio": None,
                "cache_write_ratio": None,
                "reasoning_output_ratio": None,
                "avg_cost_per_request": None,
                "avg_cost_per_1k_tokens": None,
            },
        ]
        html = render_models(rows, period="24h")
        assert "pill-unavailable" in html
        assert "unavailable" in html


class TestCollapseModelsRouting:
    """Plan §Phase D §"Rendering changes" — Models page must honor
    ``models.collapse_models``.

    When ``collapse_models`` is ``False`` (default), each ``(model_id,
    provider_id)`` pair is listed as a separate row.  When it is
    ``True``, one row per unsuffixed model is listed, with a
    ``providers`` list collecting every contributing provider id.
    """

    @pytest.mark.asyncio()
    async def test_models_page_provider_scoped_rows_when_not_collapsed(
        self,
    ) -> None:
        from eggpool.dashboard.routes import _get_catalog_rows
        from eggpool.models.config import AppConfig, ModelsConfig

        catalog = _FakeCatalogService(_make_cache())
        app_config = AppConfig.model_construct(
            models=ModelsConfig.model_construct(collapse_models=False)
        )
        rows = await _get_catalog_rows(catalog, config=app_config)
        # Each (model_id, provider_id) pair becomes its own row.
        ids = sorted((r["model_id"], r["provider_id"]) for r in rows)
        assert ids == [
            ("claude-opus-4", "anthropic"),
            ("gpt-4o", "azure"),
            ("gpt-4o", "openai"),
            ("llama-3-8b", "meta"),
        ]
        # Provider-scoped rows carry a single-element ``providers``
        # list matching the row's ``provider_id``.
        for row in rows:
            assert row["providers"] == [row["provider_id"]]
            assert row["routing_priority"] == row["routing_priority_max"]

    @pytest.mark.asyncio()
    async def test_models_page_collapsed_rows_when_collapse_models_enabled(
        self,
    ) -> None:
        from eggpool.dashboard.routes import _get_catalog_rows
        from eggpool.models.config import (
            AppConfig,
            ModelsConfig,
            ProviderConfig,
        )

        catalog = _FakeCatalogService(_make_cache())
        providers_cfg = {
            "openai": ProviderConfig.model_construct(routing_priority=5),
            "azure": ProviderConfig.model_construct(routing_priority=4),
            "anthropic": ProviderConfig.model_construct(routing_priority=7),
            "meta": ProviderConfig.model_construct(routing_priority=3),
        }
        app_config = AppConfig.model_construct(
            models=ModelsConfig.model_construct(collapse_models=True),
            providers=providers_cfg,
        )
        rows = await _get_catalog_rows(catalog, config=app_config)
        # gpt-4o is shared by openai + azure; it must collapse to a
        # single row with both providers listed.
        ids = sorted(r["model_id"] for r in rows)
        assert ids == [
            "claude-opus-4",
            "gpt-4o",
            "llama-3-8b",
        ]
        gpt = next(r for r in rows if r["model_id"] == "gpt-4o")
        assert gpt["providers"] == ["azure", "openai"]
        # Primary provider is the first sorted entry so stats keyed by
        # ``(model_id, provider_id)`` still find the row.
        assert gpt["provider_id"] == "azure"
        # routing_priority_max reflects the max priority across
        # contributing providers.
        assert gpt["routing_priority"] == 5
        assert gpt["routing_priority_max"] == 5
        # base_model_id is the unsuffixed key (no provider suffix).
        assert gpt["base_model_id"] == "gpt-4o"
        # Each collapsed entry remains marked available because the
        # catalog entry has a resolved protocol.
        assert gpt["available"] is True
        assert gpt["catalog_status"] == "available"

    @pytest.mark.asyncio()
    async def test_collapse_models_default_is_provider_scoped(self) -> None:
        """When ``collapse_models`` is missing from the config the
        dashboard must keep the historical provider-scoped shape."""
        from eggpool.dashboard.routes import _get_catalog_rows
        from eggpool.models.config import AppConfig, ModelsConfig

        catalog = _FakeCatalogService(_make_cache())
        # Build a config WITHOUT collapse_models attribute on models.
        models_cfg = ModelsConfig.model_construct()
        assert models_cfg.collapse_models is False
        app_config = AppConfig.model_construct(models=models_cfg)
        rows = await _get_catalog_rows(catalog, config=app_config)
        ids = sorted((r["model_id"], r["provider_id"]) for r in rows)
        assert ids == [
            ("claude-opus-4", "anthropic"),
            ("gpt-4o", "azure"),
            ("gpt-4o", "openai"),
            ("llama-3-8b", "meta"),
        ]


class TestModelInfoPillUsesBaseModelId:
    """Plan §Phase D §"Rendering changes" — model-info pill lookup
    must use ``base_model_id`` (the unsuffixed key) for provider-
    suffixed rows so the canonical model-info entry resolves
    correctly.
    """

    def test_pill_lookup_prefers_base_model_id(self) -> None:
        """When a row's ``model_id`` is suffixed (``gpt-4o/openai``)
        and ``base_model_id`` is unsuffixed (``gpt-4o``), the model-
        info pill must read from the unsuffixed map entry."""
        from eggpool.dashboard.render import render_models

        rows = [
            {
                "model_id": "gpt-4o/openai",
                "provider_id": "openai",
                "base_model_id": "gpt-4o",
                "available": True,
                "catalog_status": "available",
                "routing_priority": 5,
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
            },
        ]
        # Map keyed by the unsuffixed model id — the canonical key
        # used by the model-info service.
        model_info_map = {
            "gpt-4o": {
                "status": "fresh",
                "summary": "Fresh canonical entry for gpt-4o",
                "sources": ["provider_catalog"],
                "last_refreshed_at": "2026-07-01T00:00:00Z",
            },
        }
        html = render_models(
            rows,
            period="24h",
            model_info_map=model_info_map,
        )
        # Fresh pill is rendered for the row, proving the lookup
        # resolved via base_model_id.
        assert "pill-fresh" in html
        assert "Fresh canonical entry for gpt-4o" in html

    def test_pill_lookup_falls_back_to_model_id(self) -> None:
        """When ``base_model_id`` is missing or empty the lookup
        must fall back to the literal ``model_id``."""
        from eggpool.dashboard.render import render_models

        rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "base_model_id": "",
                "available": True,
                "catalog_status": "available",
                "routing_priority": 5,
                "request_count": 5,
                "cost_microdollars": 50,
                "input_tokens": 500,
                "output_tokens": 250,
                "total_tokens": 750,
                "tokens_per_second": 0.0,
                "avg_latency_ms": 200.0,
                "avg_ttft_ms": 80.0,
                "error_count": 0,
                "exact_count": 5,
                "derived_count": 0,
                "partial_count": 0,
                "estimated_count": 0,
                "unknown_count": 0,
                "provider_reported_count": 5,
                "estimated_cost_fraction": 0.1,
                "cache_read_ratio": 0.0,
                "cache_write_ratio": 0.0,
                "reasoning_output_ratio": 0.0,
                "avg_cost_per_request": 10,
                "avg_cost_per_1k_tokens": 6,
            },
        ]
        model_info_map = {
            "gpt-4o": {
                "status": "partial",
                "summary": "Fallback path",
                "sources": ["provider_catalog"],
                "last_refreshed_at": None,
            },
        }
        html = render_models(
            rows,
            period="24h",
            model_info_map=model_info_map,
        )
        assert "pill-partial" in html


class TestModelDetailLinkURLEncoding:
    """Plan §Phase D §"Rendering changes" — detail links must
    safely encode provider-suffixed model ids.
    """

    def test_model_detail_link_url_encodes_provider_suffixed_id(self) -> None:
        """A row whose ``model_id`` is ``gpt-4o/openai`` must produce
        a detail link that routes correctly.  ``html.escape`` keeps
        ``/`` as a path character so the literal ``/`` is preserved
        in the href, and the FastAPI ``{model_id:path}`` route
        captures the suffix correctly."""
        from html.parser import HTMLParser

        from eggpool.dashboard.render import render_models

        rows = [
            {
                "model_id": "gpt-4o/openai",
                "provider_id": "openai",
                "base_model_id": "gpt-4o",
                "available": True,
                "catalog_status": "available",
                "routing_priority": 5,
                "request_count": 3,
                "cost_microdollars": 30,
                "input_tokens": 300,
                "output_tokens": 150,
                "total_tokens": 450,
                "tokens_per_second": 0.0,
                "avg_latency_ms": 200.0,
                "avg_ttft_ms": 80.0,
                "error_count": 0,
                "exact_count": 3,
                "derived_count": 0,
                "partial_count": 0,
                "estimated_count": 0,
                "unknown_count": 0,
                "provider_reported_count": 3,
                "estimated_cost_fraction": 0.1,
                "cache_read_ratio": 0.0,
                "cache_write_ratio": 0.0,
                "reasoning_output_ratio": 0.0,
                "avg_cost_per_request": 10,
                "avg_cost_per_1k_tokens": 6,
            },
        ]
        html = render_models(rows, period="24h", current_theme="default")

        # Extract every /models/... href from the rendered table.
        class _HrefFinder(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.hrefs: list[str] = []

            def handle_starttag(
                self, tag: str, attrs: list[tuple[str, str | None]]
            ) -> None:
                if tag != "a":
                    return
                for name, value in attrs:
                    if name == "href" and value is not None:
                        self.hrefs.append(value)

        finder = _HrefFinder()
        finder.feed(html)
        detail_links = [h for h in finder.hrefs if h.startswith("/models/")]
        assert detail_links, "expected a /models/... detail link"
        # The suffixed id ``gpt-4o/openai`` must survive the href
        # either as a literal ``/`` or its URL-encoded ``%2F``.  Both
        # forms route correctly because the FastAPI path converter
        # is ``{model_id:path}``.
        assert any(
            "gpt-4o/openai" in href or "gpt-4o%2Fopenai" in href
            for href in detail_links
        ), detail_links

    def test_link_handles_model_id_with_special_chars(self) -> None:
        """Defensive: characters that need HTML-escaping (quotes,
        angle brackets) must be escaped so the link href is well-
        formed, but route-relevant characters survive."""
        from eggpool.dashboard.render import render_models

        rows = [
            {
                "model_id": 'model<with>"quotes"',
                "provider_id": "p",
                "base_model_id": 'model<with>"quotes"',
                "available": True,
                "catalog_status": "available",
                "routing_priority": 1,
                "request_count": 1,
                "cost_microdollars": 1,
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "tokens_per_second": 0.0,
                "avg_latency_ms": 1.0,
                "avg_ttft_ms": 1.0,
                "error_count": 0,
                "exact_count": 0,
                "derived_count": 0,
                "partial_count": 0,
                "estimated_count": 0,
                "unknown_count": 0,
                "provider_reported_count": 1,
                "estimated_cost_fraction": None,
                "cache_read_ratio": None,
                "cache_write_ratio": None,
                "reasoning_output_ratio": None,
                "avg_cost_per_request": None,
                "avg_cost_per_1k_tokens": None,
            },
        ]
        html = render_models(rows, period="24h")
        # Use raw-string scanning instead of an HTMLParser — the
        # parser decodes ``&quot;`` etc. back to ``"``, ``<``, ``>``
        # which makes the unescaped-attribute check impossible.  We
        # want to verify the raw emitted href markup is well-formed.
        import re

        href_pattern = re.compile(r'href="([^"]*)"')
        raw_hrefs = href_pattern.findall(html)
        detail_links = [h for h in raw_hrefs if h.startswith("/models/")]
        assert detail_links
        # ``<``, ``>``, ``"`` must NOT appear as raw bytes inside the
        # href attribute — they would terminate the attribute value.
        # (HTML-escaped forms like ``&quot;`` are fine.)
        for href in detail_links:
            assert "<" not in href
            assert ">" not in href
            assert '"' not in href
            # ``&quot;`` confirms proper HTML escaping.
            assert "&quot;" in href
            assert "&lt;" in href
            assert "&gt;" in href
