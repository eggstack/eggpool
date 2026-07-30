"""Tests for Phase H: dashboard model-info join regression coverage.

The dashboard ``/models`` page must join the canonical model-info
summary map onto rendered rows even when catalog rows carry
provider-suffixed ids (``minimax-m3/opencode-go``) and even when the
catalog service raises during row construction.  These tests pin the
end-to-end contract so a regression cannot silently revert to
"API-correct / dashboard-empty" behaviour.

Covers:

* Renderer joins provider-suffixed row to canonical summary.
* Route normalizes suffixed stats-only rows before requesting summaries.
* Catalog row construction does not silently disappear on exception.
* Join diagnostics trigger on all-miss state and stay quiet on empty
  model-info.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from eggpool.dashboard.render import render_models, render_overview
from eggpool.dashboard.routes import (
    CatalogRowsState,
    ModelInfoDashboardState,
    _compute_model_info_join_stats,
    _get_catalog_rows,
    _get_provider_scoped_catalog_rows,
    _normalize_dashboard_model_row,
    handle_models,
)
from eggpool.model_info.types import CanonicalModelInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_request(app_state: Any) -> Request:
    """Build a minimal ``starlette.requests.Request`` wrapping *app_state*."""

    class _App:
        state = app_state

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/models",
        "headers": [],
        "query_string": b"period=24h",
        "app": _App(),
    }

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


class _StubStats:
    """Minimal stats stub returning ``rows`` from ``get_model_stats``."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls = 0

    async def get_model_stats(
        self,
        _range: Any,
        *,
        account_name: str | None = None,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self.rows)


class _StubDashboardConfig:
    enabled = True
    themes_dir = ""
    theme = "default"


class _StubConfig:
    """Minimal config stub surfacing ``providers`` to the route."""

    def __init__(self, providers: dict[str, Any] | None = None) -> None:
        self.providers = providers or {}
        self.dashboard = _StubDashboardConfig()


class _StubModelInfoService:
    """Service stub that records the ``model_ids`` argument and returns a map."""

    def __init__(self, summary_map: dict[str, Any]) -> None:
        self.summary_map = summary_map
        self.calls: list[Any] = []

    async def get_summary_map(self, model_ids: Any = None) -> dict[str, Any]:
        self.calls.append(model_ids)
        return self.summary_map


def _make_canonical(model_id: str, **overrides: Any) -> CanonicalModelInfo:
    """Build a :class:`CanonicalModelInfo` with sensible defaults for tests."""
    now = datetime.now(UTC)
    defaults: dict[str, Any] = {
        "model_id": model_id,
        "status": "partial",
        "summary": f"Test summary for {model_id}",
        "sparse": False,
        "detail": {},
        "provenance": {},
        "conflicts": {},
        "first_seen_at": now,
        "last_seen_at": now,
        "last_refreshed_at": now,
        "next_refresh_at": now,
    }
    defaults.update(overrides)
    return CanonicalModelInfo(**defaults)


# ---------------------------------------------------------------------------
# Phase H.1 — Renderer joins provider-suffixed row to canonical summary
# ---------------------------------------------------------------------------


def test_render_models_joins_provider_suffixed_row_to_canonical_summary() -> None:
    """A row whose ``model_id`` is ``minimax-m3/opencode-go`` and
    ``base_model_id`` is ``minimax-m3`` must still match the canonical
    summary keyed by ``minimax-m3``."""
    rows = [
        {
            "model_id": "minimax-m3/opencode-go",
            "base_model_id": "minimax-m3",
            "provider_id": "opencode-go",
            "_model_info_lookup_id": "minimax-m3",
            "request_count": 0,
        },
    ]
    summary_map = {
        "minimax-m3": {
            "model_id": "minimax-m3",
            "status": "partial",
            "sparse": False,
            "summary": "Callable via opencode-go.",
            "sources": ["provider_catalog", "openrouter"],
            "last_refreshed_at": "2026-07-04T15:42:40Z",
        },
    }
    html = render_models(
        models=rows,
        period="24h",
        model_info_map=summary_map,
    )
    assert "pill-partial" in html
    assert "No model info available" not in html
    # Tooltip surfaces sources verbatim via data-tooltip / aria-label.
    assert "Sources: provider_catalog, openrouter" in html
    # Debug attribute carries the lookup key so operators can grep.
    assert 'data-model-info-key="minimax-m3"' in html
    assert 'data-model-id="minimax-m3/opencode-go"' in html


