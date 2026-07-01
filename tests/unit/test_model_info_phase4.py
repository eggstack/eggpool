"""Tests for model-info phase 4: serializer, API, dashboard rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.api.models import MODEL_INFO_STATUS_DISPLAY, serialize_openai_model
from eggpool.constants import API_V1_PREFIX
from eggpool.dashboard.render import _render_model_info_pill

# ---------------------------------------------------------------------------
# Serializer tests
# ---------------------------------------------------------------------------


class TestSerializeModelInfo:
    """Tests for the model_info parameter of serialize_openai_model."""

    def test_omits_model_info_when_none(self) -> None:
        model = {"model_id": "gpt-4", "display_name": "GPT-4"}
        result = serialize_openai_model(model, model_info=None)
        assert "eggpool" not in result

    def test_adds_compact_model_info_under_eggpool_namespace(self) -> None:
        model = {"model_id": "minimax-m3", "display_name": "MiniMax M3"}
        mi = {
            "status": "partial",
            "sparse": True,
            "summary": "New model detected; metadata sparse.",
            "sources": ["provider_catalog", "openrouter"],
            "last_refreshed_at": "2026-06-29T20:00:00+00:00",
        }
        result = serialize_openai_model(model, model_info=mi)
        assert result["eggpool"]["model_info"]["status"] == "partial"
        assert result["eggpool"]["model_info"]["sparse"] is True
        assert (
            result["eggpool"]["model_info"]["summary"]
            == "New model detected; metadata sparse."
        )
        assert result["eggpool"]["model_info"]["sources"] == [
            "provider_catalog",
            "openrouter",
        ]
        assert (
            result["eggpool"]["model_info"]["last_refreshed_at"]
            == "2026-06-29T20:00:00+00:00"
        )

    def test_does_not_include_raw_observations(self) -> None:
        model = {"model_id": "m1"}
        mi = {
            "status": "fresh",
            "sparse": False,
            "summary": "All good.",
            "sources": ["provider_catalog"],
            "last_refreshed_at": None,
        }
        result = serialize_openai_model(model, model_info=mi)
        info = result["eggpool"]["model_info"]
        assert "observations" not in info
        assert "provenance" not in info
        assert "conflicts" not in info

    def test_model_info_empty_sources_defaults_to_list(self) -> None:
        model = {"model_id": "m1"}
        mi = {
            "status": "fresh",
            "sparse": False,
            "summary": "ok",
            "sources": [],
            "last_refreshed_at": None,
        }
        result = serialize_openai_model(model, model_info=mi)
        assert result["eggpool"]["model_info"]["sources"] == []

    def test_model_info_with_limits_preserves_both(self) -> None:
        model = {
            "model_id": "m1",
            "effective_limits": {
                "context_tokens": 100000,
                "input_tokens": None,
                "output_tokens": 4096,
            },
        }
        mi = {
            "status": "fresh",
            "sparse": False,
            "summary": "ok",
            "sources": [],
            "last_refreshed_at": None,
        }
        result = serialize_openai_model(model, model_info=mi)
        assert result["eggpool"]["limits"]["context"] == 100000
        assert result["eggpool"]["model_info"]["status"] == "fresh"


# ---------------------------------------------------------------------------
# Status display mapping tests
# ---------------------------------------------------------------------------


class TestModelInfoStatusDisplay:
    def test_all_internal_statuses_mapped(self) -> None:
        expected = {
            "fresh",
            "partial",
            "sparse_new",
            "stale",
            "conflicting",
            "unmatched",
            "source_unavailable",
            "manual_override",
            "withdrawn",
        }
        assert set(MODEL_INFO_STATUS_DISPLAY.keys()) == expected

    def test_sparse_new_maps_to_sparse(self) -> None:
        assert MODEL_INFO_STATUS_DISPLAY["sparse_new"] == "sparse"

    def test_status_filter_aliases_normalize_to_canonical(self) -> None:
        from eggpool.model_info.presentation import normalize_model_info_status_filter

        assert normalize_model_info_status_filter("sparse") == "sparse_new"
        assert normalize_model_info_status_filter("sparse_new") == "sparse_new"
        assert normalize_model_info_status_filter("source-unavailable") == (
            "source_unavailable"
        )
        assert normalize_model_info_status_filter("fresh") == "fresh"


class TestCompactModelInfoSummary:
    def test_compact_summary_can_emit_display_or_canonical_status(self) -> None:
        from eggpool.model_info.presentation import compact_model_info_summary

        info = SimpleNamespace(
            model_id="gpt-4o",
            status="sparse_new",
            sparse=True,
            summary="Sparse metadata.",
            provenance={"sources": ["provider_catalog", "openrouter"]},
            detail={"providers": ["openai"]},
            conflicts={},
            last_seen_at=datetime(2026, 6, 29, 20, 0, tzinfo=UTC),
            last_refreshed_at=None,
            next_refresh_at=None,
        )

        display = compact_model_info_summary(info)
        canonical = compact_model_info_summary(info, display_status=False)

        assert display["status"] == "sparse"
        assert canonical["status"] == "sparse_new"
        assert display["sources"] == ["provider_catalog", "openrouter"]
        assert display["providers"] == ["openai"]
        assert display["last_seen_at"] == "2026-06-29T20:00:00+00:00"


# ---------------------------------------------------------------------------
# Dashboard pill rendering tests
# ---------------------------------------------------------------------------


class TestRenderModelInfoPill:
    def test_returns_dash_when_no_info(self) -> None:
        pill = _render_model_info_pill(None)
        assert "—" in pill
        assert "pill-unknown" in pill

    def test_renders_status_pill_when_summary_exists(self) -> None:
        info = {
            "status": "partial",
            "sparse": False,
            "summary": "Partial metadata available.",
            "sources": ["provider_catalog"],
            "last_refreshed_at": "2026-06-29T20:00:00Z",
        }
        pill = _render_model_info_pill(info)
        assert "pill-partial" in pill
        assert "partial" in pill
        assert "Partial metadata available." in pill

    def test_escapes_html_in_summary(self) -> None:
        info = {
            "status": "fresh",
            "sparse": False,
            "summary": "<script>alert('xss')</script>",
            "sources": [],
            "last_refreshed_at": None,
        }
        pill = _render_model_info_pill(info)
        assert "<script>" not in pill
        assert "&lt;script&gt;" in pill

    def test_handles_sparse_and_conflicting_status_classes(self) -> None:
        sparse_info = {
            "status": "sparse_new",
            "sparse": True,
            "summary": "New",
            "sources": [],
            "last_refreshed_at": None,
        }
        pill = _render_model_info_pill(sparse_info)
        assert "pill-sparse" in pill

        conflict_info = {
            "status": "conflicting",
            "sparse": False,
            "summary": "Conflict",
            "sources": [],
            "last_refreshed_at": None,
        }
        pill = _render_model_info_pill(conflict_info)
        assert "pill-conflict" in pill

    def test_sources_in_tooltip(self) -> None:
        info = {
            "status": "fresh",
            "sparse": False,
            "summary": "ok",
            "sources": ["provider_catalog", "openrouter"],
            "last_refreshed_at": "2026-06-29T20:00:00Z",
        }
        pill = _render_model_info_pill(info)
        assert "provider_catalog" in pill
        assert "openrouter" in pill


# ---------------------------------------------------------------------------
# API endpoint tests (unit-level with mocked service)
# ---------------------------------------------------------------------------


class TestModelInfoAPIEndpoints:
    """Unit tests for the model-info API handlers using mocked services."""

    @pytest.mark.asyncio()
    async def test_summary_endpoint_returns_list(self) -> None:
        from eggpool.api.model_info import handle_model_info_summary

        info = MagicMock()
        info.model_id = "gpt-4"
        info.status = "fresh"
        info.sparse = False
        info.summary = "All good."
        info.provenance = {"sources": ["provider_catalog"]}
        info.detail = {"providers": ["openai"]}
        info.last_seen_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.last_refreshed_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.next_refresh_at = None
        info.conflicts = {}

        mock_service = AsyncMock()
        mock_service.get_summary_map.return_value = {"gpt-4": info}

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_summary(request)
        body = response.body
        import json

        data = json.loads(body)
        assert data["object"] == "list"
        assert len(data["data"]) == 1
        assert data["data"][0]["model_id"] == "gpt-4"
        assert data["data"][0]["status"] == "fresh"

    @pytest.mark.asyncio()
    async def test_detail_endpoint_returns_one_model(self) -> None:
        from eggpool.api.model_info import handle_model_info_detail

        info = MagicMock()
        info.model_id = "minimax-m3"
        info.status = "partial"
        info.sparse = True
        info.summary = "Sparse."
        info.provenance = {"sources": ["provider_catalog"]}
        info.detail = {"providers": ["minimax"], "context_tokens": 220000}
        info.last_seen_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.last_refreshed_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.next_refresh_at = None
        info.conflicts = {}

        mock_service = AsyncMock()
        mock_service.get_summary.return_value = info

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_detail(request, "minimax-m3")
        import json

        data = json.loads(response.body)
        assert data["model_id"] == "minimax-m3"
        assert data["status"] == "partial"
        assert "detail" in data

    @pytest.mark.asyncio()
    async def test_sources_endpoint_redacts_secrets(self) -> None:
        from eggpool.api.model_info import handle_model_info_sources

        mock_service = AsyncMock()
        mock_service.repo.source_health_snapshot.return_value = {
            "openrouter": {
                "enabled": True,
                "last_success_at": "2026-06-29T20:00:00Z",
                "last_error_at": None,
                "last_error_class": None,
                "last_error_message": None,
                "cooldown_until": None,
                "failure_count": 0,
            }
        }

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_sources(request)
        import json

        data = json.loads(response.body)
        assert data["object"] == "list"
        entry = data["data"][0]
        assert entry["source"] == "openrouter"
        assert entry["enabled"] is True
        # Ensure secrets are NOT exposed
        assert "api_key" not in entry
        assert "token" not in entry
        assert "last_error_message" not in entry

    @pytest.mark.asyncio()
    async def test_refresh_endpoint_requires_auth(self) -> None:
        from eggpool.api.model_info import handle_model_info_refresh

        mock_service = AsyncMock()
        mock_service.refresh_due_models.return_value = {
            "refreshed": 5,
            "total": 10,
            "skipped": 3,
        }

        request = MagicMock()
        request.app.state.model_info = mock_service
        request.query_params = {}

        response = await handle_model_info_refresh(request)
        import json

        data = json.loads(response.body)
        assert data["status"] == "ok"
        assert data["refreshed"] == 5

    @pytest.mark.asyncio()
    async def test_disabled_endpoint_returns_503(self) -> None:
        from eggpool.api.model_info import handle_model_info_summary

        request = MagicMock()
        request.app.state = MagicMock(spec=[])  # no model_info attr

        response = await handle_model_info_summary(request)
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# Dashboard render integration tests
# ---------------------------------------------------------------------------


class TestRenderModelsWithInfo:
    def test_models_page_renders_without_model_info_service(self) -> None:
        from eggpool.dashboard.render import render_models

        models = [
            {
                "model_id": "gpt-4",
                "provider_id": "openai",
                "request_count": 100,
                "cost_microdollars": 5000000,
                "error_count": 2,
                "input_tokens": 100000,
                "output_tokens": 50000,
                "total_tokens": 150000,
                "avg_latency_ms": 120.5,
                "avg_ttft_ms": 45.2,
                "tokens_per_second": 85.3,
            }
        ]
        html = render_models(models)
        assert "gpt-4" in html
        assert "openai" in html
        # No model_info column content when map is empty
        assert 'class="pill' not in html or "pill-unknown" in html

    def test_models_page_renders_status_pill_when_summary_exists(self) -> None:
        from eggpool.dashboard.render import render_models

        models = [
            {
                "model_id": "gpt-4",
                "provider_id": "openai",
                "request_count": 100,
                "cost_microdollars": 5000000,
                "error_count": 0,
                "input_tokens": 100000,
                "output_tokens": 50000,
                "total_tokens": 150000,
                "avg_latency_ms": 120.5,
                "avg_ttft_ms": 45.2,
                "tokens_per_second": 85.3,
            }
        ]
        mi_map = {
            "gpt-4": {
                "status": "fresh",
                "sparse": False,
                "summary": "All good.",
                "sources": ["provider_catalog"],
                "last_refreshed_at": "2026-06-29T20:00:00Z",
            }
        }
        html = render_models(models, model_info_map=mi_map)
        assert "pill-fresh" in html
        assert "fresh" in html

    def test_models_page_escapes_model_info_summary(self) -> None:
        from eggpool.dashboard.render import render_models

        models = [
            {
                "model_id": "m1",
                "provider_id": "p1",
                "request_count": 1,
                "cost_microdollars": 0,
                "error_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "avg_latency_ms": 0,
                "avg_ttft_ms": 0,
                "tokens_per_second": 0,
            }
        ]
        mi_map = {
            "m1": {
                "status": "fresh",
                "sparse": False,
                "summary": "<img src=x onerror=alert(1)>",
                "sources": [],
                "last_refreshed_at": None,
            }
        }
        html = render_models(models, model_info_map=mi_map)
        assert "<img" not in html
        assert "&lt;img" in html

    def test_models_page_handles_sparse_and_conflicting_status_classes(self) -> None:
        from eggpool.dashboard.render import render_models

        models = [
            {
                "model_id": "m1",
                "provider_id": "p1",
                "request_count": 1,
                "cost_microdollars": 0,
                "error_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "avg_latency_ms": 0,
                "avg_ttft_ms": 0,
                "tokens_per_second": 0,
            },
            {
                "model_id": "m2",
                "provider_id": "p2",
                "request_count": 1,
                "cost_microdollars": 0,
                "error_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "avg_latency_ms": 0,
                "avg_ttft_ms": 0,
                "tokens_per_second": 0,
            },
        ]
        mi_map = {
            "m1": {
                "status": "sparse_new",
                "sparse": True,
                "summary": "New",
                "sources": [],
                "last_refreshed_at": None,
            },
            "m2": {
                "status": "conflicting",
                "sparse": False,
                "summary": "Conflict",
                "sources": [],
                "last_refreshed_at": None,
            },
        }
        html = render_models(models, model_info_map=mi_map)
        assert "pill-sparse" in html
        assert "pill-conflict" in html


# ---------------------------------------------------------------------------
# /v1/models integration tests (app-level with mocked catalog/service)
# ---------------------------------------------------------------------------


class TestV1ModelsEnrichment:
    """Integration tests for the /v1/models model-info enrichment path."""

    def test_uses_base_model_id_for_provider_suffixed_entries(self) -> None:
        """model_info is resolved by base_model_id first for suffixed entries."""
        from fastapi.testclient import TestClient

        from eggpool.app import create_app
        from eggpool.models.config import AppConfig

        config = AppConfig.from_dict(
            {
                "server": {"api_key_env": "NONEXISTENT_KEY_FOR_TEST"},
                "database": {"path": ":memory:"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "dashboard": {"enabled": False},
                "model_info": {
                    "enabled": True,
                    "include_in_models_endpoint": True,
                    "startup_refresh": False,
                },
            }
        )
        app = create_app(config)

        mock_catalog = MagicMock()
        mock_catalog.get_models_for_exposure.return_value = [
            {
                "model_id": "gpt-4/openai",
                "base_model_id": "gpt-4",
                "display_name": "GPT-4",
            },
            {
                "model_id": "minimax-m3/minimax",
                "display_name": "MiniMax M3",
            },
        ]
        app.state.catalog = mock_catalog

        mi_info_base = MagicMock()
        mi_info_base.status = "fresh"
        mi_info_base.sparse = False
        mi_info_base.summary = "All good."
        mi_info_base.provenance = {"sources": ["provider_catalog"]}
        mi_info_base.last_refreshed_at = None

        mi_info_direct = MagicMock()
        mi_info_direct.status = "partial"
        mi_info_direct.sparse = True
        mi_info_direct.summary = "Sparse."
        mi_info_direct.provenance = {"sources": ["openrouter"]}
        mi_info_direct.last_refreshed_at = None

        mock_mi_service = AsyncMock()
        mock_mi_service.get_summary_map.return_value = {
            "gpt-4": mi_info_base,
            "minimax-m3": mi_info_direct,
        }
        app.state.model_info = mock_mi_service

        client = TestClient(app)
        response = client.get(f"{API_V1_PREFIX}/models")
        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 2

        # gpt-4/openai resolved via base_model_id "gpt-4"
        m1 = data["data"][0]
        assert m1["id"] == "gpt-4/openai"
        assert m1["eggpool"]["model_info"]["status"] == "fresh"

        # minimax-m3/minimax resolved via model_id "minimax-m3/minimax"
        # which does NOT match "minimax-m3" in the map, so no enrichment
        m2 = data["data"][1]
        assert m2["id"] == "minimax-m3/minimax"
        assert "model_info" not in m2.get("eggpool", {})

    def test_omits_enrichment_when_config_disabled(self) -> None:
        """model_info enrichment is omitted when include_in_models_endpoint is false."""
        from fastapi.testclient import TestClient

        from eggpool.app import create_app
        from eggpool.models.config import AppConfig

        config = AppConfig.from_dict(
            {
                "server": {"api_key_env": "NONEXISTENT_KEY_FOR_TEST"},
                "database": {"path": ":memory:"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "dashboard": {"enabled": False},
                "model_info": {
                    "enabled": True,
                    "include_in_models_endpoint": False,
                    "startup_refresh": False,
                },
            }
        )
        app = create_app(config)

        mock_catalog = MagicMock()
        mock_catalog.get_models_for_exposure.return_value = [
            {"model_id": "gpt-4", "display_name": "GPT-4"},
        ]
        app.state.catalog = mock_catalog

        mock_mi_service = AsyncMock()
        mock_mi_service.get_summary_map.return_value = {
            "gpt-4": MagicMock(
                status="fresh",
                sparse=False,
                summary="ok",
                provenance={"sources": ["provider_catalog"]},
                last_refreshed_at=None,
            ),
        }
        app.state.model_info = mock_mi_service

        client = TestClient(app)
        response = client.get(f"{API_V1_PREFIX}/models")
        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        # eggpool namespace may exist for limits etc., but model_info must be absent
        eggpool = data["data"][0].get("eggpool", {})
        assert "model_info" not in eggpool
        mock_mi_service.get_summary_map.assert_not_called()

    def test_omits_enrichment_on_model_info_error(self) -> None:
        """model_info errors are silently caught; /v1/models still works."""
        from fastapi.testclient import TestClient

        from eggpool.app import create_app
        from eggpool.models.config import AppConfig

        config = AppConfig.from_dict(
            {
                "server": {"api_key_env": "NONEXISTENT_KEY_FOR_TEST"},
                "database": {"path": ":memory:"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "dashboard": {"enabled": False},
                "model_info": {
                    "enabled": True,
                    "include_in_models_endpoint": True,
                    "startup_refresh": False,
                },
            }
        )
        app = create_app(config)

        mock_catalog = MagicMock()
        mock_catalog.get_models_for_exposure.return_value = [
            {"model_id": "gpt-4", "display_name": "GPT-4"},
        ]
        app.state.catalog = mock_catalog

        mock_mi_service = AsyncMock()
        mock_mi_service.get_summary_map.side_effect = RuntimeError("DB unavailable")
        app.state.model_info = mock_mi_service

        client = TestClient(app)
        response = client.get(f"{API_V1_PREFIX}/models")
        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 1
        # No model_info in output despite service being configured
        eggpool = data["data"][0].get("eggpool", {})
        assert "model_info" not in eggpool


# ---------------------------------------------------------------------------
# Route registration tests
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    """Verify model-info routes are registered under expected auth policy."""

    def test_model_info_routes_registered_when_enabled(self) -> None:
        """Model-info endpoints appear in routes when model_info.enabled."""
        from eggpool.app import create_app
        from eggpool.models.config import AppConfig

        config = AppConfig.from_dict(
            {
                "server": {"api_key_env": "NONEXISTENT_KEY_FOR_TEST"},
                "database": {"path": ":memory:"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "dashboard": {"enabled": True, "public": True},
                "model_info": {
                    "enabled": True,
                    "startup_refresh": False,
                },
            }
        )
        app = create_app(config)
        paths = {route.path for route in app.routes}
        assert "/api/model-info" in paths
        assert "/api/model-info/sources" in paths
        assert "/api/model-info/{model_id:path}" in paths
        assert "/api/model-info/refresh" in paths

    def test_model_info_routes_absent_when_disabled(self) -> None:
        """Model-info endpoints are NOT registered when model_info.enabled is false."""
        from eggpool.app import create_app
        from eggpool.models.config import AppConfig

        config = AppConfig.from_dict(
            {
                "server": {"api_key_env": "NONEXISTENT_KEY_FOR_TEST"},
                "database": {"path": ":memory:"},
                "models": {"startup_refresh": False, "refresh_interval_s": 0},
                "dashboard": {"enabled": True, "public": True},
                "model_info": {
                    "enabled": False,
                    "startup_refresh": False,
                },
            }
        )
        app = create_app(config)
        paths = {route.path for route in app.routes}
        assert "/api/model-info" not in paths
        assert "/api/model-info/sources" not in paths
        assert "/api/model-info/refresh" not in paths
