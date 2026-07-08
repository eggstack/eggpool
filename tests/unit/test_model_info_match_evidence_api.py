"""Tests for model-info match evidence API endpoints (Phase 4 closure)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.api.model_info import (
    handle_model_info_aliases,
    handle_model_info_detail,
    handle_model_info_matches,
)


class TestDetailMatchEvidence:
    @pytest.mark.asyncio()
    async def test_detail_response_includes_compact_match_evidence(self) -> None:
        info = MagicMock()
        info.model_id = "claude-3-opus"
        info.status = "fresh"
        info.sparse = False
        info.summary = "All good."
        info.provenance = {"sources": ["provider_catalog"]}
        info.detail = {"providers": ["anthropic"], "context_tokens": 200000}
        info.last_seen_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.last_refreshed_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.next_refresh_at = None
        info.conflicts = {}

        mock_service = AsyncMock()
        mock_service.get_summary.return_value = info
        mock_service.repo.list_compact_observations_for_model.return_value = []
        mock_service.repo.list_match_evidence.return_value = [
            {
                "id": 1,
                "model_id": "claude-3-opus",
                "provider_id": "anthropic",
                "source": "openrouter",
                "alias": "claude-3-opus-20240229",
                "match_method": "normalized_exact",
                "confidence": 0.95,
                "diagnostics_json": '{"ignored": true}',
                "created_at": "2026-06-29T20:00:00Z",
                "last_seen_at": "2026-07-01T12:00:00Z",
            }
        ]

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_detail(request, "claude-3-opus")
        data = json.loads(response.body)

        assert "match_evidence" in data
        assert len(data["match_evidence"]) == 1
        entry = data["match_evidence"][0]
        assert entry["source"] == "openrouter"
        assert entry["alias"] == "claude-3-opus-20240229"
        assert entry["match_method"] == "normalized_exact"
        assert entry["confidence"] == 0.95
        assert entry["provider_id"] == "anthropic"
        assert entry["last_seen_at"] == "2026-07-01T12:00:00Z"

    @pytest.mark.asyncio()
    async def test_detail_response_omits_raw_match_diagnostics_by_default(
        self,
    ) -> None:
        info = MagicMock()
        info.model_id = "claude-3-opus"
        info.status = "fresh"
        info.sparse = False
        info.summary = "All good."
        info.provenance = {"sources": ["provider_catalog"]}
        info.detail = {"providers": ["anthropic"], "context_tokens": 200000}
        info.last_seen_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.last_refreshed_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.next_refresh_at = None
        info.conflicts = {}

        mock_service = AsyncMock()
        mock_service.get_summary.return_value = info
        mock_service.repo.list_compact_observations_for_model.return_value = []
        mock_service.repo.list_match_evidence.return_value = [
            {
                "id": 1,
                "model_id": "claude-3-opus",
                "provider_id": "anthropic",
                "source": "openrouter",
                "alias": "claude-3-opus-20240229",
                "match_method": "normalized_exact",
                "confidence": 0.95,
                "diagnostics_json": '{"full": "diagnostics", "data": true}',
                "created_at": "2026-06-29T20:00:00Z",
                "last_seen_at": "2026-07-01T12:00:00Z",
            }
        ]

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_detail(request, "claude-3-opus")
        data = json.loads(response.body)

        assert "match_evidence" in data
        for entry in data["match_evidence"]:
            assert "diagnostics_json" not in entry

    @pytest.mark.asyncio()
    async def test_match_evidence_empty_when_no_evidence_rows(self) -> None:
        info = MagicMock()
        info.model_id = "no-evidence-model"
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
        mock_service.get_summary.return_value = info
        mock_service.repo.list_compact_observations_for_model.return_value = []
        mock_service.repo.list_match_evidence.return_value = []

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_detail(request, "no-evidence-model")
        data = json.loads(response.body)

        assert "match_evidence" not in data


class TestMatchesEndpoint:
    @pytest.mark.asyncio()
    async def test_matches_endpoint_returns_evidence(self) -> None:
        mock_service = AsyncMock()
        mock_service.repo.list_match_evidence.return_value = [
            {
                "id": 1,
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "source": "openrouter",
                "alias": "gpt-4o-2024-05-13",
                "match_method": "regex_rule",
                "confidence": 0.85,
                "diagnostics_json": None,
                "created_at": "2026-06-29T20:00:00Z",
                "last_seen_at": "2026-07-01T12:00:00Z",
            },
            {
                "id": 2,
                "model_id": "gpt-4o",
                "provider_id": "openai",
                "source": "huggingface",
                "alias": "gpt-4o-base",
                "match_method": "normalized_exact",
                "confidence": 1.0,
                "diagnostics_json": '{"tier": 2}',
                "created_at": "2026-06-30T10:00:00Z",
                "last_seen_at": "2026-07-02T08:00:00Z",
            },
        ]

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_matches(request, "gpt-4o")
        data = json.loads(response.body)

        assert data["model_id"] == "gpt-4o"
        assert len(data["match_evidence"]) == 2
        assert data["match_evidence"][0]["source"] == "openrouter"
        assert data["match_evidence"][0]["match_method"] == "regex_rule"
        assert data["match_evidence"][1]["source"] == "huggingface"
        assert data["match_evidence"][1]["confidence"] == 1.0

    @pytest.mark.asyncio()
    async def test_matches_endpoint_omits_diagnostics_json(self) -> None:
        mock_service = AsyncMock()
        mock_service.repo.list_match_evidence.return_value = [
            {
                "id": 1,
                "model_id": "test-model",
                "provider_id": None,
                "source": "openrouter",
                "alias": "test-alias",
                "match_method": "similarity_guarded",
                "confidence": 0.7,
                "diagnostics_json": '{"raw": "sensitive data"}',
                "created_at": "2026-06-29T20:00:00Z",
                "last_seen_at": "2026-07-01T12:00:00Z",
            }
        ]

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_matches(request, "test-model")
        data = json.loads(response.body)

        entry = data["match_evidence"][0]
        assert "diagnostics_json" not in entry

    @pytest.mark.asyncio()
    async def test_matches_endpoint_returns_empty_when_no_evidence(self) -> None:
        mock_service = AsyncMock()
        mock_service.repo.list_match_evidence.return_value = []

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_matches(request, "unknown-model")
        data = json.loads(response.body)

        assert data["model_id"] == "unknown-model"
        assert data["match_evidence"] == []

    @pytest.mark.asyncio()
    async def test_matches_endpoint_503_when_model_info_disabled(self) -> None:
        request = MagicMock()
        request.app.state.model_info = None

        response = await handle_model_info_matches(request, "any-model")
        assert response.status_code == 503

    @pytest.mark.asyncio()
    async def test_matches_endpoint_500_on_repo_error(self) -> None:
        mock_service = AsyncMock()
        mock_service.repo.list_match_evidence.side_effect = RuntimeError("db down")

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_matches(request, "error-model")
        assert response.status_code == 500
        data = json.loads(response.body)
        assert data["error"] == "RuntimeError"

    @pytest.mark.asyncio()
    async def test_detail_includes_evidence_when_repo_raises(self) -> None:
        info = MagicMock()
        info.model_id = "partial-model"
        info.status = "fresh"
        info.sparse = False
        info.summary = "Ok."
        info.provenance = {"sources": ["provider_catalog"]}
        info.detail = {"providers": ["openai"]}
        info.last_seen_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.last_refreshed_at = datetime(2026, 6, 29, 20, 0, tzinfo=UTC)
        info.next_refresh_at = None
        info.conflicts = {}

        mock_service = AsyncMock()
        mock_service.get_summary.return_value = info
        mock_service.repo.list_compact_observations_for_model.return_value = []
        mock_service.repo.list_match_evidence.side_effect = RuntimeError("db error")

        request = MagicMock()
        request.app.state.model_info = mock_service

        response = await handle_model_info_detail(request, "partial-model")
        data = json.loads(response.body)

        assert data["model_id"] == "partial-model"
        assert "match_evidence" not in data


class TestProviderSuffixResolution:
    """Verify that aliases/matches resolve provider-suffixed IDs
    through the same canonical lookup as the detail endpoint."""

    @pytest.mark.asyncio()
    async def test_aliases_endpoint_resolves_provider_suffixed_id_to_canonical(
        self,
    ) -> None:
        mock_service = AsyncMock()
        mock_service.repo.get_aliases_for_model.return_value = ["minimax-m3"]
        mock_service.repo.list_alias_rows_for_model.return_value = []

        request = MagicMock()
        request.app.state.model_info = mock_service
        request.app.state.config = MagicMock(providers={"opencode-go": ...})

        response = await handle_model_info_aliases(request, "minimax-m3%2Fopencode-go")
        data = json.loads(response.body)

        assert data["model_id"] == "minimax-m3"
        assert data["requested_model_id"] == "minimax-m3/opencode-go"
        assert data["provider_suffix"] == "opencode-go"
        mock_service.repo.get_aliases_for_model.assert_awaited_once_with("minimax-m3")

    @pytest.mark.asyncio()
    async def test_matches_endpoint_resolves_provider_suffixed_id_to_canonical(
        self,
    ) -> None:
        mock_service = AsyncMock()
        mock_service.repo.list_match_evidence.return_value = [
            {
                "id": 1,
                "model_id": "minimax-m3",
                "provider_id": None,
                "source": "openrouter",
                "alias": "minimax/minimax-m3",
                "match_method": "normalized_exact",
                "confidence": 0.85,
                "diagnostics_json": None,
                "created_at": "2026-06-29T20:00:00Z",
                "last_seen_at": "2026-07-01T12:00:00Z",
            }
        ]

        request = MagicMock()
        request.app.state.model_info = mock_service
        request.app.state.config = MagicMock(providers={"opencode-go": ...})

        response = await handle_model_info_matches(request, "minimax-m3%2Fopencode-go")
        data = json.loads(response.body)

        assert data["model_id"] == "minimax-m3"
        assert data["requested_model_id"] == "minimax-m3/opencode-go"
        assert data["provider_suffix"] == "opencode-go"
        assert len(data["match_evidence"]) == 1
        mock_service.repo.list_match_evidence.assert_awaited_once_with(
            "minimax-m3", source=None
        )

    @pytest.mark.asyncio()
    async def test_aliases_and_matches_unsuffixed_ids_remain_unchanged(
        self,
    ) -> None:
        mock_service = AsyncMock()
        mock_service.repo.get_aliases_for_model.return_value = ["minimax-m3"]
        mock_service.repo.list_alias_rows_for_model.return_value = []
        mock_service.repo.list_match_evidence.return_value = []

        request = MagicMock()
        request.app.state.model_info = mock_service
        request.app.state.config = MagicMock(providers={"opencode-go": ...})

        aliases_resp = await handle_model_info_aliases(request, "minimax-m3")
        aliases_data = json.loads(aliases_resp.body)
        assert aliases_data["model_id"] == "minimax-m3"
        assert aliases_data["requested_model_id"] == "minimax-m3"
        assert aliases_data["provider_suffix"] is None

        matches_resp = await handle_model_info_matches(request, "minimax-m3")
        matches_data = json.loads(matches_resp.body)
        assert matches_data["model_id"] == "minimax-m3"
        assert matches_data["requested_model_id"] == "minimax-m3"
        assert matches_data["provider_suffix"] is None
