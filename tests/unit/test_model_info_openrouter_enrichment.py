"""Tests for the OpenRouter enrichment corrective plan.

This file pins the invariants the plan promised:

* Source health records ``openrouter`` even when no local model matched
  (Phase 1.1).
* Forced refresh returns ``source_diagnostics`` with miss reason and
  catalog count (Phase 1.2).
* Alias lookup is case-insensitive and de-duplicates identical cases
  (Phase 2.1 + 2.2).
* Configured aliases are reseeded on manual refresh (Phase 2.3).
* Forced refresh invalidates and re-fetches the OpenRouter cache
  when an alias exists but did not match (Phase 2.4).
* Canonical detail promotes an external ``display_name_<source>`` into
  ``detail.display_name`` when the provider has none (Phase 3.1).
* Source-scoped pricing is captured under ``detail.pricing.<source>``
  (Phase 3.1).
* API detail ``observations[]`` reflects persisted DB rows rather than
  the legacy synthesised projection (Phase 4).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from eggpool.api.model_info import (
    _detail_response,
    handle_model_info_detail,
)
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.service import (
    ModelInfoService,
    build_canonical_detail,
)
from eggpool.model_info.sources.openrouter import (
    _parse_entry_to_record,
)
from eggpool.model_info.types import SourceModelRecord
from eggpool.models.config import (
    ModelInfoAliasConfig,
    ModelInfoConfig,
    ModelInfoSourceConfig,
    ModelInfoSourcesConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_migrations(db: Database) -> None:
    await MigrationRunner(db).run()


async def _seed_model(db: Database, model_id: str) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, model_id),
        )


def _make_config(
    *,
    or_enabled: bool = True,
    aliases: list[ModelInfoAliasConfig] | None = None,
    openrouter_ttl: int = 60,
) -> ModelInfoConfig:
    sources = ModelInfoSourcesConfig(
        provider_catalog=ModelInfoSourceConfig(),
        openrouter=ModelInfoSourceConfig(
            enabled=or_enabled, ttl_seconds=openrouter_ttl
        ),
        artificial_analysis=ModelInfoSourceConfig(enabled=False),
        huggingface=ModelInfoSourceConfig(enabled=False),
    )
    return ModelInfoConfig(
        sources=sources,
        aliases=aliases or [],
    )


def _make_cache(model_id: str, *, display_name: str | None = None) -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    entry: dict[str, Any] = {
        "model_id": model_id,
        "display_name": display_name if display_name is not None else model_id,
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
    # ``_make_cache`` keeps the entry in both indexes by default but
    # callers that want to test "provider has no display name" can
    # pass ``display_name=None`` to mean "leave it unset".  When the
    # explicit ``None`` is passed we drop the field entirely so the
    # canonical detail merge falls back to OpenRouter's
    # ``display_name_openrouter``.
    if display_name is None:
        entry.pop("display_name", None)
    cache._models[model_id] = entry
    cache._provider_models[(model_id, "openai")] = dict(entry)
    return cache


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


def _or_payload(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(entries)}


def _minimax_entry() -> dict[str, Any]:
    return {
        "id": "minimax/minimax-m3",
        "name": "MiniMax: MiniMax M3",
        "context_length": 1_048_576,
        "top_provider": {"max_completion_tokens": 512_000},
        "architecture": {
            "input_modalities": ["text", "image", "video"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["tools"],
        "pricing": {
            "prompt": "0.0000003",
            "completion": "0.0000012",
        },
    }


# ---------------------------------------------------------------------------
# Phase 1.1: source health records OpenRouter even when no model matches
# ---------------------------------------------------------------------------


class TestOpenRouterSourceHealthWithoutMatch:
    @pytest.mark.asyncio()
    async def test_refresh_records_openrouter_health_even_on_no_match(self) -> None:
        """A successful OpenRouter fetch with no matched local model still
        records a source success row so operators can tell the source is
        alive and not stale."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            client = _MockHttpClient(response=_or_payload(_minimax_entry()))
            config = _make_config()
            cache = _make_cache("minimax-m3")
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )

            result = await service.refresh_model_info("no-such-model", force=True)
            assert "openrouter" in result["sources_attempted"]
            assert result["sources_matched"] == []
            health = await service.repo.source_health_snapshot()
            assert "openrouter" in health
            assert health["openrouter"]["last_success_at"] is not None
            assert health["openrouter"]["last_payload_count"] == 1
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_refresh_sources_matched_when_alias_matches(self) -> None:
        """When a configured alias resolves in the OpenRouter catalog, the
        result reports the match in ``sources_matched``."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            aliases = [
                ModelInfoAliasConfig(
                    provider_id="opencode-go",
                    model_id="minimax-m3",
                    source="openrouter",
                    source_model_id="minimax/minimax-m3",
                    confidence="exact",
                )
            ]
            client = _MockHttpClient(response=_or_payload(_minimax_entry()))
            config = _make_config(aliases=aliases)
            cache = _make_cache("minimax-m3")
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )

            result = await service.refresh_model_info("minimax-m3", force=True)
            assert "openrouter" in result["sources_matched"]
            assert result["observations"] >= 1
            diag = result["source_diagnostics"]["openrouter"]
            assert diag["miss_reason"] == "matched"
            assert diag["matched_source_model_id"] == "minimax/minimax-m3"
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Phase 1.2: source_diagnostics with miss reasons
# ---------------------------------------------------------------------------


class TestOpenRouterDiagnostics:
    @pytest.mark.asyncio()
    async def test_diagnostics_reports_alias_not_in_catalog(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            aliases = [
                ModelInfoAliasConfig(
                    provider_id="opencode-go",
                    model_id="minimax-m3",
                    source="openrouter",
                    source_model_id="minimax/minimax-m3",
                    confidence="exact",
                )
            ]
            # OpenRouter catalog has different IDs - no match.
            client = _MockHttpClient(response=_or_payload({"id": "other/model-x"}))
            config = _make_config(aliases=aliases)
            cache = _make_cache("minimax-m3")
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )

            result = await service.refresh_model_info("minimax-m3", force=True)
            diag = result["source_diagnostics"]["openrouter"]
            assert diag["fetched"] is True
            assert diag["catalog_count"] == 1
            assert diag["alias_candidates"] == ["minimax/minimax-m3"]
            assert diag["miss_reason"] == "alias_not_in_catalog"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_diagnostics_reports_no_aliases_when_unconfigured(self) -> None:
        """When no alias is configured the tiered resolver may still
        match via normalized exact. Diagnostics must still surface the
        empty configured-alias list and the match_method used."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            client = _MockHttpClient(response=_or_payload(_minimax_entry()))
            config = _make_config(aliases=[])
            cache = _make_cache("minimax-m3")
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )

            result = await service.refresh_model_info("minimax-m3", force=True)
            diag = result["source_diagnostics"]["openrouter"]
            assert diag["alias_candidates"] == []
            assert diag["match_method"] in (
                "normalized_exact",
                "exact_source_id",
                "regex_rule",
                "similarity_guarded",
                "configured_exact_alias",
            )
            assert diag["matched_source_model_id"] == "minimax/minimax-m3"
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Phase 2.1 + 2.2: case-insensitive alias lookup
# ---------------------------------------------------------------------------


