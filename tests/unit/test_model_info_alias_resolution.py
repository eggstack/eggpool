"""Tests for the Phase 1 OpenRouter polish alias-resolution determinism.

These tests pin the deterministic alias-candidate selection rules the
OpenRouter polish closeout plan introduces:

1. Duplicate case-variant aliases pointing to the same source id
   resolve successfully (no false ambiguity).
2. Exact-case alias rows take precedence over case-folded conflicting
   rows.
3. Folded-case conflicting aliases with no exact-case row produce a
   clear ``miss_reason = ambiguous_aliases`` diagnostic, not silent
   no-match.
4. When multiple aliases remain but only one is in the OpenRouter
   index, that one wins and the others are reported in diagnostics.
5. ``source_diagnostics`` exposes exact-case vs case-folded selection
   so operators can see which row the resolver chose.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.identity import (
    choose_alias_candidates,
    dedupe_alias_strings,
    resolve_openrouter_record,
)
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.service import ModelInfoService
from eggpool.model_info.types import SourceModelRecord
from eggpool.models.config import (
    ModelInfoAliasConfig,
    ModelInfoConfig,
    ModelInfoSourceConfig,
    ModelInfoSourcesConfig,
)


async def _run_migrations(db: Database) -> None:
    await MigrationRunner(db).run()


async def _seed_model(db: Database, model_id: str) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
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


def _make_config(
    *,
    aliases: list[ModelInfoAliasConfig] | None = None,
) -> ModelInfoConfig:
    sources = ModelInfoSourcesConfig(
        provider_catalog=ModelInfoSourceConfig(),
        openrouter=ModelInfoSourceConfig(enabled=True, ttl_seconds=60),
        artificial_analysis=ModelInfoSourceConfig(enabled=False),
        huggingface=ModelInfoSourceConfig(enabled=False),
    )
    return ModelInfoConfig(sources=sources, aliases=aliases or [])


def _make_cache(model_id: str) -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    entry: dict[str, Any] = {
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
    cache._models[model_id] = entry
    cache._provider_models[(model_id, "openai")] = dict(entry)
    return cache


class _MockHttpClient:
    def __init__(self, response: dict | Exception | None = None) -> None:
        self._response = response

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        if isinstance(self._response, Exception):
            raise self._response
        return httpx.Response(
            status_code=200,
            json=self._response,
            request=httpx.Request("GET", url),
        )


def _or_payload(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(entries)}


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


class TestChooseAliasCandidates:
    def test_prefers_exact_case_rows(self) -> None:
        rows = [
            {
                "model_id": "MiniMax-M3",
                "alias": "minimax/minimax-m3",
                "source": "openrouter",
                "provider_id": "opencode-go",
                "confidence": 1.0,
            },
            {
                "model_id": "minimax-m3",
                "alias": "minimax/minimax-m3",
                "source": "openrouter",
                "provider_id": "opencode-go",
                "confidence": 1.0,
            },
        ]
        out = choose_alias_candidates("minimax-m3", rows)
        assert len(out) == 1
        assert out[0]["model_id"] == "minimax-m3"
        assert out[0]["match_kind"] == "exact_case"

    def test_falls_back_to_case_folded_when_no_exact(self) -> None:
        rows = [
            {
                "model_id": "MiniMax-M3",
                "alias": "minimax/minimax-m3",
                "source": "openrouter",
                "provider_id": "opencode-go",
                "confidence": 1.0,
            }
        ]
        out = choose_alias_candidates("minimax-m3", rows)
        assert len(out) == 1
        assert out[0]["model_id"] == "MiniMax-M3"
        assert out[0]["match_kind"] == "case_folded"

    def test_empty_rows_returns_empty(self) -> None:
        assert choose_alias_candidates("minimax-m3", []) == []


class TestDedupeAliasStrings:
    def test_collapses_identical_aliases(self) -> None:
        rows = [
            {"alias": "minimax/minimax-m3", "model_id": "MiniMax-M3"},
            {"alias": "minimax/minimax-m3", "model_id": "minimax-m3"},
        ]
        assert dedupe_alias_strings(rows) == ["minimax/minimax-m3"]

    def test_preserves_distinct_aliases(self) -> None:
        rows = [
            {"alias": "minimax/minimax-m3", "model_id": "minimax-m3"},
            {"alias": "other/vendor-id", "model_id": "MiniMax-M3"},
        ]
        assert dedupe_alias_strings(rows) == [
            "minimax/minimax-m3",
            "other/vendor-id",
        ]

    def test_skips_non_string_aliases(self) -> None:
        rows = [
            {"alias": None, "model_id": "minimax-m3"},
            {"alias": 42, "model_id": "MiniMax-M3"},
            {"alias": "minimax/minimax-m3", "model_id": "minimax-m3"},
        ]
        assert dedupe_alias_strings(rows) == ["minimax/minimax-m3"]


# ---------------------------------------------------------------------------
# End-to-end resolver tests (DB-backed)
# ---------------------------------------------------------------------------


class TestDuplicateCaseAliasResolution:
    @pytest.mark.asyncio()
    async def test_duplicate_case_aliases_dedupe_to_single_match(self) -> None:
        """Two stored-case aliases pointing to the same OpenRouter id must
        resolve to one candidate, not be reported as ambiguous."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "MiniMax-M3")
            await _seed_model(db, "minimax-m3")
            repo = ModelInfoRepository(db)
            for stored_id in ("MiniMax-M3", "minimax-m3"):
                await repo.upsert_alias(
                    model_id=stored_id,
                    provider_id="opencode-go",
                    alias="minimax/minimax-m3",
                    source="openrouter",
                    confidence=1.0,
                )

            indexed = {"minimax/minimax-m3": _make_or_record("minimax/minimax-m3")}
            result = await resolve_openrouter_record("minimax-m3", repo, indexed)
            assert result is not None
            assert result.source_model_id == "minimax/minimax-m3"
        finally:
            await db.disconnect()


