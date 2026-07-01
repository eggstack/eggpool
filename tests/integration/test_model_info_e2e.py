"""End-to-end integration test for the model-info detail pipeline.

This test exercises the full path from the local provider catalog
through the model-info service and renders the model detail page,
using a 33-model fixture sourced from the live OpenRouter ``/models``
endpoint captured at development time. The HTTP boundary is mocked
so the test is fully deterministic — no network calls.

The user's report (matched by the fixture shape and the operator's
production topology): "our model page on dashboard correctly shows
all models, but the model details are completely broken. we have
access to 33 models for testing on another system, and not one has
correctly pulled info from API and landed in our details page."

Before fix: ALL 33 detail pages return ``Model info not available``
because OpenRouter identifiers are always ``<vendor>/<model>`` while
Eggpool catalog rows are unsuffixed base IDs, and the existing
identity resolver refuses to match without a configured alias.

This file acts as a regression test for the fix; without the fix,
``test_every_model_detail_populates`` fails for all 33 models.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from eggpool.app import create_app
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.service import ModelInfoService
from eggpool.model_info.sources.openrouter import OpenRouterModelInfoSource
from eggpool.models.config import (
    AppConfig,
    ModelInfoConfig,
    ModelInfoSourceConfig,
    ModelInfoSourcesConfig,
)
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from fastapi import FastAPI

    from eggpool.model_info.repository import ModelInfoRepository
    from eggpool.model_info.types import CanonicalModelInfo


FIXTURE_PATH = Path(__file__).parent / "_openrouter_real_fixture.json"


def _load_real_openrouter_fixture() -> list[dict[str, object]]:
    """Return the captured live OpenRouter response payload."""
    payload = json.loads(FIXTURE_PATH.read_text())
    data = payload.get("data")
    assert isinstance(data, list) and len(data) >= 33, (
        f"real OpenRouter fixture should carry at least 33 entries, got "
        f"{len(data) if isinstance(data, list) else 'non-list'}"
    )
    return data


def _base_model_id_from_or(or_id: str) -> str:
    """``openai/gpt-4o`` -> ``gpt-4o``. OpenRouter uses
    ``<vendor>/<model>``; Eggpool catalogs models by the unsuffixed
    ``<model>``. Strip the **first** segment so ``gpt-4o`` resolves
    to the right catalog row."""
    if "/" in or_id:
        return or_id.split("/", 1)[1]
    return or_id


def _make_cache_with_real_models() -> tuple[ModelCatalogCache, list[str]]:
    """Build an in-memory catalog mirroring the user's 33-model test set."""
    fixture = _load_real_openrouter_fixture()
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    base_ids_ordered: list[str] = []
    seen: set[str] = set()
    for entry in fixture:
        or_id = entry["id"]
        assert isinstance(or_id, str)
        base_id = _base_model_id_from_or(or_id)
        if base_id in seen:
            continue
        seen.add(base_id)
        vendor = or_id.split("/", 1)[0]
        cache._models[base_id] = {
            "model_id": base_id,
            "display_name": entry.get("name") or base_id,
            "protocol": _vendor_default_protocol(vendor),
            "capabilities": {"supports_tools": True},
            "source_metadata": {"source": "captured_openrouter"},
            "first_seen_at": now_ts,
            "last_seen_at": now_ts,
            "discovered_limits": {},
            "effective_limits": {
                "context_tokens": entry.get("context_length") or 8192,
                "input_tokens": entry.get("context_length") or 8192,
                "output_tokens": 8192,
                "enforce": True,
            },
        }
        cache._provider_models[(base_id, vendor)] = dict(cache._models[base_id])
        base_ids_ordered.append(base_id)
    return cache, base_ids_ordered


def _vendor_default_protocol(vendor: str) -> str:
    if vendor in {"anthropic"}:
        return "anthropic"
    return "openai"


def _build_config(tmp_path: Path) -> AppConfig:
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": str(tmp_path / "model_info_e2e.sqlite3")},
            "upstream": {"base_url": "https://upstream.example.com"},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "acct-a", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {
                "enabled": True,
                "public": True,
                "refresh_interval_s": 60,
            },
        }
    )


async def _run_migrations(db: Database) -> None:
    await MigrationRunner(db).run()


async def _seed_models(db: Database, model_ids: list[str]) -> None:
    async with db.transaction():
        for model_id in model_ids:
            await db.execute_write(
                "INSERT OR IGNORE INTO models (model_id, display_name) VALUES (?, ?)",
                (model_id, model_id),
            )


class _StaticHttpClient:
    """Mock HTTP client returning the captured live OpenRouter payload."""

    def __init__(self, response: dict[str, object]) -> None:
        self._response = response
        self.calls: list[str] = []

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append(url)
        return httpx.Response(
            status_code=200,
            json=self._response,
            request=httpx.Request("GET", url),
        )