def test_render_models_shows_public_benchmark_values() -> None:
    """The models table exposes actual benchmark values, not just a status."""
    html = render_models(
        models=[
            {
                "model_id": "minimax-m3",
                "provider_id": "opencode-go",
                "request_count": 1,
            }
        ],
        period="24h",
        model_info_map={
            "minimax-m3": {
                "model_id": "minimax-m3",
                "status": "fresh",
                "summary": "Benchmark metadata available from OpenRouter.",
                "sources": ["provider_catalog", "openrouter"],
                "benchmarks": [
                    {
                        "name": "Artificial Analysis Intelligence Index",
                        "score": 44.4,
                        "source": "artificial_analysis",
                        "notes": "Composite intelligence index",
                    },
                    {
                        "name": "Design Arena: models / website",
                        "score": 1295,
                        "rank": 15,
                        "source": "openrouter",
                    },
                ],
                "benchmark_count": 2,
            },
        },
    )
    assert "Benchmarks" in html
    assert "AA 44.4" in html
    assert "Arena 1295 #15" in html
    assert "Composite intelligence index" in html
    assert "Benchmarks: AA: Intelligence Index 44.4" in html


def test_render_overview_links_model_with_summary_and_benchmarks() -> None:
    """Top-model rows link to model info and keep the tooltip compact."""
    html = render_overview(
        overview={
            "summary": {"total_requests": 1},
            "imbalance": {"imbalance_ratio": 0.0},
        },
        accounts=[],
        models=[
            {
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "request_count": 1,
                "cost_microdollars": 0,
                "total_tokens": 10,
            }
        ],
        model_info_map={
            "gpt-4o": {
                "model_id": "gpt-4o",
                "summary": "OpenAI vision model.",
                "benchmarks": [
                    {
                        "name": "MMLU",
                        "score": 88.7,
                        "source": "artificial_analysis",
                    }
                ],
                "benchmark_count": 1,
            }
        },
    )
    assert 'href="/models/gpt-4o?theme="' in html
    assert "OpenAI vision model." in html
    assert "Benchmarks: AA: MMLU 88.7" in html


def test_render_models_lookup_id_wins_over_literal_model_id() -> None:
    """``_model_info_lookup_id`` is consulted before ``base_model_id``
    and the literal ``model_id``."""
    rows = [
        {
            "model_id": "minimax-m3/opencode-go",
            "base_model_id": "wrong-base",
            "_model_info_lookup_id": "minimax-m3",
            "request_count": 0,
        },
    ]
    summary_map = {
        "minimax-m3": {
            "model_id": "minimax-m3",
            "status": "fresh",
            "sparse": False,
            "summary": "Looked up by lookup_id.",
            "sources": ["openrouter"],
            "last_refreshed_at": "2026-07-04T15:42:40Z",
        },
    }
    html = render_models(
        models=rows,
        period="24h",
        model_info_map=summary_map,
    )
    assert "pill-fresh" in html
    assert "No model info available" not in html


# ---------------------------------------------------------------------------
# Phase H.2 — Route normalization strips provider suffix before requesting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_handle_models_normalizes_suffixed_stats_rows() -> None:
    """Stats rows carrying provider-suffixed ids must still hit the
    canonical summary map; the route's normalization step splits them
    into ``base_model_id`` + ``provider_id`` and stores
    ``_model_info_lookup_id`` so the renderer can match."""

    class _MinimalCatalogService:
        cache = None

        def get_models_for_exposure(
            self, health_manager: Any = None
        ) -> list[dict[str, Any]]:
            return []

    class _ProviderCatalogService:
        def get_provider_model_entries(self) -> dict[tuple[str, str], dict[str, Any]]:
            return {("minimax-m3", "opencode-go"): {"protocol": "openai"}}

    stats = _StubStats(
        rows=[
            {
                "model_id": "minimax-m3/opencode-go",
                "provider_id": "opencode-go",
                "request_count": 7,
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
                "estimated_cost_fraction": 0.0,
                "cache_read_ratio": 0.0,
                "cache_write_ratio": 0.0,
                "reasoning_output_ratio": 0.0,
                "avg_cost_per_request": 0,
                "avg_cost_per_1k_tokens": 0,
            },
        ],
    )
    mi_service = _StubModelInfoService(
        summary_map={
            "minimax-m3": _make_canonical("minimax-m3"),
        },
    )
    app = FastAPI()
    app.state.stats = stats
    app.state.config = _StubConfig(
        providers={"opencode-go": type("_P", (), {"routing_priority": 5})()},
    )
    app.state.model_info = mi_service
    app.state.catalog = _ProviderCatalogService()
    app.state.stats_db = None
    app.state.dashboard_db = None
    request = _stub_request(app.state)
    response = await handle_models(request)
    assert response.status_code == 200
    body = response.body.decode("utf-8")
    assert "pill-partial" in body
    assert "No model info available" not in body
    # The summary fetch is requested with ``None`` so the canonical
    # table returns everything available; we still expect the renderer
    # to match the rendered row via the lookup-id normalization.
    assert mi_service.calls and mi_service.calls[0] is None