class TestAliasCaseInsensitiveLookup:
    @pytest.mark.asyncio()
    async def test_get_aliases_for_model_is_case_insensitive(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "MiniMax-M3")
            await _seed_model(db, "minimax-m3")
            repo = ModelInfoRepository(db)
            await repo.upsert_alias(
                model_id="minimax-m3",  # different case in storage
                provider_id="opencode-go",
                alias="minimax/minimax-m3",
                source="openrouter",
                confidence=1.0,
            )

            upper_aliases = await repo.get_aliases_for_model("MINIMAX-M3")
            mixed_aliases = await repo.get_aliases_for_model("Minimax-m3")
            lower_aliases = await repo.get_aliases_for_model("minimax-m3")
            assert upper_aliases == ["minimax/minimax-m3"]
            assert mixed_aliases == ["minimax/minimax-m3"]
            assert lower_aliases == ["minimax/minimax-m3"]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_list_alias_rows_includes_stored_model_id(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "MiniMax-M3")
            await _seed_model(db, "minimax-m3")
            repo = ModelInfoRepository(db)
            await repo.upsert_alias(
                model_id="minimax-m3",
                provider_id="opencode-go",
                alias="minimax/minimax-m3",
                source="openrouter",
                confidence=1.0,
            )

            rows = await repo.list_alias_rows_for_model("MINIMAX-M3")
            assert len(rows) == 1
            assert rows[0]["model_id"] == "minimax-m3"
            assert rows[0]["alias"] == "minimax/minimax-m3"
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Phase 2.3: configured aliases reseeded on forced refresh
# ---------------------------------------------------------------------------


