"""Opt-in live verification for public OpenRouter benchmark enrichment.

Run with ``EGGPOOL_LIVE_MODEL_INFO_TESTS=1``.  The default test suite keeps
network access disabled; this test intentionally exercises the same source,
matcher, SQLite repository, canonical builder, and models-table renderer used
by production.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest

from eggpool.catalog.cache import ModelCatalogCache
from eggpool.dashboard.render import render_models
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.presentation import compact_model_info_summary
from eggpool.model_info.service import ModelInfoService
from eggpool.models.config import (
    ModelInfoConfig,
    ModelInfoSourceConfig,
    ModelInfoSourcesConfig,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("EGGPOOL_LIVE_MODEL_INFO_TESTS") != "1",
        reason="live model-info tests disabled",
    ),
]


LIVE_PROVIDER_MODEL_IDS = (
    "minimax-m3",
    "kimi-k2.7-code",
    "glm-5.2",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "qwen3.7-max",
    "qwen3.7-plus",
)


def _cache_for_live_models() -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now = datetime.now(UTC).timestamp()
    for model_id in LIVE_PROVIDER_MODEL_IDS:
        entry = {
            "model_id": model_id,
            "display_name": model_id,
            "protocol": "openai",
            "capabilities": {"supports_tools": True},
            "source_metadata": {},
            "first_seen_at": now,
            "last_seen_at": now,
            "discovered_limits": {},
            "effective_limits": {
                "context_tokens": 128000,
                "input_tokens": 128000,
                "output_tokens": 16384,
            },
        }
        cache._models[model_id] = entry
        cache._provider_models[(model_id, "opencode-go")] = dict(entry)
    return cache


@pytest.mark.asyncio()
async def test_live_openrouter_benchmarks_survive_full_pipeline() -> None:
    """Live public scores match, persist, and reach dashboard HTML."""
    db = Database(path=":memory:")
    await db.connect()
    try:
        await MigrationRunner(db).run()
        cache = _cache_for_live_models()
        config = ModelInfoConfig(
            max_models_per_cycle=50,
            sources=ModelInfoSourcesConfig(
                openrouter=ModelInfoSourceConfig(enabled=True),
                artificial_analysis=ModelInfoSourceConfig(enabled=False),
                huggingface=ModelInfoSourceConfig(enabled=False),
            ),
        )
        async with httpx.AsyncClient(timeout=45) as client:
            service = ModelInfoService(config, db, cache, outbound_client=client)
            await service.load_cache()
            await service.reconcile_catalog_snapshot(reason="live-test")
            result = await service.refresh_due_models(force=True)

            assert result["total"] == len(LIVE_PROVIDER_MODEL_IDS)
            assert result["openrouter_matched"] >= 5

            benchmarked_infos = []
            for model_id in LIVE_PROVIDER_MODEL_IDS:
                info = await service.repo.get_canonical(model_id)
                assert info is not None, f"no canonical row for {model_id}"
                external_id = info.detail.get("external_ids", {}).get("openrouter")
                assert isinstance(external_id, str), (
                    f"{model_id} did not persist a real OpenRouter ID"
                )
                observation_rows = (
                    await service.repo.list_compact_observations_for_model(model_id)
                )
                openrouter_rows = [
                    row for row in observation_rows if row.get("source") == "openrouter"
                ]
                assert openrouter_rows
                if info.detail.get("benchmarks"):
                    benchmarked_infos.append(info)
                    assert openrouter_rows[0].get("benchmarks")
                    assert "Public benchmark metadata unavailable" not in (
                        info.summary or ""
                    )

            assert benchmarked_infos, "live OpenRouter catalog returned no scores"
            sample = benchmarked_infos[0]
            summary = compact_model_info_summary(sample)
            html = render_models(
                [
                    {
                        "model_id": sample.model_id,
                        "provider_id": "opencode-go",
                        "request_count": 1,
                    }
                ],
                model_info_map={sample.model_id: summary},
            )
            assert "benchmark-values" in html
            assert any(
                str(row.get("score")) in html
                for row in summary["benchmarks"]
                if row.get("score") is not None
            )
    finally:
        await db.disconnect()