class TestExactCaseAliasWinsOverCaseFolded:
    @pytest.mark.asyncio()
    async def test_exact_case_alias_wins_over_case_folded_conflict(self) -> None:
        """When an exact-case row points to vendor-A and a folded row points
        to vendor-B, the exact-case row must win."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "MiniMax-M3")
            await _seed_model(db, "minimax-m3")
            repo = ModelInfoRepository(db)
            # exact-case row points to vendor-A
            await repo.upsert_alias(
                model_id="minimax-m3",
                provider_id="opencode-go",
                alias="vendor-a/minimax-m3",
                source="openrouter",
                confidence=1.0,
            )
            # case-folded row points to vendor-B
            await repo.upsert_alias(
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                alias="vendor-b/minimax-m3",
                source="openrouter",
                confidence=1.0,
            )

            indexed = {
                "vendor-a/minimax-m3": _make_or_record("vendor-a/minimax-m3"),
                "vendor-b/minimax-m3": _make_or_record("vendor-b/minimax-m3"),
            }
            result = await resolve_openrouter_record("minimax-m3", repo, indexed)
            assert result is not None
            assert result.source_model_id == "vendor-a/minimax-m3"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_folded_conflicting_aliases_are_ambiguous_without_exact(
        self,
    ) -> None:
        """When two folded-case rows point to different sources and no
        exact-case row exists, the resolver must return ``None`` so the
        caller can report ``miss_reason = ambiguous_aliases``."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "MiniMax-M3")
            await _seed_model(db, "MINIMAX-M3")
            repo = ModelInfoRepository(db)
            await repo.upsert_alias(
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                alias="minimax/minimax-m3",
                source="openrouter",
                confidence=1.0,
            )
            await repo.upsert_alias(
                model_id="MINIMAX-M3",
                provider_id="opencode-go",
                alias="other/vendor-id",
                source="openrouter",
                confidence=1.0,
            )

            indexed = {
                "minimax/minimax-m3": _make_or_record("minimax/minimax-m3"),
                "other/vendor-id": _make_or_record("other/vendor-id"),
            }
            # No exact-case row for the requested id; the two folded
            # rows both resolve to distinct indexed records, which is
            # the ambiguity case.
            result = await resolve_openrouter_record("minimax-m3", repo, indexed)
            assert result is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_multiple_aliases_only_one_in_catalog_can_match(self) -> None:
        """When multiple aliases exist but only one appears in the
        indexed catalog, that one wins (the others are ignored)."""
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
                alias="vendor-a/minimax-m3",
                source="openrouter",
                confidence=1.0,
            )
            await repo.upsert_alias(
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                alias="vendor-b/minimax-m3",
                source="openrouter",
                confidence=1.0,
            )

            # Indexed catalog only carries vendor-a; vendor-b is unknown.
            indexed = {"vendor-a/minimax-m3": _make_or_record("vendor-a/minimax-m3")}
            result = await resolve_openrouter_record("minimax-m3", repo, indexed)
            assert result is not None
            assert result.source_model_id == "vendor-a/minimax-m3"
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Diagnostics tests (service.py integration)
# ---------------------------------------------------------------------------


class TestSourceDiagnosticsAliasFields:
    @pytest.mark.asyncio()
    async def test_diagnostics_exposes_alias_rows_and_selection(self) -> None:
        """``source_diagnostics.openrouter`` must expose ``alias_rows`` and
        ``alias_selection`` so operators can see exact-case vs case-folded
        selection."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "minimax-m3")

            client = _MockHttpClient(response=_or_payload({"id": "minimax/minimax-m3"}))
            config = _make_config(
                aliases=[
                    ModelInfoAliasConfig(
                        provider_id="opencode-go",
                        model_id="minimax-m3",
                        source="openrouter",
                        source_model_id="minimax/minimax-m3",
                        confidence="exact",
                    )
                ]
            )
            cache = _make_cache("minimax-m3")
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )

            result = await service.refresh_model_info("minimax-m3", force=True)
            diag = result["source_diagnostics"]["openrouter"]
            assert diag["alias_selection"] == "exact_case"
            assert diag["alias_candidates"] == ["minimax/minimax-m3"]
            assert len(diag["alias_rows"]) == 1
            assert diag["alias_rows"][0]["match_kind"] == "exact_case"
            assert diag["alias_rows"][0]["alias"] == "minimax/minimax-m3"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_diagnostics_reports_case_folded_selection(self) -> None:
        """When only a case-folded alias row exists, ``alias_selection``
        must be ``case_folded``."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "MiniMax-M3")
            await _seed_model(db, "minimax-m3")

            client = _MockHttpClient(response=_or_payload({"id": "minimax/minimax-m3"}))
            config = _make_config()
            cache = _make_cache("minimax-m3")
            service = ModelInfoService(
                config=config, db=db, catalog=cache, outbound_client=client
            )
            # Inject a case-variant alias row after startup.
            repo = ModelInfoRepository(db)
            await repo.upsert_alias(
                model_id="MiniMax-M3",
                provider_id="opencode-go",
                alias="minimax/minimax-m3",
                source="openrouter",
                confidence=1.0,
            )

            result = await service.refresh_model_info("minimax-m3", force=True)
            diag = result["source_diagnostics"]["openrouter"]
            assert diag["alias_selection"] == "case_folded"
            assert all(row["match_kind"] == "case_folded" for row in diag["alias_rows"])
        finally:
            await db.disconnect()
