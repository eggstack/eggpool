from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.service import ModelInfoService
from eggpool.model_info.types import CanonicalModelInfo
from eggpool.models.config import (
    ModelInfoConfig,
    ModelInfoSourceConfig,
    ModelInfoSourcesConfig,
)


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(db: Database, model_id: str) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, model_id),
        )


def _make_cache(model_id: str, provider_id: str = "opencode-go") -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    cache._models[model_id] = {
        "model_id": model_id,
        "display_name": model_id,
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
    cache._provider_models[(model_id, provider_id)] = dict(cache._models[model_id])
    return cache


def _openrouter_payload(*models: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(models)}


class _MockHttpClient:
    def __init__(self, response: dict | Exception | None = None) -> None:
        self._response = response
        self.call_count = 0

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        self.call_count += 1
        if isinstance(self._response, Exception):
            raise self._response
        return httpx.Response(
            status_code=200,
            json=self._response,
            request=httpx.Request("GET", url),
        )


class TestFreshDBMinimaxM3ServiceChain:
    @pytest.mark.asyncio()
    async def test_full_chain_sparse_to_non_sparse(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            cache = _make_cache("minimax-m3", provider_id="opencode-go")
            or_payload = _openrouter_payload(
                {
                    "id": "minimax/minimax-m3",
                    "name": "MiniMax M3",
                    "context_length": 128000,
                },
            )
            client = _MockHttpClient(or_payload)

            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            now = datetime.now(UTC)
            sparse_row = CanonicalModelInfo(
                model_id="minimax-m3",
                status="sparse_new",
                summary="",
                sparse=True,
                detail={},
                provenance={"sources": ["traffic_observation"]},
                conflicts={},
                first_seen_at=now,
                last_seen_at=now,
                last_refreshed_at=None,
                next_refresh_at=now,
            )
            await service.repo.upsert_canonical(sparse_row)

            info_before = await service.repo.get_canonical("minimax-m3")
            assert info_before is not None
            assert info_before.sparse is True
            assert info_before.status == "sparse_new"

            await service.load_cache()
            await service.reconcile_catalog_snapshot()

            result = await service.refresh_model_info("minimax-m3", force=True)
            assert result["errors"] == 0
            assert "openrouter" in result["sources_attempted"]

            info_after = await service.repo.get_canonical("minimax-m3")
            assert info_after is not None
            assert info_after.sparse is False
            assert info_after.status in ("partial", "fresh")
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_match_evidence_persisted(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            cache = _make_cache("minimax-m3", provider_id="opencode-go")
            or_payload = _openrouter_payload(
                {
                    "id": "minimax/minimax-m3",
                    "name": "MiniMax M3",
                    "context_length": 128000,
                },
            )
            client = _MockHttpClient(or_payload)

            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            now = datetime.now(UTC)
            sparse_row = CanonicalModelInfo(
                model_id="minimax-m3",
                status="sparse_new",
                summary="",
                sparse=True,
                detail={},
                provenance={"sources": ["traffic_observation"]},
                conflicts={},
                first_seen_at=now,
                last_seen_at=now,
                last_refreshed_at=None,
                next_refresh_at=now,
            )
            await service.repo.upsert_canonical(sparse_row)

            await service.load_cache()
            await service.reconcile_catalog_snapshot()

            await service.refresh_model_info("minimax-m3", force=True)

            evidence = await service.repo.list_match_evidence(
                "minimax-m3", source="openrouter"
            )
            assert len(evidence) >= 1
            row = evidence[0]
            assert row["match_method"] in ("normalized_exact", "regex_rule")
            assert float(row["confidence"]) > 0.0
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_due_refresh_returns_openrouter_matched(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            cache = _make_cache("minimax-m3", provider_id="opencode-go")
            or_payload = _openrouter_payload(
                {
                    "id": "minimax/minimax-m3",
                    "name": "MiniMax M3",
                    "context_length": 128000,
                },
            )
            client = _MockHttpClient(or_payload)

            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            await service.load_cache()
            await service.reconcile_catalog_snapshot()

            now = datetime.now(UTC)
            past = now - timedelta(hours=1)
            existing = await service.repo.get_canonical("minimax-m3")
            assert existing is not None
            due = CanonicalModelInfo(
                model_id="minimax-m3",
                status=existing.status,
                summary=existing.summary,
                sparse=existing.sparse,
                detail=existing.detail,
                provenance=existing.provenance,
                conflicts=existing.conflicts,
                first_seen_at=existing.first_seen_at,
                last_seen_at=existing.last_seen_at,
                last_refreshed_at=existing.last_refreshed_at,
                next_refresh_at=past,
            )
            await service.repo.upsert_canonical(due)

            result = await service.refresh_due_models()
            assert result.get("openrouter_matched", 0) >= 1
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Provider namespace accessor wiring (tiered OpenRouter matching)
# ---------------------------------------------------------------------------


class TestKnownProviderNamespacesAccessor:
    """Pin the contract between ``ModelCatalogCache.known_provider_ids`` and
    ``ModelInfoService._known_provider_namespaces``.

    Without this accessor the tiered OpenRouter resolver falls back to legacy
    exact-alias matching and silently loses the fresh-DB normalization fix.
    """

    @pytest.mark.asyncio()
    async def test_service_known_provider_namespaces_uses_catalog_accessor(
        self,
    ) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            cache = _make_cache("minimax-m3", provider_id="opencode-go")
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=None)
            assert service._known_provider_namespaces() == {"opencode-go"}
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_service_known_provider_namespaces_none_when_no_rows(
        self,
    ) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            cache = ModelCatalogCache()
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=None)
            assert service._known_provider_namespaces() is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_cache_exposes_known_provider_ids(self) -> None:
        cache = ModelCatalogCache()
        assert hasattr(cache, "known_provider_ids")
        assert callable(cache.known_provider_ids)
        cache._provider_models[("minimax-m3", "opencode-go")] = {
            "model_id": "minimax-m3",
            "protocol": "openai",
        }
        assert cache.known_provider_ids() == {"opencode-go"}