@pytest.mark.asyncio()
async def test_handle_models_normalizes_suffixed_stats_rows_when_catalog_missing() -> (
    None
):
    """When the catalog is unattached, stats rows that carry a
    provider-suffixed id must still hit the canonical summary map."""

    stats = _StubStats(
        rows=[
            {
                "model_id": "minimax-m3/opencode-go",
                "provider_id": "opencode-go",
                "request_count": 3,
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
                "estimated_cost_fraction": 0.0,
                "cache_read_ratio": 0.0,
                "cache_write_ratio": 0.0,
                "reasoning_output_ratio": 0.0,
                "avg_cost_per_request": 0,
                "avg_cost_per_1k_tokens": 0,
            },
        ],
    )
    mi_service = _StubModelInfoService(
        summary_map={
            "minimax-m3": _make_canonical("minimax-m3"),
        },
    )
    app = FastAPI()
    app.state.stats = stats
    app.state.config = _StubConfig()
    app.state.model_info = mi_service
    app.state.catalog = None
    app.state.stats_db = None
    app.state.dashboard_db = None
    request = _stub_request(app.state)
    response = await handle_models(request)
    assert response.status_code == 200
    body = response.body.decode("utf-8")
    assert "pill-partial" in body
    assert "No model info available" not in body


# ---------------------------------------------------------------------------
# Phase H.3 — Catalog row construction does not silently disappear
# ---------------------------------------------------------------------------


def test_normalize_helper_strips_known_provider_suffix() -> None:
    row = {"model_id": "minimax-m3/opencode-go"}
    out = _normalize_dashboard_model_row(row, known_providers={"opencode-go"})
    assert out["base_model_id"] == "minimax-m3"
    assert out["provider_id"] == "opencode-go"
    assert out["_model_info_lookup_id"] == "minimax-m3"
    assert out["_model_id_was_suffixed"] is True


def test_normalize_helper_preserves_existing_base_model_id() -> None:
    row = {
        "model_id": "minimax-m3",
        "base_model_id": "minimax-m3",
        "provider_id": "",
    }
    out = _normalize_dashboard_model_row(row, known_providers={"opencode-go"})
    assert out["base_model_id"] == "minimax-m3"
    assert out["provider_id"] == ""
    assert out["_model_info_lookup_id"] == "minimax-m3"
    assert out["_model_id_was_suffixed"] is False


def test_normalize_helper_keeps_unknown_suffix_intact() -> None:
    """If the suffix is not a known provider the parser leaves the id
    intact, which is the same behaviour the routing layer relies on."""
    row = {"model_id": "minimax-m3/unknown-provider"}
    out = _normalize_dashboard_model_row(row, known_providers={"opencode-go"})
    assert out["base_model_id"] == "minimax-m3/unknown-provider"
    assert out["provider_id"] == ""
    assert out["_model_info_lookup_id"] == "minimax-m3/unknown-provider"
    assert out["_model_id_was_suffixed"] is False