@pytest_asyncio.fixture()
async def app_with_real_models_async(
    tmp_path: Path,
) -> tuple[FastAPI, list[str]]:
    fixture = _load_real_openrouter_fixture()
    cache, base_ids = _make_cache_with_real_models()

    config = _build_config(tmp_path)
    application = create_app(config)
    db = Database(path=config.database.path)

    await db.connect()
    await _run_migrations(db)
    await _seed_models(db, base_ids)
    application.state.db = db
    application.state.stats_db = db
    application.state.stats = StatsService(db)

    sources_cfg = ModelInfoSourcesConfig(
        provider_catalog=ModelInfoSourceConfig(),
        openrouter=ModelInfoSourceConfig(enabled=True),
        artificial_analysis=ModelInfoSourceConfig(enabled=False),
        huggingface=ModelInfoSourceConfig(enabled=False),
    )
    mi_config = ModelInfoConfig(sources=sources_cfg)
    http = _StaticHttpClient({"data": fixture})

    or_source = OpenRouterModelInfoSource(
        config=sources_cfg.openrouter,
        client=http,
    )

    service = ModelInfoService(mi_config, db, cache, outbound_client=http)
    service._openrouter_source = or_source  # noqa: SLF001 - test wiring
    application.state.model_info = service

    await service.refresh_provider_catalog_observations()
    await service.reconcile_catalog_snapshot(reason="test_e2e")
    # The startup reconciliation only writes provider-catalog data;
    # the periodic refresh (or a manual /v1/model-info/refresh) is what
    # actually pulls OpenRouter observations and lands them on the
    # canonical row.  ``refresh_due_models`` only picks up rows whose
    # ``next_refresh_at`` has passed; after a fresh reconcile every
    # row carries a 12h partial_ttl, so the test must use the
    # ``force=True`` path the dashboard's "Refresh now" button and
    # the API's ``POST /v1/model-info/refresh?force=true`` use.
    for mid in base_ids:
        await service.refresh_model_info(mid, force=True)

    try:
        yield application, base_ids
    finally:
        await db.disconnect()


@pytest.mark.asyncio()
async def test_every_model_detail_populates(
    app_with_real_models_async: tuple[FastAPI, list[str]],
) -> None:
    """Every model in the captured 33-model fixture must populate its
    model-info detail page from the OpenRouter response.

    Uses the live ``resolve_openrouter_record`` path (no configured
    aliases), so this test fails for every model that does not match
    by ``<vendor>/<model>`` -> ``<model>`` alias translation.
    """
    application, base_ids = app_with_real_models_async
    client = TestClient(application)

    fixture = _load_real_openrouter_fixture()
    or_index = {entry["id"]: entry for entry in fixture}

    mi_service = application.state.model_info

    failures: list[str] = []
    for base_id in base_ids:
        info: CanonicalModelInfo | None = await mi_service.repo.get_canonical(base_id)
        if info is None:
            failures.append(f"{base_id}: no canonical row persisted")
            continue

        limits = info.detail.get("limits", {}) if isinstance(info.detail, dict) else {}
        if not limits.get("effective_context"):
            failures.append(
                f"{base_id}: missing effective_context from provider catalog"
            )
        if not limits.get("external_context"):
            failures.append(
                f"{base_id}: missing external_context from OpenRouter "
                "(resolve_openrouter_record returned None without an alias)"
            )
        external_ids = (
            info.detail.get("external_ids", {}) if isinstance(info.detail, dict) else {}
        )
        if "openrouter" not in external_ids:
            failures.append(
                f"{base_id}: missing openrouter external_id "
                "(resolve_openrouter_record returned None without an alias)"
            )
        else:
            ext = external_ids["openrouter"]
            assert isinstance(ext, str)
            if ext not in or_index:
                failures.append(f"{base_id}: external_id {ext} not in fixture")

        from urllib.parse import quote

        response = client.get(f"/models/{quote(base_id, safe='')}")
        assert response.status_code == 200, (base_id, response.status_code)
        body = response.text
        if "Model info not available" in body:
            failures.append(f"{base_id}: detail route renders empty state")

    assert not failures, (
        f"{len(failures)}/{len(base_ids)} models failed to populate:\n"
        + "\n".join(failures[:20])
    )


@pytest.mark.asyncio()
async def test_external_ids_match_vendor_prefix(
    app_with_real_models_async: tuple[FastAPI, list[str]],
) -> None:
    """Every detail must record its ``<vendor>/<model>`` OpenRouter ID."""
    application, base_ids = app_with_real_models_async
    mi_service = application.state.model_info
    repo: ModelInfoRepository = mi_service.repo

    matched: dict[str, str] = {}
    for mid in base_ids:
        row = await repo.get_canonical(mid)
        if row is None:
            continue
        ext = row.detail.get("external_ids", {}) if isinstance(row.detail, dict) else {}
        or_id = ext.get("openrouter")
        if isinstance(or_id, str):
            matched[mid] = or_id

    assert len(matched) == len(base_ids), (
        f"only {len(matched)}/{len(base_ids)} models resolved to an "
        f"OpenRouter external ID: missing="
        f"{[m for m in base_ids if m not in matched][:10]}"
    )


@pytest.mark.asyncio()
async def test_detail_page_renders_provider_and_openrouter_context(
    app_with_real_models_async: tuple[FastAPI, list[str]],
) -> None:
    """The rendered detail page must surface both provider catalog
    limits and OpenRouter external context."""
    application, _ = app_with_real_models_async
    client = TestClient(application)
    from urllib.parse import quote

    fixture = _load_real_openrouter_fixture()
    first_or_id = fixture[0]["id"]
    first_base_id = _base_model_id_from_or(first_or_id)
    assert isinstance(first_or_id, str)
    response = client.get(f"/models/{quote(first_base_id, safe='')}")
    assert response.status_code == 200
    body = response.text
    assert "Model info not available" not in body
    assert "Summary" in body
    assert "Effective ctx" in body or "External ctx" in body
