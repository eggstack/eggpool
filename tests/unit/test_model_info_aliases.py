"""Tests for configured alias seeding and source-keyed alias reads.

Phase A of the model-info corrective plan:
- ``ModelInfoService.seed_configured_aliases()`` inserts every entry
  from ``config.model_info.aliases`` into ``model_info_aliases``.
- The API aliases endpoint returns source-keyed entries.
- Idempotency: re-running updates ``last_seen_at`` without duplicating rows.
- Hugging Face fetch is reachable only when an alias is seeded.
- Unknown source aliases are skipped with a warning, not raised.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.service import (
    ModelInfoService,
    _alias_confidence_to_float,
)
from eggpool.models.config import (
    ModelInfoAliasConfig,
    ModelInfoConfig,
    ModelInfoSourceConfig,
    ModelInfoSourcesConfig,
)


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(db: Database, model_id: str = "gpt-4o") -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, model_id),
        )


def _make_config(
    *,
    hf_enabled: bool = False,
    aliases: list[ModelInfoAliasConfig] | None = None,
) -> ModelInfoConfig:
    sources = ModelInfoSourcesConfig(
        provider_catalog=ModelInfoSourceConfig(),
        openrouter=ModelInfoSourceConfig(),
        artificial_analysis=ModelInfoSourceConfig(enabled=False),
        huggingface=ModelInfoSourceConfig(enabled=hf_enabled),
    )
    return ModelInfoConfig(
        sources=sources,
        aliases=aliases or [],
    )


def _make_cache(*models: str) -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    for model_id in models:
        cache._models[model_id] = {
            "model_id": model_id,
            "display_name": model_id,
            "protocol": "openai",
            "capabilities": {},
            "source_metadata": {},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {},
        }
    return cache


# ---------------------------------------------------------------------------
# Unit tests on the confidence helper
# ---------------------------------------------------------------------------


class TestAliasConfidence:
    def test_string_curated_returns_high_confidence(self) -> None:
        assert _alias_confidence_to_float("curated") == 0.9

    def test_string_exact_returns_full_confidence(self) -> None:
        assert _alias_confidence_to_float("exact") == 1.0

    def test_string_low_returns_low_confidence(self) -> None:
        assert _alias_confidence_to_float("low") == 0.3

    def test_unknown_string_returns_default(self) -> None:
        assert _alias_confidence_to_float("something_else") == 0.5

    def test_none_returns_default(self) -> None:
        assert _alias_confidence_to_float(None) == 0.5

    def test_float_is_clamped(self) -> None:
        assert _alias_confidence_to_float(0.42) == 0.42
        assert _alias_confidence_to_float(1.5) == 1.0
        assert _alias_confidence_to_float(-0.1) == 0.0

    def test_strips_whitespace_and_lowercases(self) -> None:
        assert _alias_confidence_to_float("  CURATED  ") == 0.9


# ---------------------------------------------------------------------------
# Service-level tests for seed_configured_aliases
# ---------------------------------------------------------------------------


class TestSeedConfiguredAliases:
    @pytest.mark.asyncio()
    async def test_seed_configured_huggingface_aliases_on_startup(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "llama-3.1-405b-instruct")

            aliases = [
                ModelInfoAliasConfig(
                    provider_id="fireworks",
                    model_id="llama-3.1-405b-instruct",
                    source="huggingface",
                    source_model_id="meta-llama/Llama-3.1-405B-Instruct",
                    confidence="curated",
                )
            ]
            config = _make_config(hf_enabled=True, aliases=aliases)
            cache = _make_cache("llama-3.1-405b-instruct")
            service = ModelInfoService(config=config, db=db, catalog=cache)

            result = await service.seed_configured_aliases()
            assert result == {"seeded": 1, "skipped": 0}

            repo = ModelInfoRepository(db)
            rows = await repo.list_alias_rows_for_model("llama-3.1-405b-instruct")
            assert len(rows) == 1
            assert rows[0]["source"] == "huggingface"
            assert rows[0]["alias"] == "meta-llama/Llama-3.1-405B-Instruct"
            assert rows[0]["provider_id"] == "fireworks"
            assert rows[0]["confidence"] == 0.9
            assert rows[0]["active"] is True
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_seed_configured_openrouter_aliases_on_startup(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o-mini")

            aliases = [
                ModelInfoAliasConfig(
                    provider_id="openrouter",
                    model_id="gpt-4o-mini",
                    source="openrouter",
                    source_model_id="openai/gpt-4o-mini",
                    confidence="exact",
                )
            ]
            config = _make_config(aliases=aliases)
            cache = _make_cache("gpt-4o-mini")
            service = ModelInfoService(config=config, db=db, catalog=cache)

            result = await service.seed_configured_aliases()
            assert result == {"seeded": 1, "skipped": 0}

            repo = ModelInfoRepository(db)
            rows = await repo.list_alias_rows_for_model("gpt-4o-mini")
            assert len(rows) == 1
            assert rows[0]["source"] == "openrouter"
            assert rows[0]["confidence"] == 1.0
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_configured_alias_is_idempotent(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            alias = ModelInfoAliasConfig(
                provider_id="openrouter",
                model_id="gpt-4o",
                source="openrouter",
                source_model_id="openai/gpt-4o",
                confidence="curated",
            )
            config = _make_config(aliases=[alias])
            cache = _make_cache("gpt-4o")
            service = ModelInfoService(config=config, db=db, catalog=cache)

            first = await service.seed_configured_aliases()
            assert first == {"seeded": 1, "skipped": 0}

            # Second run: ON CONFLICT refreshes last_seen_at and confidence;
            # no duplicate row.
            second = await service.seed_configured_aliases()
            assert second == {"seeded": 1, "skipped": 0}

            repo = ModelInfoRepository(db)
            rows = await repo.list_alias_rows_for_model("gpt-4o")
            assert len(rows) == 1
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_configured_alias_enables_huggingface_fetch(self) -> None:
        """The HF source uses aliases; without a seeded alias row, fetch_one
        is never reached for that model."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "llama-3.1-405b-instruct")

            aliases = [
                ModelInfoAliasConfig(
                    provider_id="fireworks",
                    model_id="llama-3.1-405b-instruct",
                    source="huggingface",
                    source_model_id="meta-llama/Llama-3.1-405B-Instruct",
                    confidence="curated",
                )
            ]
            config = _make_config(hf_enabled=True, aliases=aliases)
            cache = _make_cache("llama-3.1-405b-instruct")

            # Stub outbound client so the HF source is constructed.
            client = MagicMock()
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )
            # Replace HF source's fetch_one with a MagicMock so we can
            # assert it is called with the configured alias.
            service._huggingface_source.fetch_one = AsyncMock(  # type: ignore[union-attr]
                return_value=None
            )

            await service.seed_configured_aliases()
            aliases_list = await service._repo.get_aliases_for_model(  # type: ignore[attr-defined]
                "llama-3.1-405b-instruct", source="huggingface"
            )
            assert aliases_list == ["meta-llama/Llama-3.1-405B-Instruct"]

            # Now simulate refresh_due_models calling HF on this model.
            from eggpool.model_info.types import CanonicalModelInfo

            now = datetime.now(UTC)
            await service._repo.upsert_canonical(  # type: ignore[attr-defined]
                CanonicalModelInfo(
                    model_id="llama-3.1-405b-instruct",
                    status="sparse_new",
                    summary="",
                    sparse=True,
                    detail={},
                    provenance={"sources": ["provider_catalog"]},
                    conflicts={},
                    first_seen_at=now,
                    last_seen_at=now,
                    last_refreshed_at=None,
                    next_refresh_at=now,
                )
            )

            await service.refresh_due_models()
            service._huggingface_source.fetch_one.assert_awaited_with(  # type: ignore[union-attr]
                "meta-llama/Llama-3.1-405B-Instruct"
            )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_unknown_source_alias_is_rejected_or_warned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "some-model")

            bad_alias = ModelInfoAliasConfig(
                provider_id="some-provider",
                model_id="some-model",
                source="nonexistent-source",
                source_model_id="x/y",
                confidence="curated",
            )
            config = _make_config(aliases=[bad_alias])
            cache = _make_cache("some-model")
            service = ModelInfoService(config=config, db=db, catalog=cache)

            with caplog.at_level(logging.WARNING):
                result = await service.seed_configured_aliases()

            assert result == {"seeded": 0, "skipped": 1}
            assert any("unknown source" in rec.message for rec in caplog.records)
            repo = ModelInfoRepository(db)
            rows = await repo.list_alias_rows_for_model("some-model")
            assert rows == []
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_empty_source_model_id_is_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "some-model")

            bad_alias = ModelInfoAliasConfig(
                provider_id="some-provider",
                model_id="some-model",
                source="huggingface",
                source_model_id="",
                confidence="curated",
            )
            config = _make_config(aliases=[bad_alias])
            cache = _make_cache("some-model")
            service = ModelInfoService(config=config, db=db, catalog=cache)

            with caplog.at_level(logging.WARNING):
                result = await service.seed_configured_aliases()

            assert result == {"seeded": 0, "skipped": 1}
            assert any("empty source_model_id" in rec.message for rec in caplog.records)
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_load_cache_seeds_aliases_before_provider_refresh(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "test-model")

            aliases = [
                ModelInfoAliasConfig(
                    provider_id="openrouter",
                    model_id="test-model",
                    source="openrouter",
                    source_model_id="openai/test-model",
                    confidence="curated",
                )
            ]
            config = _make_config(aliases=aliases)
            cache = _make_cache("test-model")
            service = ModelInfoService(config=config, db=db, catalog=cache)

            # load_cache should run seed_configured_aliases.
            await service.load_cache()

            repo = ModelInfoRepository(db)
            rows = await repo.list_alias_rows_for_model("test-model")
            assert len(rows) == 1
            assert rows[0]["source"] == "openrouter"
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# API: source-keyed alias shape
# ---------------------------------------------------------------------------