@pytest.mark.asyncio()
async def test_get_catalog_rows_logs_and_surfaces_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the underlying catalog accessor raises, the helper must log
    the traceback (instead of swallowing it) and return a
    ``CatalogRowsState`` with ``degraded_reason="fetch_error"``."""

    class _BrokenCache:
        def get_provider_model_entries(self) -> dict[tuple[str, str], dict[str, Any]]:
            raise RuntimeError("catalog exploded")

    class _BrokenCatalogService:
        cache = _BrokenCache()

    import logging

    with caplog.at_level(logging.WARNING, logger="eggpool.dashboard.routes"):
        state = await _get_catalog_rows(_BrokenCatalogService())
    assert state.degraded_reason == "fetch_error"
    assert state.error_class == "RuntimeError"
    assert state.rows == []
    # The traceback is logged at exception level — verify by checking
    # the caplog captured a record with a relevant message.
    assert any(
        "Failed to enumerate provider-scoped catalog rows" in record.message
        for record in caplog.records
    )


def test_get_provider_scoped_catalog_rows_returns_state_dataclass() -> None:
    """Smoke check: the new return shape is a ``CatalogRowsState``."""
    from datetime import UTC, datetime

    from eggpool.catalog.cache import ModelCatalogCache

    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    entry = {
        "model_id": "minimax-m3",
        "display_name": "minimax-m3",
        "protocol": "openai",
        "capabilities": {},
        "source_metadata": {},
        "first_seen_at": now_ts,
        "last_seen_at": now_ts,
        "discovered_limits": {},
        "effective_limits": {"context_tokens": 128000},
    }
    cache._provider_models[("minimax-m3", "opencode-go")] = entry

    state = _get_provider_scoped_catalog_rows(
        type("_Svc", (), {"cache": cache})(),
        priority_by_provider={},
        account=None,
    )
    assert isinstance(state, CatalogRowsState)
    assert state.degraded_reason is None
    assert state.row_count == 1
    row = state.rows[0]
    assert row["base_model_id"] == "minimax-m3"
    assert row["model_id"] == "minimax-m3"
    assert row["provider_id"] == "opencode-go"


@pytest.mark.asyncio()
async def test_handle_models_logs_when_catalog_attached_but_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the catalog is attached but returns zero rows the route must
    log a warning so operators see a diagnostic instead of an empty
    page."""

    class _EmptyCatalogService:
        cache = type("_C", (), {"get_provider_model_entries": lambda self: {}})()

        def get_models_for_exposure(
            self, health_manager: Any = None
        ) -> list[dict[str, Any]]:
            return []

    import logging

    stats = _StubStats()
    mi_service = _StubModelInfoService(summary_map={})
    app = FastAPI()
    app.state.stats = stats
    app.state.config = _StubConfig()
    app.state.model_info = mi_service
    app.state.catalog = _EmptyCatalogService()
    app.state.stats_db = None
    app.state.dashboard_db = None
    request = _stub_request(app.state)
    with caplog.at_level(logging.WARNING, logger="eggpool.dashboard.routes"):
        response = await handle_models(request)
    assert response.status_code == 200
    assert any(
        "Dashboard catalog returned no rows" in record.message
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Phase H.4 — Join diagnostics trigger on all-miss state
# ---------------------------------------------------------------------------


def test_compute_join_stats_counts_matches_and_samples_unmatched() -> None:
    rows = [
        {
            "model_id": "minimax-m3/opencode-go",
            "base_model_id": "minimax-m3",
            "_model_info_lookup_id": "minimax-m3",
            "provider_id": "opencode-go",
        },
        {
            "model_id": "gpt-4o",
            "base_model_id": "gpt-4o",
            "_model_info_lookup_id": "gpt-4o",
            "provider_id": "openai",
        },
    ]
    summary_map = {"minimax-m3": {"status": "fresh"}}
    matched, sample = _compute_model_info_join_stats(rows, summary_map)
    assert matched == 1
    assert len(sample) == 1
    assert sample[0]["model_id"] == "gpt-4o"
    assert sample[0]["lookup_id"] == "gpt-4o"


def test_render_models_join_failure_warning_on_all_miss() -> None:
    rows = [
        {
            "model_id": "minimax-m3/opencode-go",
            "base_model_id": "minimax-m3",
            "_model_info_lookup_id": "minimax-m3",
            "provider_id": "opencode-go",
            "request_count": 0,
        },
    ]
    summary_map = {
        "minimax-m3": {
            "model_id": "minimax-m3",
            "status": "partial",
            "sparse": False,
            "summary": "Canonical summary exists.",
            "sources": ["openrouter"],
            "last_refreshed_at": "2026-07-04T15:42:40Z",
        },
    }
    state = ModelInfoDashboardState(
        summaries=summary_map,
        available=True,
        summary_count=1,
        matched_row_count=0,
        unmatched_row_count=1,
        unmatched_sample=(
            {
                "model_id": "minimax-m3/opencode-go",
                "base_model_id": "minimax-m3",
                "lookup_id": "minimax-m3",
                "provider_id": "opencode-go",
            },
        ),
    )
    html = render_models(
        models=rows,
        period="24h",
        model_info_map=summary_map,
        model_info_state=state,
    )
    assert "Model info is loaded but did not match" in html
    assert "minimax-m3/opencode-go" in html


def test_render_models_no_join_warning_when_summary_map_empty() -> None:
    """Empty model-info state is not a join-failure — surface nothing."""
    rows = [
        {
            "model_id": "minimax-m3/opencode-go",
            "base_model_id": "minimax-m3",
            "_model_info_lookup_id": "minimax-m3",
            "provider_id": "opencode-go",
            "request_count": 0,
        },
    ]
    state = ModelInfoDashboardState(
        summaries={},
        available=True,
        summary_count=0,
    )
    html = render_models(
        models=rows,
        period="24h",
        model_info_map={},
        model_info_state=state,
    )
    assert "Model info is loaded but did not match" not in html
    assert "did not match" not in html.lower()
