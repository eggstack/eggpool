"""Tests for model-info FastAPI route registration order.

Pins the contract that all specific suffix routes (``/matches`` and
``/aliases``) are registered before the greedy detail route
(``/{model_id:path}``).  Without this ordering, FastAPI's path matcher
captures ``<model_id>/aliases`` or ``<model_id>/matches`` as the
``model_id`` parameter and dispatches to the detail handler instead of
the suffix-specific one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from eggpool.api.model_info import register_model_info_routes


def _make_app() -> tuple[FastAPI, AsyncMock]:
    """Build a FastAPI app with model-info routes wired to a mock service."""
    app = FastAPI()
    mock_service = AsyncMock()
    info = MagicMock()
    info.model_id = "minimax-m3"
    info.status = "fresh"
    info.sparse = False
    info.summary = "ok"
    info.provenance = {"sources": ["provider_catalog"]}
    info.detail = {"providers": ["openai"]}
    info.last_seen_at = datetime(2026, 7, 1, tzinfo=UTC)
    info.last_refreshed_at = datetime(2026, 7, 1, tzinfo=UTC)
    info.next_refresh_at = None
    info.conflicts = {}
    mock_service.get_summary.return_value = info
    mock_service.repo.list_compact_observations_for_model.return_value = []
    mock_service.repo.list_match_evidence.return_value = []
    mock_service.repo.get_aliases_for_model.return_value = ["minimax-m3"]
    mock_service.repo.list_alias_rows_for_model.return_value = []
    app.state.model_info = mock_service
    register_model_info_routes(app, require_auth=False)
    return app, mock_service


class TestRouteRegistrationOrder:
    """Pin the route registration order so greedy routes never shadow suffixes."""

    def test_aliases_route_registered_before_detail_route(self) -> None:
        app, _ = _make_app()
        paths = [getattr(route, "path", "") for route in app.routes]
        aliases_idx = paths.index("/api/model-info/{model_id:path}/aliases")
        detail_idx = paths.index("/api/model-info/{model_id:path}")
        assert aliases_idx < detail_idx, (
            f"/aliases (idx {aliases_idx}) must be registered before greedy "
            f"detail (idx {detail_idx}); got order: {paths}"
        )

    def test_matches_route_registered_before_detail_route(self) -> None:
        app, _ = _make_app()
        paths = [getattr(route, "path", "") for route in app.routes]
        matches_idx = paths.index("/api/model-info/{model_id:path}/matches")
        detail_idx = paths.index("/api/model-info/{model_id:path}")
        assert matches_idx < detail_idx, (
            f"/matches (idx {matches_idx}) must be registered before greedy "
            f"detail (idx {detail_idx}); got order: {paths}"
        )


class TestRouteDispatch:
    """Functional dispatch tests via TestClient."""

    def test_get_model_id_aliases_dispatches_to_aliases_handler(self) -> None:
        app, mock_service = _make_app()
        with TestClient(app) as client:
            response = client.get("/api/model-info/minimax-m3/aliases")
        assert response.status_code == 200
        payload = response.json()
        assert payload["model_id"] == "minimax-m3"
        # Aliases handler called — not detail.
        mock_service.repo.get_aliases_for_model.assert_awaited_once_with("minimax-m3")

    def test_get_model_id_matches_dispatches_to_matches_handler(self) -> None:
        app, mock_service = _make_app()
        with TestClient(app) as client:
            response = client.get("/api/model-info/minimax-m3/matches")
        assert response.status_code == 200
        payload = response.json()
        assert payload["model_id"] == "minimax-m3"
        # Matches handler called.
        mock_service.repo.list_match_evidence.assert_awaited_once()

    def test_get_model_id_detail_still_dispatches_to_detail_handler(self) -> None:
        app, mock_service = _make_app()
        with TestClient(app) as client:
            response = client.get("/api/model-info/minimax-m3")
        assert response.status_code == 200
        # Detail handler called (uses ``get_summary``).
        mock_service.get_summary.assert_awaited_once()