class TestRefreshResedsConfiguredAliases:
    @pytest.mark.asyncio()
    async def test_forced_refresh_reseeds_configured_aliases(self) -> None:
        """A forced refresh must observe any newly configured aliases even
        if startup already ran ``seed_configured_aliases``."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            client = _MockHttpClient(response=_or_payload(_minimax_entry()))
            config = _make_config()
            cache = _make_cache("minimax-m3")
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )

            # Append a configured alias after startup
            new_alias = ModelInfoAliasConfig(
                provider_id="opencode-go",
                model_id="minimax-m3",
                source="openrouter",
                source_model_id="minimax/minimax-m3",
                confidence="exact",
            )
            # Mutate in-place since config is frozen; replace _config via
            # monkeypatching works, but easier: build a new config with
            # the alias and reuse the same db/cache.
            config_with_alias = _make_config(aliases=[new_alias])
            service._config = config_with_alias  # type: ignore[attr-defined]

            seed_spy = AsyncMock(wraps=service.seed_configured_aliases)
            service.seed_configured_aliases = seed_spy  # type: ignore[method-assign]

            result = await service.refresh_model_info("minimax-m3", force=True)
            assert "openrouter" in result["sources_matched"]
            seed_spy.assert_awaited()
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Phase 2.4: invalidate_cache on forced refresh with aliases but no match
# ---------------------------------------------------------------------------


class TestOpenRouterCacheBypassOnForce:
    @pytest.mark.asyncio()
    async def test_invalidate_cache_runs_when_alias_missing(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            aliases = [
                ModelInfoAliasConfig(
                    provider_id="opencode-go",
                    model_id="minimax-m3",
                    source="openrouter",
                    source_model_id="minimax/minimax-m3",
                    confidence="exact",
                )
            ]
            client = _MockHttpClient(response=_or_payload(_minimax_entry()))
            config = _make_config(aliases=aliases)
            cache = _make_cache("minimax-m3")
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )

            invalidate = MagicMock()
            service._openrouter_source.invalidate_cache = invalidate  # type: ignore[attr-defined]

            result = await service.refresh_model_info("minimax-m3", force=True)
            diag = result["source_diagnostics"]["openrouter"]
            # First pass populated catalog with our entry, matched,
            # so no retry path was needed. Cache bypass is reserved
            # for the miss case.
            assert invalidate.call_count == 0
            assert diag["miss_reason"] == "matched"

            # Now flip the catalog to a different model — first fetch
            # yields alias_not_in_catalog; the retry invalidates the
            # cache and refetches, finding nothing.
            client._response = _or_payload({"id": "other/model-x"})
            invalidate.reset_mock()
            # Also reset the cache so the second fetch re-reads.
            if service._openrouter_source is not None:
                service._openrouter_source._cache.invalidate()  # type: ignore[attr-defined]

            result2 = await service.refresh_model_info("minimax-m3", force=True)
            diag2 = result2["source_diagnostics"]["openrouter"]
            assert diag2["miss_reason"] == "alias_not_in_catalog"
            assert invalidate.call_count >= 1
            assert diag2["cache_retry"] is True
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Phase 3.1: canonical merge promotes display_name_<source>
# ---------------------------------------------------------------------------


class TestCanonicalDetailDisplayNamePromotion:
    def test_external_display_name_promoted_when_provider_lacks_one(self) -> None:
        merged, _provenance, _conflicts = build_canonical_detail(
            model_id="minimax-m3",
            provider_detail={},  # no display_name from provider
            observation_payloads=[
                {
                    "source": "openrouter",
                    "source_model_id": "minimax/minimax-m3",
                    "confidence": 0.5,
                    "normalized": {
                        "display_name": "MiniMax: MiniMax M3",
                        "context_window": 1_048_576,
                        "max_output_tokens": 512_000,
                        "modalities": ["text", "image", "video"],
                    },
                }
            ],
        )
        assert merged["display_name"] == "MiniMax: MiniMax M3"
        assert merged["display_name_source"] == "openrouter"
        limits = merged["limits"]
        assert limits["external_context"] == 1_048_576
        assert limits["external_output"] == 512_000
        assert merged["external_ids"]["openrouter"] == "minimax/minimax-m3"

    def test_provider_display_name_wins_over_external(self) -> None:
        """The provider-catalog display_name stays authoritative when present."""
        merged, _, _ = build_canonical_detail(
            model_id="minimax-m3",
            provider_detail={"display_name": "Provider Display Name"},
            observation_payloads=[
                {
                    "source": "openrouter",
                    "source_model_id": "minimax/minimax-m3",
                    "normalized": {
                        "display_name": "OR Display Name",
                    },
                }
            ],
        )
        assert merged["display_name"] == "Provider Display Name"
        assert "display_name_source" not in merged

    def test_external_pricing_under_source_block(self) -> None:
        merged, _, _ = build_canonical_detail(
            model_id="minimax-m3",
            provider_detail={},
            observation_payloads=[
                {
                    "source": "openrouter",
                    "source_model_id": "minimax/minimax-m3",
                    "normalized": {
                        "input_price_per_1k": 0.0000003,
                        "output_price_per_1k": 0.0000012,
                    },
                }
            ],
        )
        pricing = merged["pricing"]
        assert "openrouter" in pricing
        assert pricing["openrouter"]["input_price_per_1k"] == 0.0000003
        assert pricing["openrouter"]["output_price_per_1k"] == 0.0000012


# ---------------------------------------------------------------------------
# Phase 4: API observations reflect DB rows
# ---------------------------------------------------------------------------


class TestCompactObservationsRepository:
    @pytest.mark.asyncio()
    async def test_list_compact_observations_returns_db_rows(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            openrouter_record = SourceModelRecord(
                source="openrouter",
                source_model_id="minimax/minimax-m3",
                observed_at=now,
                raw_hash="or-hash-1",
                raw_payload={},
                normalized={
                    "display_name": "MiniMax: MiniMax M3",
                    "context_window": 1_048_576,
                    "max_output_tokens": 512_000,
                    "modalities": ["text", "image", "video"],
                },
                display_name="MiniMax: MiniMax M3",
                context_window=1_048_576,
                max_output_tokens=512_000,
                modalities=frozenset({"text", "image", "video"}),
                confidence=0.5,
            )
            await repo.upsert_observation(openrouter_record, model_id="minimax-m3")

            rows = await repo.list_compact_observations_for_model("minimax-m3")
            assert len(rows) == 1
            row = rows[0]
            assert row["source"] == "openrouter"
            assert row["source_model_id"] == "minimax/minimax-m3"
            assert row["provider_id"] is None
            assert row["confidence"] == 0.5
            assert row["display_name"] == "MiniMax: MiniMax M3"
            assert row["context_window"] == 1_048_576
            assert row["max_output_tokens"] == 512_000

            # Case-insensitive lookup also matches
            rows_upper = await repo.list_compact_observations_for_model("MINIMAX-M3")
            assert len(rows_upper) == 1
            assert rows_upper[0]["source_model_id"] == "minimax/minimax-m3"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_list_compact_observations_picks_latest_per_source(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            repo = ModelInfoRepository(db)
            older = datetime(2026, 1, 1, tzinfo=UTC)
            newer = datetime(2026, 7, 4, tzinfo=UTC)
            for observed_at in (older, newer):
                rec = SourceModelRecord(
                    source="openrouter",
                    source_model_id="minimax/minimax-m3",
                    observed_at=observed_at,
                    raw_hash=f"hash-{observed_at.isoformat()}",
                    raw_payload={},
                    normalized={"context_window": 1_048_576},
                    context_window=1_048_576,
                    confidence=0.5,
                )
                await repo.upsert_observation(rec, model_id="minimax-m3")

            rows = await repo.list_compact_observations_for_model("minimax-m3")
            assert len(rows) == 1
            assert rows[0]["observed_at"].startswith("2026-07-04")
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_list_compact_observations_empty_when_no_rows(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            repo = ModelInfoRepository(db)
            rows = await repo.list_compact_observations_for_model("nope")
            assert rows == []
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Phase 4: API endpoint returns real DB observations
# ---------------------------------------------------------------------------


class TestDetailEndpointObservations:
    @pytest.mark.asyncio()
    async def test_detail_endpoint_observes_db_rows(self) -> None:
        from fastapi import FastAPI

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")
            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            await repo.upsert_observation(
                SourceModelRecord(
                    source="openrouter",
                    source_model_id="minimax/minimax-m3",
                    observed_at=now,
                    raw_hash="or-hash",
                    raw_payload={},
                    normalized={
                        "display_name": "MiniMax: MiniMax M3",
                        "context_window": 1_048_576,
                    },
                    display_name="MiniMax: MiniMax M3",
                    context_window=1_048_576,
                    confidence=0.5,
                ),
                model_id="minimax-m3",
            )

            # Seed a canonical row so the endpoint succeeds.
            info = MagicMock()
            info.detail = {
                "providers": ["opencode-go"],
                "limits": {
                    "external_context": 1_048_576,
                    "external_output": 512_000,
                },
                "modalities": ["text", "image", "video"],
                "external_ids": {"openrouter": "minimax/minimax-m3"},
                "benchmarks": [],
                "huggingface_metadata": {},
                "family": None,
                "license": None,
                "release_date": None,
                "supports_tools": True,
                "display_name": "MiniMax: MiniMax M3",
                "display_name_source": "openrouter",
                "pricing": {"openrouter": {"input_price_per_1k": 0.3e-6}},
            }
            info.provenance = {"sources": ["openrouter", "provider_catalog"]}
            info.conflicts = {}
            info.status = "partial"
            info.sparse = False
            info.summary = ""
            info.model_id = "minimax-m3"
            info.first_seen_at = now
            info.last_seen_at = now
            info.last_refreshed_at = None
            info.next_refresh_at = None

            service = MagicMock()
            service.get_summary = AsyncMock(return_value=info)
            service.repo = repo

            app = FastAPI()
            app.state.model_info = service

            request = MagicMock()
            request.app.state.model_info = service
            request.app.state.config = MagicMock()
            request.app.state.config.providers = {"opencode-go"}

            response = await handle_model_info_detail(request, "minimax-m3")
            data = json.loads(response.body)
            observations = data["observations"]
            assert len(observations) == 1
            assert observations[0]["source"] == "openrouter"
            assert observations[0]["source_model_id"] == "minimax/minimax-m3"
            assert observations[0]["provider_id"] is None
            assert observations[0]["confidence"] == 0.5
            # Synthesised-projection guard: legacy rows flagged _synthetic.
            for obs in observations:
                assert obs.get("_synthetic") is not True
            # Detail block surfaces display_name + pricing block.
            assert data["detail"]["display_name"] == "MiniMax: MiniMax M3"
            assert data["detail"]["display_name_source"] == "openrouter"
            assert (
                data["detail"]["pricing"]["openrouter"]["input_price_per_1k"] == 0.3e-6
            )
        finally:
            await db.disconnect()

    def test_detail_response_falls_back_to_synthetic(self) -> None:
        """When the caller does not pass observations AND does not pass
        ``observations_error``, the legacy synthesised projection is
        preserved for backward compatibility with existing test doubles."""
        from types import SimpleNamespace

        info = SimpleNamespace(
            detail={"providers": ["openai"]},
            provenance={"sources": ["provider_catalog", "openrouter"]},
            conflicts={},
            status="fresh",
            sparse=False,
            summary="",
            model_id="test",
            first_seen_at=None,
            last_seen_at=datetime(2026, 7, 4, tzinfo=UTC),
            last_refreshed_at=None,
            next_refresh_at=None,
        )
        response = _detail_response(info, observations=None)
        assert any(o.get("_synthetic") for o in response["observations"])

    def test_detail_response_empty_observations_with_error(self) -> None:
        """Phase 2 polish: when ``observations_error`` is set, the
        response must NOT synthesise observation rows.  Callers see an
        empty list + the error class name."""
        from types import SimpleNamespace

        info = SimpleNamespace(
            detail={"providers": ["openai"]},
            provenance={"sources": ["provider_catalog", "openrouter"]},
            conflicts={},
            status="fresh",
            sparse=False,
            summary="",
            model_id="test",
            first_seen_at=None,
            last_seen_at=None,
            last_refreshed_at=None,
            next_refresh_at=None,
        )
        response = _detail_response(
            info, observations=None, observations_error="OperationalError"
        )
        assert response["observations"] == []
        assert response["observations_error"] == "OperationalError"
        for obs in response["observations"]:
            assert obs.get("_synthetic") is not True

    @pytest.mark.asyncio()
    async def test_detail_handler_observation_read_failure_returns_empty_with_error(
        self,
    ) -> None:
        """Phase 2 polish: when ``repo.list_compact_observations_for_model``
        raises, the API returns ``observations == []`` with
        ``observations_error`` set to the exception class name.  No
        synthetic rows leak into the response."""
        import json

        from fastapi import FastAPI

        info = MagicMock()
        info.detail = {
            "providers": ["opencode-go"],
            "limits": {},
            "modalities": [],
            "external_ids": {},
            "benchmarks": [],
            "huggingface_metadata": {},
            "family": None,
            "license": None,
            "release_date": None,
            "supports_tools": True,
            "display_name": None,
            "pricing": {},
        }
        info.provenance = {"sources": ["provider_catalog", "openrouter"]}
        info.conflicts = {}
        info.status = "partial"
        info.sparse = False
        info.summary = ""
        info.model_id = "minimax-m3"
        info.first_seen_at = None
        info.last_seen_at = None
        info.last_refreshed_at = None
        info.next_refresh_at = None

        service = MagicMock()
        service.get_summary = AsyncMock(return_value=info)
        service.repo.list_compact_observations_for_model = AsyncMock(
            side_effect=RuntimeError("db locked")
        )

        app = FastAPI()
        app.state.model_info = service

        request = MagicMock()
        request.app.state.model_info = service
        request.app.state.config = MagicMock()
        request.app.state.config.providers = {"opencode-go"}

        response = await handle_model_info_detail(request, "minimax-m3")
        data = json.loads(response.body)
        assert data["observations"] == []
        assert data["observations_error"] == "RuntimeError"
        # No synthetic rows slipped through the handler error path.
        for obs in data["observations"]:
            assert obs.get("_synthetic") is not True


# ---------------------------------------------------------------------------
# Phase 2 + 3: OpenRouter parsing still reads new payloads
# ---------------------------------------------------------------------------


class TestParseEntryToRecord:
    def test_parses_minimax_m3_shape(self) -> None:
        now = datetime.now(UTC)
        raw = _minimax_entry()
        record = _parse_entry_to_record(raw["id"], raw, now)
        assert record.source == "openrouter"
        assert record.source_model_id == "minimax/minimax-m3"
        assert record.display_name == "MiniMax: MiniMax M3"
        assert record.context_window == 1_048_576
        assert record.max_output_tokens == 512_000
        assert "text" in record.modalities
        assert "image" in record.modalities
        assert "video" in record.modalities
        assert record.supports_tools is True

    def test_parses_nested_public_benchmarks(self) -> None:
        """OpenRouter's nested benchmark object reaches normalized output."""
        now = datetime.now(UTC)
        raw = {
            **_minimax_entry(),
            "benchmarks": {
                "artificial_analysis": {
                    "intelligence_index": 55.7,
                    "coding_index": 74.3,
                },
                "design_arena": [
                    {
                        "arena": "models",
                        "category": "website",
                        "elo": 1281,
                        "win_rate": 55.0,
                        "rank": 21,
                    }
                ],
            },
        }

        record = _parse_entry_to_record(raw["id"], raw, now)

        assert len(record.benchmarks) == 3
        intelligence = next(
            b
            for b in record.benchmarks
            if b.benchmark_name == "Artificial Analysis Intelligence Index"
        )
        assert intelligence.score == 55.7
        assert intelligence.source == "artificial_analysis"
        arena = next(b for b in record.benchmarks if b.source == "openrouter")
        assert arena.benchmark_name == "Design Arena: models / website"
        assert arena.score == 1281.0
        assert arena.rank == 21
        assert len(record.normalized["benchmarks"]) == 3

    def test_ignores_non_finite_benchmark_values(self) -> None:
        """NaN and infinity must not enter persisted model metadata."""
        raw = {
            **_minimax_entry(),
            "benchmarks": {
                "artificial_analysis": {
                    "valid_index": 55.7,
                    "nan_index": float("nan"),
                    "infinite_index": float("inf"),
                }
            },
        }

        record = _parse_entry_to_record(raw["id"], raw, datetime.now(UTC))

        assert len(record.benchmarks) == 1
        assert record.benchmarks[0].benchmark_name == (
            "Artificial Analysis Valid Index"
        )


