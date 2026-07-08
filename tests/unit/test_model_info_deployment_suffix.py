"""Service-level test for deployment-suffix tier on live-shaped highspeed
data.

Pins the acceptance criteria from plan Phase 2: highspeed provider-catalog
rows should resolve to their base OpenRouter IDs through the
``deployment_suffix_normalized_exact`` tier, persist a match_evidence
row with the new match_method, and move the canonical status from
``sparse_new`` toward ``partial``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.normalization import (
    generate_deployment_suffix_variants,
    normalize_model_key,
)
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


FIXTURES_DIR = Path(__file__).parents[1] / "fixtures" / "model_info"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


class _OpenRouterMockClient:
    """Minimal mock httpx-like async client that returns the OpenRouter
    highspeed fixture for any GET.  Mirrors the pattern used in
    ``test_model_info_fresh_db_service.py``."""

    def __init__(self, or_payload: dict | Exception) -> None:
        self._payload = or_payload
        self.call_count = 0

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        self.call_count += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return httpx.Response(
            status_code=200,
            json=self._payload,
            request=httpx.Request("GET", url),
        )


def _make_provider_catalog_cache(
    model_ids: list[str],
    *,
    provider_id: str = "minimax",
) -> ModelCatalogCache:
    """Build a catalog with the given highspeed model IDs."""
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    for mid in model_ids:
        cache._models[mid] = {
            "model_id": mid,
            "display_name": mid,
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
        cache._provider_models[(mid, provider_id)] = dict(cache._models[mid])
    return cache


def _make_or_payload(records: dict | list) -> dict:
    if "data" in records:
        return records
    return {"data": records if isinstance(records, list) else [records]}


async def _seed_sparse(service: ModelInfoService, model_id: str) -> None:
    now = datetime.now(UTC)
    sparse_row = CanonicalModelInfo(
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
        next_refresh_at=now - timedelta(minutes=1),
    )
    await service.repo.upsert_canonical(sparse_row)


class TestHighspeedMinimaxM27ResolvesToBase:
    @pytest.mark.asyncio()
    async def test_full_chain_enriches_to_base(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "MiniMax-M2.7-highspeed")

            cache = _make_provider_catalog_cache(["MiniMax-M2.7-highspeed"])
            or_payload = _load_json(
                FIXTURES_DIR / "openrouter_minimax_highspeed_sample.json"
            )
            client = _OpenRouterMockClient(or_payload)

            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            await _seed_sparse(service, "MiniMax-M2.7-highspeed")

            await service.load_cache()
            await service.reconcile_catalog_snapshot()

            result = await service.refresh_model_info(
                "MiniMax-M2.7-highspeed", force=True
            )
            assert result["errors"] == 0

            info_after = await service.repo.get_canonical("MiniMax-M2.7-highspeed")
            assert info_after is not None
            detail = info_after.detail
            assert isinstance(detail, dict)
            external_ids = detail.get("external_ids", {})
            assert external_ids.get("openrouter") == "minimax/minimax-m2.7", (
                f"expected external_ids.openrouter=minimax/minimax-m2.7, "
                f"got detail={detail!r}"
            )

            provenance = info_after.provenance
            assert "openrouter" in provenance.get("sources", [])
            assert "provider_catalog" in provenance.get("sources", [])

            evidence = await service.repo.list_match_evidence(
                "MiniMax-M2.7-highspeed", source="openrouter"
            )
            assert len(evidence) >= 1
            row = evidence[0]
            assert row["match_method"] == "deployment_suffix_normalized_exact"
            assert float(row["confidence"]) > 0.0
            assert row["alias"] == "minimax/minimax-m2.7"
        finally:
            await db.disconnect()


class TestHighspeedVariantsMoveStatusFromSparseToPartial:
    @pytest.mark.asyncio()
    async def test_status_advances_after_deployment_suffix_match(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "MiniMax-M2.5-highspeed")

            cache = _make_provider_catalog_cache(["MiniMax-M2.5-highspeed"])
            or_fixture = _load_json(
                FIXTURES_DIR / "openrouter_minimax_highspeed_sample.json"
            )
            client = _OpenRouterMockClient(or_fixture)

            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            await _seed_sparse(service, "MiniMax-M2.5-highspeed")

            await service.load_cache()
            await service.reconcile_catalog_snapshot()
            await service.refresh_model_info("MiniMax-M2.5-highspeed", force=True)

            refreshed = await service.repo.get_canonical("MiniMax-M2.5-highspeed")
            assert refreshed is not None
            assert refreshed.sparse is False
            assert refreshed.status in ("partial", "fresh", "conflicting")
            assert (
                refreshed.detail.get("external_ids", {}).get("openrouter")
                == "minimax/minimax-m2.5"
            )
        finally:
            await db.disconnect()


class TestHighspeedM21Fixture:
    """All three M-x highspeed variants should resolve to their base
    sources when run against the shared fixture."""

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        ("variant_id", "expected_openrouter_id"),
        [
            ("MiniMax-M2.1-highspeed", "minimax/minimax-m2.1"),
            ("MiniMax-M2.5-highspeed", "minimax/minimax-m2.5"),
            ("MiniMax-M2.7-highspeed", "minimax/minimax-m2.7"),
        ],
    )
    async def test_each_variant(
        self, variant_id: str, expected_openrouter_id: str
    ) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, variant_id)

            cache = _make_provider_catalog_cache([variant_id])
            or_fixture = _load_json(
                FIXTURES_DIR / "openrouter_minimax_highspeed_sample.json"
            )
            client = _OpenRouterMockClient(or_fixture)

            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            await _seed_sparse(service, variant_id)
            await service.load_cache()
            await service.reconcile_catalog_snapshot()
            await service.refresh_model_info(variant_id, force=True)

            refreshed = await service.repo.get_canonical(variant_id)
            assert refreshed is not None
            assert (
                refreshed.detail.get("external_ids", {}).get("openrouter")
                == expected_openrouter_id
            )
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# Separator-agnostic deployment suffix stripping
# ---------------------------------------------------------------------------


class TestDeploymentSuffixSeparatorAgnostic:
    """Verify that deployment-suffix stripping works across all supported
    separator families: hyphen, underscore, dot, space, colon, slash."""

    @pytest.mark.parametrize(
        ("raw", "expected_base"),
        [
            ("MiniMax-M2.7-highspeed", "MiniMax-M2.7"),
            ("MiniMax_M2.7_highspeed", "MiniMax_M2.7"),
            ("MiniMax M2.7 highspeed", "MiniMax M2.7"),
            ("MiniMax.M2.7.highspeed", "MiniMax.M2.7"),
            ("MiniMax:M2.7:highspeed", "MiniMax:M2.7"),
            ("minimax/MiniMax-M2.7-highspeed", "minimax/MiniMax-M2.7"),
            ("MiniMax-M2.7-fast", "MiniMax-M2.7"),
            ("MiniMax_M2.7_turbo", "MiniMax_M2.7"),
            ("MiniMax M2.7 speed", "MiniMax M2.7"),
        ],
    )
    def test_deployment_suffix_variants_strip_all_supported_separators(
        self, raw: str, expected_base: str
    ) -> None:
        variants = generate_deployment_suffix_variants(raw)
        assert len(variants) == 2
        assert variants[0] == raw
        assert variants[1] == expected_base

    @pytest.mark.parametrize(
        ("raw", "expected_norm"),
        [
            ("MiniMax-M2.7-highspeed", "minimaxm27"),
            ("MiniMax_M2.7_highspeed", "minimaxm27"),
            ("MiniMax M2.7 highspeed", "minimaxm27"),
            ("MiniMax.M2.7.highspeed", "minimaxm27"),
            ("MiniMax:M2.7:highspeed", "minimaxm27"),
            ("minimax/MiniMax-M2.7-fast", "minimaxminimaxm27"),
        ],
    )
    def test_deployment_suffix_variants_normalize_to_base(
        self, raw: str, expected_norm: str
    ) -> None:
        variants = generate_deployment_suffix_variants(raw)
        assert len(variants) == 2
        assert normalize_model_key(variants[1]) == expected_norm

    def test_namespace_preservation_with_separator_variants(self) -> None:
        assert generate_deployment_suffix_variants("minimax/MiniMax-M2.7-highspeed")[
            1
        ].startswith("minimax/")
        assert generate_deployment_suffix_variants("minimax/MiniMax_M2.7_highspeed")[
            1
        ].startswith("minimax/")
        assert generate_deployment_suffix_variants("minimax/MiniMax.M2.7.highspeed")[
            1
        ].startswith("minimax/")

    @pytest.mark.parametrize(
        "raw",
        [
            "MiniMax-M2.7-pro",
            "mimo-v2.5-pro",
            "qwen3.7-plus",
            "kimi-k2.7-code",
            "hy3-preview",
            "deepseek-v4-flash",
            "gpt-5-mini-turbo",
            "highspeed",
        ],
    )
    def test_deployment_suffix_variants_do_not_strip_semantic_or_unanchored_names(
        self, raw: str
    ) -> None:
        assert generate_deployment_suffix_variants(raw) == (raw,)

    def test_single_separator_mixed_with_namespace(self) -> None:
        """Namespace segment uses slash but model segment uses underscore."""
        result = generate_deployment_suffix_variants("minimax/MiniMax_M2.7_highspeed")
        assert result == (
            "minimax/MiniMax_M2.7_highspeed",
            "minimax/MiniMax_M2.7",
        )


# ---------------------------------------------------------------------------
# Deployment-suffix match-evidence diagnostics
# ---------------------------------------------------------------------------


class TestDeploymentSuffixMatchEvidenceDiagnostics:
    """Verify that match evidence diagnostics include raw_candidate
    and stripped_suffix fields after a deployment-suffix match."""

    @pytest.mark.asyncio()
    async def test_diagnostics_include_raw_candidate_and_suffix(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "MiniMax-M2.7-highspeed")

            cache = _make_provider_catalog_cache(["MiniMax-M2.7-highspeed"])
            or_payload = _load_json(
                FIXTURES_DIR / "openrouter_minimax_highspeed_sample.json"
            )
            client = _OpenRouterMockClient(or_payload)

            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            await _seed_sparse(service, "MiniMax-M2.7-highspeed")
            await service.load_cache()
            await service.reconcile_catalog_snapshot()
            await service.refresh_model_info("MiniMax-M2.7-highspeed", force=True)

            evidence = await service.repo.list_match_evidence(
                "MiniMax-M2.7-highspeed", source="openrouter"
            )
            assert len(evidence) >= 1
            row = evidence[0]
            assert row["match_method"] == "deployment_suffix_normalized_exact"
            diag = json.loads(row["diagnostics_json"])
            assert diag["raw_candidate"] == "MiniMax-M2.7-highspeed"
            assert diag["stripped_suffix"] == "highspeed"
            assert diag["base_variant"] == "MiniMax-M2.7"
        finally:
            await db.disconnect()
