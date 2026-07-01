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
    cache.set_account_provider("acct-a", "openai")
    cache.set_account_provider("acct-b", "meta")
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
        # acct-a supports gpt-4o through its own provider only.
        assert ids == [
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


class TestMergeProviderScopedClosure:
    """Plan §Defect 1 — provider-scoped merge must dedupe by
    ``(model_id, provider_id)`` so an unused sibling provider for the
    same base model is not suppressed by an active sibling's stats
    row.  Collapsed mode continues to dedupe by model_id only.
    """

    def test_provider_scoped_keeps_unused_sibling_provider(self) -> None:
        """A stats row with activity on ``(gpt-4o, openai)`` must
        not suppress the unused ``(gpt-4o, openrouter)`` catalog
        row.  Both pairs render."""
        from eggpool.dashboard.routes import _merge_models_with_catalog

        catalog_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 0,
                "_sparse": True,
            },
            {
                "model_id": "gpt-4o",
                "provider_id": "openrouter",
                "request_count": 0,
                "_sparse": True,
            },
        ]
        stats_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 12,
                "cost_microdollars": 60,
            },
        ]
        merged = _merge_models_with_catalog(
            stats_rows, catalog_rows, collapse_models=False
        )
        keys = sorted((r.get("model_id"), r.get("provider_id")) for r in merged)
        assert keys == [
            ("gpt-4o", "openai"),
            ("gpt-4o", "openrouter"),
        ]
        # Active row carries real traffic.
        by_key = {(r["model_id"], r["provider_id"]): r for r in merged}
        assert by_key[("gpt-4o", "openai")]["request_count"] == 12
        # Unused sibling remains a sparse catalog row.
        assert by_key[("gpt-4o", "openrouter")]["request_count"] == 0
        assert by_key[("gpt-4o", "openrouter")].get("_in_catalog") is True

    def test_provider_scoped_legacy_stats_without_provider_does_not_hide_providers(
        self,
    ) -> None:
        """A legacy stats row that omits ``provider_id`` falls back
        to ``catalog_by_id`` for diagnostic fields but does NOT
        suppress provider-scoped catalog rows with explicit
        provider IDs."""
        from eggpool.dashboard.routes import _merge_models_with_catalog

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
                "request_count": 0,
                "_sparse": True,
            },
            {
                "model_id": "gpt-4o",
                "provider_id": "openrouter",
                "base_model_id": "gpt-4o",
                "available": True,
                "catalog_status": "available",
                "routing_priority": 4,
                "providers": ["openrouter"],
                "protocol": "openai",
                "request_count": 0,
                "_sparse": True,
            },
        ]
        stats_rows = [
            {
                "model_id": "gpt-4o",
                "request_count": 12,
                "cost_microdollars": 60,
            },
        ]
        merged = _merge_models_with_catalog(
            stats_rows, catalog_rows, collapse_models=False
        )
        keys = sorted((r.get("model_id"), r.get("provider_id") or "") for r in merged)
        # Both explicit-provider catalog rows must render alongside
        # the legacy stats row that uses model_id-only dedup.
        provider_pairs = [
            (mid, pid) for mid, pid in keys if pid in ("openai", "openrouter")
        ]
        assert provider_pairs == [
            ("gpt-4o", "openai"),
            ("gpt-4o", "openrouter"),
        ]

    def test_collapsed_still_dedupes_by_model_id(self) -> None:
        """In collapsed mode the merge keeps one row per
        unsuffixed model_id and lifts ``providers`` from the
        catalog row onto the stats row."""
        from eggpool.dashboard.routes import _merge_models_with_catalog

        catalog_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "base_model_id": "gpt-4o",
                "providers": ["openai", "openrouter"],
                "available": True,
                "catalog_status": "available",
                "routing_priority": 5,
                "routing_priority_max": 5,
                "protocol": "openai",
                "request_count": 0,
                "_sparse": True,
            },
        ]
        stats_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 12,
                "cost_microdollars": 60,
            },
        ]
        merged = _merge_models_with_catalog(
            stats_rows, catalog_rows, collapse_models=True
        )
        gpt_rows = [r for r in merged if r.get("model_id") == "gpt-4o"]
        assert len(gpt_rows) == 1
        # Catalog diagnostic fields are lifted onto the active row.
        assert gpt_rows[0]["providers"] == ["openai", "openrouter"]
        assert gpt_rows[0]["available"] is True
        assert gpt_rows[0]["request_count"] == 12

    def test_collapse_models_kwarg_defaults_to_provider_scoped(self) -> None:
        """When ``collapse_models`` is omitted the merge keeps
        provider-scoped behavior — the historical default."""
        from eggpool.dashboard.routes import _merge_models_with_catalog

        catalog_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 0,
                "_sparse": True,
            },
            {
                "model_id": "gpt-4o",
                "provider_id": "openrouter",
                "request_count": 0,
                "_sparse": True,
            },
        ]
        stats_rows = [
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 12,
            },
        ]
        merged_default = _merge_models_with_catalog(stats_rows, catalog_rows)
        merged_explicit = _merge_models_with_catalog(
            stats_rows, catalog_rows, collapse_models=False
        )
        default_keys = sorted(
            (r.get("model_id"), r.get("provider_id")) for r in merged_default
        )
        explicit_keys = sorted(
            (r.get("model_id"), r.get("provider_id")) for r in merged_explicit
        )
        assert default_keys == explicit_keys

    @pytest.mark.asyncio()
    async def test_models_page_catalog_complete_mixed_used_unused_provider_rows(
        self,
    ) -> None:
        """End-to-end: the Models page renders every
        ``(model_id, provider_id)`` catalog exposure even when one
        provider has traffic and another does not."""
        from fastapi import FastAPI
        from starlette.requests import Request

        from eggpool.dashboard.routes import handle_models

        catalog = _FakeCatalogService(_make_cache())

        class _StubStats:
            async def get_model_stats(
                self,
                _range: Any,
                *,
                account_name: str | None = None,
                use_cache: bool = True,
            ) -> list[dict[str, Any]]:
                # Only ``gpt-4o/openai`` has traffic; ``gpt-4o/azure``
                # is an unused sibling. The fixture cache exposes
                # gpt-4o under both providers.
                return [
                    {
                        "model_id": "gpt-4o",
                        "provider_id": "openai",
                        "request_count": 12,
                        "cost_microdollars": 60,
                        "input_tokens": 1200,
                        "output_tokens": 600,
                        "total_tokens": 1800,
                        "tokens_per_second": 0.0,
                        "avg_latency_ms": 200.0,
                        "avg_ttft_ms": 80.0,
                        "error_count": 0,
                        "exact_count": 6,
                        "derived_count": 6,
                        "partial_count": 0,
                        "estimated_count": 0,
                        "unknown_count": 0,
                        "provider_reported_count": 12,
                        "estimated_cost_fraction": 0.1,
                        "cache_read_ratio": 0.0,
                        "cache_write_ratio": 0.0,
                        "reasoning_output_ratio": 0.0,
                        "avg_cost_per_request": 5,
                        "avg_cost_per_1k_tokens": 3,
                    },
                ]

        class _StubDashboardConfig:
            enabled = True
            themes_dir = ""
            theme = "default"

        class _StubConfig:
            dashboard = _StubDashboardConfig()

        app = FastAPI()
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
        # Unused sibling provider still appears.
        assert ">azure<" in body
        # Active provider still appears.
        assert ">openai<" in body