class TestModelInfoAliasesEndpoint:
    @pytest.mark.asyncio()
    async def test_aliases_endpoint_returns_source_keyed_entries(self) -> None:
        from fastapi import FastAPI

        from eggpool.api.model_info import handle_model_info_aliases

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")
            repo = ModelInfoRepository(db)
            await repo.upsert_alias(
                model_id="gpt-4o",
                provider_id="openrouter",
                alias="openai/gpt-4o",
                source="openrouter",
                confidence=1.0,
            )
            await repo.upsert_alias(
                model_id="gpt-4o",
                provider_id="huggingface",
                alias="openai/gpt-4o",
                source="huggingface",
                confidence=0.6,
            )

            service = MagicMock()
            service.repo = repo

            app = FastAPI()
            app.state.model_info = service
            request = MagicMock()
            request.app.state.model_info = service

            response = await handle_model_info_aliases(request, "gpt-4o")
            data = json.loads(response.body)
            assert data["model_id"] == "gpt-4o"
            # Both rows have the same alias string but different sources,
            # so the flat list returns two entries (one per source).
            assert sorted(data["aliases"]) == ["openai/gpt-4o", "openai/gpt-4o"]
            assert len(data["aliases_by_source"]) == 2
            sources = {row["source"] for row in data["aliases_by_source"]}
            assert sources == {"openrouter", "huggingface"}
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_aliases_endpoint_503_when_service_missing(self) -> None:

        from eggpool.api.model_info import handle_model_info_aliases

        request = MagicMock()
        request.app.state.model_info = None

        response = await handle_model_info_aliases(request, "x")
        assert response.status_code == 503
