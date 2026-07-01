"""Tests for the OpenRouter identity resolver's provider-catalog fallback.

These tests exercise the rule added in ``resolve_openrouter_record``:
when no ``openrouter``-sourced alias exists, an exact-match alias from
``provider_catalog`` is consulted. The fallback is the entire reason
the dashboard's model-info detail page is able to populate OpenRouter
metadata for every model in the operator's 33-model test set without
any hand-curated ``[model_info.aliases]`` block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.identity import resolve_openrouter_record
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.types import SourceModelRecord


async def _run_migrations(db: Database) -> None:
    await MigrationRunner(db).run()


async def _seed_model(db: Database, model_id: str) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, model_id),
        )


def _make_or_record(source_model_id: str) -> SourceModelRecord:
    now = datetime.now(UTC)
    return SourceModelRecord(
        source="openrouter",
        source_model_id=source_model_id,
        model_id=source_model_id,
        provider_id=None,
        observed_at=now,
        raw_hash=source_model_id,
        raw_payload={},
        normalized={},
        display_name=source_model_id,
        context_window=128000,
        max_input_tokens=None,
        max_output_tokens=8192,
        modalities=frozenset({"text"}),
        confidence=0.9,
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


class TestResolveOpenRouterRecordProviderCatalogFallback:
    @pytest.mark.asyncio()
    async def test_provider_catalog_alias_matches_openrouter_vendor_prefix(
        self,
    ) -> None:
        """A provider-catalog alias like ``openai/gpt-4o`` is the only
        way an OpenRouter record ``openai/gpt-4o`` can be paired with
        the unsuffixed catalog row ``gpt-4o``. The operator's 33-model
        dashboard test fixture (all with vendor-prefixed OpenRouter
        ids) depends on this path.
        """
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            # Mirror what ``refresh_provider_catalog_observations``
            # writes when ``ProviderCatalogSource._build_record`` emits
            # the ``<provider_id>/<model_id>`` alias.
            await repo.upsert_alias(
                model_id="gpt-4o",
                provider_id="openai",
                alias="openai/gpt-4o",
                source="provider_catalog",
                confidence=1.0,
            )

            or_records = {
                "openai/gpt-4o": _make_or_record("openai/gpt-4o"),
            }
            result = await resolve_openrouter_record("gpt-4o", repo, or_records)
            assert result is not None
            assert result.source_model_id == "openai/gpt-4o"
            assert result.context_window == 128000
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_provider_catalog_alias_does_not_match_unrelated_vendor(
        self,
    ) -> None:
        """A provider-catalog alias whose value does NOT appear in the
        OpenRouter catalog must not produce a phantom match. The
        resolver still requires an exact match against indexed records.
        """
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            await repo.upsert_alias(
                model_id="gpt-4o",
                provider_id="weird-vendor",
                alias="weird-vendor/gpt-4o",
                source="provider_catalog",
                confidence=1.0,
            )

            # Empty indexed dict: even with the alias, no record matches.
            result = await resolve_openrouter_record("gpt-4o", repo, {})
            assert result is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_curated_openrouter_alias_takes_precedence(
        self,
    ) -> None:
        """When the operator ships a curated ``openrouter`` alias, Rule 1
        wins and the provider-catalog fallback is unused. This preserves
        the existing identity resolution contract for power users.
        """
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")

            repo = ModelInfoRepository(db)
            await repo.upsert_alias(
                model_id="gpt-4o",
                provider_id="openai",
                alias="openai/gpt-4o",
                source="provider_catalog",
                confidence=1.0,
            )
            await repo.upsert_alias(
                model_id="gpt-4o",
                provider_id="openrouter",
                alias="openai/gpt-4o-curated",
                source="openrouter",
                confidence=0.9,
            )

            or_records = {
                "openai/gpt-4o": _make_or_record("openai/gpt-4o"),
                "openai/gpt-4o-curated": _make_or_record("openai/gpt-4o-curated"),
            }
            result = await resolve_openrouter_record("gpt-4o", repo, or_records)
            assert result is not None
            assert result.source_model_id == "openai/gpt-4o-curated"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_ambiguous_provider_catalog_aliases_skip(self) -> None:
        """When multiple provider-catalog aliases exist for the same
        model_id (e.g. it appears under two providers, both with
        vendor-prefixed OpenRouter entries), the resolver returns the
        single matching one if unambiguous, otherwise it skips."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "shared-model")

            repo = ModelInfoRepository(db)
            await repo.upsert_alias(
                model_id="shared-model",
                provider_id="vendor-a",
                alias="vendor-a/shared-model",
                source="provider_catalog",
                confidence=1.0,
            )
            await repo.upsert_alias(
                model_id="shared-model",
                provider_id="vendor-b",
                alias="vendor-b/shared-model",
                source="provider_catalog",
                confidence=1.0,
            )

            # Unambiguous: only vendor-a has an OpenRouter record.
            or_records = {
                "vendor-a/shared-model": _make_or_record("vendor-a/shared-model"),
            }
            result = await resolve_openrouter_record("shared-model", repo, or_records)
            assert result is not None
            assert result.source_model_id == "vendor-a/shared-model"

            # Ambiguous: both vendors are on OpenRouter.  Must skip.
            or_records_both: dict[str, Any] = {
                "vendor-a/shared-model": _make_or_record("vendor-a/shared-model"),
                "vendor-b/shared-model": _make_or_record("vendor-b/shared-model"),
            }
            result_skip = await resolve_openrouter_record(
                "shared-model", repo, or_records_both
            )
            assert result_skip is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_provider_catalog_source_emits_vendor_prefixed_alias(
        self,
    ) -> None:
        """Direct regression: ``ProviderCatalogSource._build_record``
        must include ``<provider_id>/<model_id>`` in its alias tuple so
        the upstream resolver fallback path has anything to look up."""
        cache = _make_cache("claude-opus-4")
        cache._provider_models[("claude-opus-4", "anthropic")] = dict(
            cache._models["claude-opus-4"]
        )

        from eggpool.model_info.sources.provider_catalog import (
            ProviderCatalogSource,
        )

        source = ProviderCatalogSource(cache)
        records = await source.fetch_all()
        assert len(records) == 1
        record = records[0]
        assert "anthropic/claude-opus-4" in record.aliases
        # The bare id is always present so resolvers that key on the
        # model_id itself succeed without operator configuration.
        assert "claude-opus-4" in record.aliases

    @pytest.mark.asyncio()
    async def test_provider_id_alias_skipped_when_equal_to_model_id(
        self,
    ) -> None:
        """A provider that uses the same id as the model (no real
        namespaces) must not generate an alias like
        ``claude-opus-4/claude-opus-4`` because it would only match an
        equivalent OpenRouter entry if that exotic name existed there."""
        cache = _make_cache("claude-opus-4")
        cache._provider_models[("claude-opus-4", "claude-opus-4")] = dict(
            cache._models["claude-opus-4"]
        )

        from eggpool.model_info.sources.provider_catalog import (
            ProviderCatalogSource,
        )

        source = ProviderCatalogSource(cache)
        records = await source.fetch_all()
        record = records[0]
        assert "claude-opus-4/claude-opus-4" not in record.aliases