class TestInfoStatusFilterAliases:
    """Plan §Optional tightening — the info_status filter must
    accept both the canonical status names (``sparse_new``,
    ``conflicting``) and the display aliases the compact
    ``/api/model-info`` summary exposes (``sparse``,
    ``conflict``).
    """

    def test_info_status_filter_accepts_sparse_and_sparse_new(self) -> None:
        from eggpool.dashboard.routes import _apply_model_filters

        rows = [
            {
                "model_id": "gpt-4o",
                "base_model_id": "gpt-4o",
                "request_count": 10,
            },
            {
                "model_id": "llama-3-8b",
                "base_model_id": "llama-3-8b",
                "request_count": 5,
            },
        ]
        mi_map = {
            "gpt-4o": {"status": "sparse_new"},
            "llama-3-8b": {"status": "conflicting"},
        }
        canonical = _apply_model_filters(
            rows, info_status="sparse_new", model_info_map=mi_map
        )
        assert [r["model_id"] for r in canonical] == ["gpt-4o"]
        # The display alias ``sparse`` resolves to the canonical
        # ``sparse_new`` and matches the same row.
        alias = _apply_model_filters(rows, info_status="sparse", model_info_map=mi_map)
        assert [r["model_id"] for r in alias] == ["gpt-4o"]
        # ``conflict`` (display) → ``conflicting`` (canonical).
        alias_conflict = _apply_model_filters(
            rows, info_status="conflict", model_info_map=mi_map
        )
        assert [r["model_id"] for r in alias_conflict] == ["llama-3-8b"]


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
        # The active ``gpt-4o/openai`` row (count=10) is filtered out
        # by ``used=unused``.  The unused provider-scoped sibling
        # ``gpt-4o/azure`` stays visible because the Models page is
        # catalog-complete in provider-scoped mode.
        assert "openai" not in body
        assert "azure" in body
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
    async def test_handle_models_uses_collapsed_merge_when_enabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The handler must pass ``collapse_models`` through to the merge.

        A stale second merge using the default provider-scoped keying
        would render both the active stats row and the sparse collapsed
        catalog row for the same model.
        """
        from fastapi import FastAPI
        from starlette.requests import Request

        from eggpool.dashboard import routes as routes_module
        from eggpool.models.config import ModelsConfig

        captured: dict[str, Any] = {}

        def _capture_render_models(
            rows: list[dict[str, Any]],
            **_kwargs: Any,
        ) -> str:
            captured["rows"] = rows
            return "<html></html>"

        monkeypatch.setattr(routes_module, "render_models", _capture_render_models)

        class _StubStats:
            async def get_model_stats(
                self,
                _range: Any,
                *,
                account_name: str | None = None,
                use_cache: bool = True,
            ) -> list[dict[str, Any]]:
                return [
                    {
                        "model_id": "gpt-4o",
                        "provider_id": "openai",
                        "request_count": 12,
                    }
                ]

        class _StubDashboardConfig:
            enabled = True
            themes_dir = ""
            theme = "default"

        class _StubConfig:
            dashboard = _StubDashboardConfig()
            models = ModelsConfig.model_construct(collapse_models=True)
            providers: dict[str, Any] = {}

        app = FastAPI()
        app.state.stats = _StubStats()
        app.state.config = _StubConfig()
        app.state.model_info = None
        app.state.catalog = _FakeCatalogService(_make_cache())

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

        response = await routes_module.handle_models(Request(scope, receive))

        assert response.status_code == 200
        rows = cast("list[dict[str, Any]]", captured["rows"])
        gpt_rows = [r for r in rows if r.get("model_id") == "gpt-4o"]
        assert len(gpt_rows) == 1
        assert gpt_rows[0]["request_count"] == 12
        assert gpt_rows[0]["providers"] == ["azure", "openai"]

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

    def test_read_collapse_models_ignores_non_bool_values(self) -> None:
        from eggpool.dashboard.routes import _read_collapse_models

        class _Models:
            collapse_models = "false"

        class _Config:
            models = _Models()

        assert _read_collapse_models(_Config()) is False


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
        """Defensive: characters that would break the route path or
        terminate the href attribute (``?``, ``#``, ``<``, ``>``,
        ``"``) must be percent-encoded so the emitted markup is
        safe and routes correctly.  The detail handler
        (``/models/{model_id:path}``) ``unquote()``s the path so
        the original characters are recovered server-side."""
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
        import re

        href_pattern = re.compile(r'href="([^"]*)"')
        raw_hrefs = href_pattern.findall(html)
        detail_links = [h for h in raw_hrefs if h.startswith("/models/")]
        assert detail_links
        # None of the raw HTML-special characters may appear inside
        # the href attribute — they would terminate the quoted value
        # or change the URL grammar.  The percent-encoded forms
        # (``%22`` for ``"``, ``%3C`` for ``<``, ``%3E`` for ``>``)
        # prove that ``urllib.parse.quote`` was applied to the path
        # segment.
        for href in detail_links:
            assert "<" not in href
            assert ">" not in href
            assert '"' not in href
            # The percent-encoded forms prove that
            # ``urllib.parse.quote`` was applied to the path
            # segment.  ``%3C`` encodes ``<``, ``%3E`` encodes
            # ``>``, ``%22`` encodes ``"``.
            assert "%3C" in href
            assert "%3E" in href
            assert "%22" in href
