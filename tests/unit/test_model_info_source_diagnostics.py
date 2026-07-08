"""Tests for source-level diagnostics in the model-info subsystem.

Covers:

1. ``/api/model-info/sources`` always lists every configured source,
   including sources that have never been written to
   ``model_info_source_health`` (e.g., disabled AA).
2. Disabled / missing-key / unconstructed reasons are surfaced as
   stable, machine-readable labels.
3. AA matching now uses the tiered resolver instead of exact-only.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.service import ModelInfoService
from eggpool.models.config import (
    ModelInfoConfig,
    ModelInfoSourceConfig,
    ModelInfoSourcesConfig,
)


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


def _make_cache() -> ModelCatalogCache:
    return ModelCatalogCache()


class _MockHttpClient:
    def __init__(self, payload: dict | Exception | None) -> None:
        self._payload = payload
        self.call_count = 0

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        self.call_count += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return httpx.Response(
            status_code=200,
            json=self._payload if isinstance(self._payload, dict) else {},
            request=httpx.Request("GET", url),
        )


class TestSourceDiagnosticsListsAllConfiguredSources:
    """The endpoint must include ``provider_catalog``, ``openrouter``,
    ``artificial_analysis``, and ``huggingface`` regardless of whether
    they have ever been written to ``model_info_source_health``."""

    @pytest.mark.asyncio()
    async def test_disabled_aa_appears_with_reason_disabled(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            cache = _make_cache()
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                    # AA stays at default (disabled, no key).
                )
            )
            service = ModelInfoService(
                config, db, cache, outbound_client=_MockHttpClient({})
            )

            diagnostics = service.source_diagnostics()
            assert "provider_catalog" in diagnostics
            assert "openrouter" in diagnostics
            assert "artificial_analysis" in diagnostics
            assert "huggingface" in diagnostics

            aa = diagnostics["artificial_analysis"]
            assert aa["configured"] is True
            assert aa["enabled"] is False
            assert aa["constructed"] is False
            assert aa["reason"] == "disabled"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_enabled_aa_without_api_key_reports_missing_key(self) -> None:
        """Even without a configured key, an enabled-but-unconstructed
        source must surface ``reason`` explicitly."""

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            cache = _make_cache()
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                    artificial_analysis=ModelInfoSourceConfig(
                        enabled=True,
                        api_key_env="DEFINITELY_NOT_SET_AA_KEY",
                    ),
                )
            )
            service = ModelInfoService(
                config, db, cache, outbound_client=_MockHttpClient({})
            )

            diagnostics = service.source_diagnostics()
            aa = diagnostics["artificial_analysis"]
            assert aa["enabled"] is True
            assert aa["configured"] is True
            assert aa["api_key_present"] is False
            assert aa["reason"] == "missing_api_key"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_enabled_aa_with_inline_key_reports_ready(self) -> None:
        """An inline api_key is treated as ``api_key_present=True``."""

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            cache = _make_cache()
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                    artificial_analysis=ModelInfoSourceConfig(
                        enabled=True,
                        api_key="abc123",
                    ),
                )
            )
            service = ModelInfoService(
                config, db, cache, outbound_client=_MockHttpClient({})
            )
            diagnostics = service.source_diagnostics()
            aa = diagnostics["artificial_analysis"]
            assert aa["api_key_present"] is True
            assert aa["requires_api_key"] is True
        finally:
            await db.disconnect()


class TestAaMatchingUsesTieredResolver:
    """AA matching should now run through the tiered resolver the same
    way OpenRouter does.  Verified at the service level."""

    @pytest.mark.asyncio()
    async def test_aa_resolves_normalized_alias(self) -> None:
        """`MiniMax-M3` -> `minimaxm3` normalized matches
        `minimax/MiniMax: MiniMax M3` AA source id."""

        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            async with db.transaction():
                await db.execute_write(
                    "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
                    ("minimax-m3", "minimax-m3"),
                )

            cache = _make_cache()
            # Provide AA payload that has a single record matching via
            # the normalized_exact tier.
            aa_payload = {
                "data": [
                    {
                        "id": "minimax/MiniMax: MiniMax M3",
                        "name": "MiniMax M3",
                        "intelligence_index": 95.0,
                        "speed_index": 88.0,
                        "quality_index": 92.0,
                    }
                ]
            }
            client = _MockHttpClient(aa_payload)
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=False),
                    artificial_analysis=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            now = datetime.now(UTC)
            await service.repo.upsert_canonical(_sparse_row("minimax-m3", now))

            await service.load_cache()
            await service.refresh_model_info("minimax-m3", force=True)
            canonical = await service.repo.get_canonical("minimax-m3")
            assert canonical is not None
            detail = canonical.detail
            assert isinstance(detail, dict)
            ext = detail.get("external_ids", {})
            assert ext.get("artificial_analysis") == "minimax/MiniMax: MiniMax M3"
            provenance = canonical.provenance
            assert "artificial_analysis" in provenance.get("sources", [])
        finally:
            await db.disconnect()


def _sparse_row(model_id: str, now: datetime):
    from eggpool.model_info.types import CanonicalModelInfo

    return CanonicalModelInfo(
        model_id=model_id,
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