# ---------------------------------------------------------------------------
# Phase 3: scheduled refresh parity (refresh_due_models)
# ---------------------------------------------------------------------------


class TestRefreshDueModelsEnrichment:
    @pytest.mark.asyncio()
    async def test_refresh_due_models_enriches_minimax_m3_from_openrouter(
        self,
    ) -> None:
        """The scheduled ``refresh_due_models`` path must persist
        OpenRouter observations and update the canonical detail with
        external display_name / context / external_ids when the alias
        resolves."""
        from datetime import timedelta

        from eggpool.model_info.types import CanonicalModelInfo

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            client = _MockHttpClient(
                response=_or_payload(
                    {
                        **_minimax_entry(),
                        "benchmarks": {
                            "artificial_analysis": {
                                "intelligence_index": 55.7,
                            }
                        },
                    }
                )
            )
            aliases = [
                ModelInfoAliasConfig(
                    provider_id="opencode-go",
                    model_id="minimax-m3",
                    source="openrouter",
                    source_model_id="minimax/minimax-m3",
                    confidence="exact",
                )
            ]
            config = _make_config(aliases=aliases)
            cache = _make_cache("minimax-m3", display_name=None)
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )
            # Seed configured aliases since ``refresh_due_models`` does
            # not re-seed them (only ``refresh_model_info`` does).
            await service.seed_configured_aliases()

            # Seed a due canonical row so the cycle picks it up.
            now = datetime.now(UTC)
            due_info = CanonicalModelInfo(
                model_id="minimax-m3",
                status="partial",
                summary="seed",
                sparse=False,
                detail={},
                provenance={"sources": ["provider_catalog"]},
                conflicts={},
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now - timedelta(hours=2),
                last_refreshed_at=now - timedelta(hours=2),
                next_refresh_at=now - timedelta(minutes=1),
            )
            await service.repo.upsert_canonical(due_info)

            result = await service.refresh_due_models()
            assert result["total"] >= 1
            assert result["openrouter_attempted"] >= 1
            assert result["openrouter_matched"] >= 1
            assert result["openrouter_missed"] == (
                result["openrouter_attempted"] - result["openrouter_matched"]
            )

            # Verify OpenRouter observation persisted
            rows = await service.repo.list_compact_observations_for_model("minimax-m3")
            or_rows = [r for r in rows if r["source"] == "openrouter"]
            assert len(or_rows) == 1
            assert or_rows[0]["source_model_id"] == "minimax/minimax-m3"

            # Verify canonical detail has display_name + external context
            canonical = await service.repo.get_canonical("minimax-m3")
            assert canonical is not None
            assert canonical.detail["display_name"] == "MiniMax: MiniMax M3"
            assert canonical.detail["display_name_source"] == "openrouter"
            assert canonical.detail["external_ids"]["openrouter"] == (
                "minimax/minimax-m3"
            )
            assert canonical.detail["limits"]["external_context"] == 1_048_576
            assert canonical.detail["limits"]["external_output"] == 512_000
            benchmarks = canonical.detail["benchmarks"]
            assert isinstance(benchmarks, list)
            assert len(benchmarks) == 1
            assert benchmarks[0]["name"] == ("Artificial Analysis Intelligence Index")
            assert benchmarks[0]["score"] == 55.7
            assert benchmarks[0]["source"] == "artificial_analysis"
            assert "Public benchmark metadata unavailable" not in canonical.summary
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_refresh_due_models_records_openrouter_health_when_no_match(
        self,
    ) -> None:
        """``refresh_due_models`` records OpenRouter source health even
        when no alias matches the catalog."""
        from datetime import timedelta

        from eggpool.model_info.types import CanonicalModelInfo

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "no-match-model")

            client = _MockHttpClient(response=_or_payload({"id": "some-other/model"}))
            config = _make_config()
            cache = _make_cache("no-match-model")
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )

            now = datetime.now(UTC)
            due_info = CanonicalModelInfo(
                model_id="no-match-model",
                status="partial",
                summary="seed",
                sparse=False,
                detail={},
                provenance={"sources": ["provider_catalog"]},
                conflicts={},
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now - timedelta(hours=2),
                last_refreshed_at=now - timedelta(hours=2),
                next_refresh_at=now - timedelta(minutes=1),
            )
            await service.repo.upsert_canonical(due_info)

            await service.refresh_due_models()

            health = await service.repo.source_health_snapshot()
            assert "openrouter" in health
            assert health["openrouter"]["last_payload_count"] == 1
            assert health["openrouter"]["failure_count"] == 0
        finally:
            await db.disconnect()
