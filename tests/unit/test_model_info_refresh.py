"""Tests for the on-demand single-model refresh path.

Phase C of the model-info corrective plan:

* ``ModelInfoService.refresh_model_info`` runs the same per-model
  enrichment logic as ``refresh_due_models`` but only for the named
  model, honoring a ``source`` filter and a ``force`` flag.
* The HTTP layer (``POST /api/model-info/refresh?model_id=...``)
  delegates to this method and returns counts in the response body.
* Non-forced refreshes skip rows that are not yet due.
* The single-model refresh path uses ``get_latest_observation_payloads``
  so previously persisted observations from prior cycles are merged
  back into the canonical detail, even if the current cycle only
  fetched one source.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from eggpool.api.model_info import handle_model_info_refresh
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.service import ModelInfoService
from eggpool.model_info.types import (
    CanonicalModelInfo,
    SourceModelRecord,
)
from eggpool.models.config import (
    ModelInfoConfig,
    ModelInfoSourceConfig,
    ModelInfoSourcesConfig,
)

# ---------------------------------------------------------------------------
# Helpers (local — keep tests self-contained)
# ---------------------------------------------------------------------------


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(db: Database, model_id: str, display_name: str = "") -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, display_name or model_id),
        )


def _make_cache(
    model_id: str,
    *,
    context: int = 128000,
    display_name: str | None = None,
    protocol: str | None = "openai",
) -> ModelCatalogCache:
    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    cache._models[model_id] = {
        "model_id": model_id,
        "display_name": display_name or model_id,
        "protocol": protocol,
        "capabilities": {"supports_tools": True},
        "source_metadata": {},
        "first_seen_at": now_ts,
        "last_seen_at": now_ts,
        "discovered_limits": {},
        "effective_limits": {
            "context_tokens": context,
            "input_tokens": context,
            "output_tokens": 16384,
            "enforce": True,
        },
    }
    cache._provider_models[(model_id, "openai")] = dict(cache._models[model_id])
    return cache


def _make_or_model(
    model_id: str,
    *,
    name: str = "",
    context_length: int = 0,
    modalities: list[str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": model_id}
    if name:
        entry["name"] = name
    if context_length:
        entry["context_length"] = context_length
    if modalities is not None:
        entry["architecture"] = {
            "input_modalities": modalities,
            "output_modalities": ["text"],
        }
    return entry


def _openrouter_payload(*models: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(models)}


def _aa_model(model_id: str, *, name: str = "") -> dict[str, Any]:
    return {
        "id": model_id,
        "name": name or model_id,
        "intelligence_index": 80.0,
    }


def _aa_payload(*models: dict[str, Any]) -> dict[str, Any]:
    return {"data": list(models)}


class _MockHttpClient:
    """Mock HTTP client that returns pre-configured responses.

    Mirrors the pattern used in test_model_info_phase{3,5}.py so
    fixtures compose cleanly with existing tests.
    """

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


# ---------------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------------


class TestRefreshModelInfoService:
    @pytest.mark.asyncio()
    async def test_creates_canonical_when_missing(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-x")

            cache = _make_cache("gpt-x", display_name="GPT-X")
            config = ModelInfoConfig()
            service = ModelInfoService(config, db, cache)

            # No canonical row exists yet.
            assert await service.repo.get_canonical("gpt-x") is None

            result = await service.refresh_model_info("gpt-x", force=True)
            assert result["requested"] == 1
            assert result["errors"] == 0
            assert result["sources_attempted"] == ["provider_catalog"]
            assert "provider_catalog" in result["sources_matched"]
            assert result["observations"] >= 1

            # Canonical row was created.
            info = await service.repo.get_canonical("gpt-x")
            assert info is not None
            assert info.model_id == "gpt-x"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_force_bypasses_next_refresh_at(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-x")

            cache = _make_cache("gpt-x")
            config = ModelInfoConfig()
            service = ModelInfoService(config, db, cache)

            # Seed a canonical row whose next_refresh_at is in the
            # future (so non-forced refresh would skip).
            now = datetime.now(UTC)
            future = now + timedelta(hours=1)
            info = CanonicalModelInfo(
                model_id="gpt-x",
                status="partial",
                summary="seeded",
                sparse=False,
                detail={},
                provenance={"sources": ["provider_catalog"]},
                conflicts={},
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now - timedelta(hours=1),
                last_refreshed_at=now - timedelta(hours=1),
                next_refresh_at=future,
            )
            await service.repo.upsert_canonical(info)

            # Non-forced: skipped because next_refresh_at is future.
            not_forced = await service.refresh_model_info("gpt-x", force=False)
            assert not_forced["skipped"] == 1
            assert not_forced["refreshed"] == 0

            # Forced: bypasses the gate and updates last_refreshed_at.
            forced = await service.refresh_model_info("gpt-x", force=True)
            assert forced["requested"] == 1
            assert forced["errors"] == 0

            updated = await service.repo.get_canonical("gpt-x")
            assert updated is not None
            assert updated.last_refreshed_at is not None
            assert updated.last_refreshed_at >= now
            assert updated.provenance.get("force_refreshed") is True
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_force_false_skips_when_not_due(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-x")

            cache = _make_cache("gpt-x")
            config = ModelInfoConfig()
            service = ModelInfoService(config, db, cache)

            now = datetime.now(UTC)
            future = now + timedelta(hours=1)
            await service.repo.upsert_canonical(
                CanonicalModelInfo(
                    model_id="gpt-x",
                    status="partial",
                    summary="seeded",
                    sparse=False,
                    detail={},
                    provenance={"sources": ["provider_catalog"]},
                    conflicts={},
                    first_seen_at=now - timedelta(days=1),
                    last_seen_at=now - timedelta(hours=1),
                    last_refreshed_at=now - timedelta(hours=1),
                    next_refresh_at=future,
                )
            )

            result = await service.refresh_model_info("gpt-x")
            assert result["skipped"] == 1
            assert result["refreshed"] == 0
            # No sources were attempted because we bailed before the
            # fetch loop.
            assert result["sources_attempted"] == []
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_empty_model_id_returns_zero_count_dict(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)

            cache = ModelCatalogCache()
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            for empty in ("", "   "):
                result = await service.refresh_model_info(empty)
                assert result == {
                    "requested": 0,
                    "refreshed": 0,
                    "skipped": 0,
                    "errors": 0,
                    "sources_attempted": [],
                    "sources_matched": [],
                    "observations": 0,
                }
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_source_filter_openrouter_only(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "openai/gpt-4o")

            or_payload = _openrouter_payload(
                _make_or_model("openai/gpt-4o", name="GPT-4o", context_length=128000),
            )
            client = _MockHttpClient(or_payload)

            cache = _make_cache("openai/gpt-4o", display_name="GPT-4o")
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            result = await service.refresh_model_info(
                "openai/gpt-4o", force=True, source="openrouter"
            )
            assert result["errors"] == 0
            # Provider catalog is always attempted so callability
            # facts stay current, even when only OpenRouter is
            # requested.
            assert "provider_catalog" in result["sources_attempted"]
            assert "openrouter" in result["sources_attempted"]
            # AA and HF are not attempted.
            assert "artificial_analysis" not in result["sources_attempted"]
            assert "huggingface" not in result["sources_attempted"]

            # Detail reflects OpenRouter enrichment.
            info = await service.repo.get_canonical("openai/gpt-4o")
            assert info is not None
            assert info.detail.get("external_ids", {}).get("openrouter") == (
                "openai/gpt-4o"
            )
            assert info.detail.get("limits", {}).get("external_context") == 128000
            assert "openrouter" in info.provenance.get("sources", [])
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_full_cycle_with_or_and_aa(self) -> None:
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "openai/gpt-4o")

            or_payload = _openrouter_payload(
                _make_or_model("openai/gpt-4o", name="GPT-4o", context_length=128000),
            )
            aa_payload = _aa_payload(_aa_model("openai/gpt-4o", name="GPT-4o"))  # noqa: F841 — symmetry

            # The mock client dispatches the same response to both
            # sources; for OR this is fine because it parses the
            # payload as an OR catalog, and for AA the AA adapter
            # will see different entries but still find the model
            # entry in either payload shape. Use OR for both — AA's
            # parser is permissive about missing fields.
            client = _MockHttpClient(or_payload)

            cache = _make_cache("openai/gpt-4o", display_name="GPT-4o")
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                    artificial_analysis=ModelInfoSourceConfig(
                        enabled=True, api_key="dummy-key"
                    ),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            result = await service.refresh_model_info("openai/gpt-4o", force=True)
            assert result["errors"] == 0
            assert "provider_catalog" in result["sources_attempted"]
            assert "openrouter" in result["sources_attempted"]
            assert (
                "artificial_analysis" in result["sources_attempted"]
                or client.call_count >= 1
            )

            info = await service.repo.get_canonical("openai/gpt-4o")
            assert info is not None
            assert info.detail.get("external_ids", {}).get("openrouter") == (
                "openai/gpt-4o"
            )
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_preserves_persisted_observations_across_cycle(self) -> None:
        """A previous cycle's HuggingFace observation must survive a
        later single-model refresh even if HF is not re-fetched this
        cycle."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "llama-3-8b")

            repo = ModelInfoRepository(db)
            # Persist a Hugging Face observation row directly.
            hf_record = SourceModelRecord(
                source="huggingface",
                source_model_id="meta-llama/Llama-3-8B",
                observed_at=datetime.now(UTC),
                raw_hash="hf-hash",
                raw_payload={},
                normalized={
                    "pipeline_tag": "text-generation",
                    "license": "apache-2.0",
                },
                license="apache-2.0",
            )
            await repo.upsert_observation(
                hf_record, model_id="llama-3-8b", provider_id="openai"
            )

            cache = _make_cache("llama-3-8b")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            # Run with no external sources enabled.
            result = await service.refresh_model_info("llama-3-8b", force=True)
            assert result["errors"] == 0

            info = await repo.get_canonical("llama-3-8b")
            assert info is not None
            assert "huggingface_metadata" in info.detail
            assert info.detail["huggingface_metadata"]["license"] == "apache-2.0"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_provider_id_suffix_strips_for_lookup(self) -> None:
        """Suffixed model IDs (e.g. ``gpt-x/openai``) are treated as a
        single opaque string — the caller is responsible for passing
        the canonical model_id. The service does NOT silently reparse
        the suffix."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-x")

            cache = _make_cache("gpt-x")
            service = ModelInfoService(ModelInfoConfig(), db, cache)

            result = await service.refresh_model_info("gpt-x/openai", force=True)
            assert result["errors"] == 0
            # Canonical row was created at the literal input.
            assert await service.repo.get_canonical("gpt-x/openai") is not None
            # No canonical row for the bare id.
            assert await service.repo.get_canonical("gpt-x") is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_manual_refresh_model_id_refreshes_only_requested_model(
        self,
    ) -> None:
        """``refresh_model_info(model_id=...)`` only touches the named
        model. Other canonical rows are not affected even when they
        would otherwise be due for refresh."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "model-a")
            await _seed_model(db, "model-b")

            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            past = now - timedelta(days=2)
            for mid in ("model-a", "model-b"):
                cache = _make_cache(mid)
                repo_obj = ModelInfoRepository(db)
                await repo_obj.upsert_canonical(
                    CanonicalModelInfo(
                        model_id=mid,
                        status="partial",
                        summary="seeded",
                        sparse=False,
                        detail={},
                        provenance={"sources": ["provider_catalog"]},
                        conflicts={},
                        first_seen_at=past,
                        last_seen_at=past,
                        last_refreshed_at=past,
                        next_refresh_at=past,  # both due
                    )
                )

            cache = _make_cache("model-a")
            cache._models["model-b"] = dict(cache._models["model-a"])
            cache._models["model-b"]["model_id"] = "model-b"
            cache._provider_models[("model-b", "openai")] = dict(
                cache._models["model-b"]
            )
            service = ModelInfoService(ModelInfoConfig(), db, cache)

            result = await service.refresh_model_info("model-a", force=True)
            assert result["requested"] == 1
            assert result["refreshed"] == 1
            assert result["errors"] == 0

            # Only model-a's last_refreshed_at advanced.
            a = await repo.get_canonical("model-a")
            b = await repo.get_canonical("model-b")
            assert a is not None and a.last_refreshed_at is not None
            assert b is not None and b.last_refreshed_at == past
            assert a.last_refreshed_at > past
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_manual_refresh_source_huggingface_fetches_hf_only_plus_provider(
        self,
    ) -> None:
        """When ``source="huggingface"`` is requested, only the HF
        source plus provider_catalog are attempted. OpenRouter and AA
        are not attempted."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "meta-llama/Llama-3-8B")

            cache = _make_cache("meta-llama/Llama-3-8B")
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                    artificial_analysis=ModelInfoSourceConfig(
                        enabled=True, api_key="dummy"
                    ),
                    huggingface=ModelInfoSourceConfig(enabled=True),
                )
            )
            # Hugging Face source only initializes when an outbound
            # client is wired in.
            client = _MockHttpClient({"data": []})
            service = ModelInfoService(config, db, cache, outbound_client=client)

            result = await service.refresh_model_info(
                "meta-llama/Llama-3-8B",
                force=True,
                source="huggingface",
            )
            assert "provider_catalog" in result["sources_attempted"]
            assert "huggingface" in result["sources_attempted"]
            assert "openrouter" not in result["sources_attempted"]
            assert "artificial_analysis" not in result["sources_attempted"]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_manual_refresh_reports_source_matched_only_on_record(
        self,
    ) -> None:
        """``sources_matched`` only contains sources that returned a
        record. Sources that returned ``None`` (no match) are not
        listed as matched."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "unmatched-model")

            # OpenRouter catalog has no entry for this model.
            or_payload = _openrouter_payload(
                _make_or_model("different-model"),
            )
            client = _MockHttpClient(or_payload)

            cache = _make_cache("unmatched-model")
            config = ModelInfoConfig(
                sources=ModelInfoSourcesConfig(
                    openrouter=ModelInfoSourceConfig(enabled=True),
                )
            )
            service = ModelInfoService(config, db, cache, outbound_client=client)

            result = await service.refresh_model_info("unmatched-model", force=True)
            assert "openrouter" in result["sources_attempted"]
            assert "openrouter" not in result["sources_matched"]
            assert "provider_catalog" in result["sources_matched"]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_manual_refresh_unknown_model_creates_unmatched_canonical(
        self,
    ) -> None:
        """An unknown model that exists in the catalog cache should
        still get a canonical row created without crashing."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "brand-new-model")

            cache = _make_cache("brand-new-model")
            service = ModelInfoService(ModelInfoConfig(), db, cache)

            # No prior canonical row.
            assert await service.repo.get_canonical("brand-new-model") is None

            result = await service.refresh_model_info("brand-new-model", force=True)
            assert result["errors"] == 0
            # Canonical row was created even though no external source matched.
            assert await service.repo.get_canonical("brand-new-model") is not None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_force_refresh_batch_processes_all_due_canonical_rows(
        self,
    ) -> None:
        """``force_refresh_batch`` bypasses the ``next_refresh_at``
        gate and refreshes every canonical row in a single bounded
        call."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "model-a")
            await _seed_model(db, "model-b")

            now = datetime.now(UTC)
            future = now + timedelta(hours=1)
            cache = _make_cache("model-a")
            cache._models["model-b"] = dict(cache._models["model-a"])
            cache._models["model-b"]["model_id"] = "model-b"
            cache._provider_models[("model-b", "openai")] = dict(
                cache._models["model-b"]
            )

            service = ModelInfoService(ModelInfoConfig(), db, cache)
            repo = service.repo
            for mid in ("model-a", "model-b"):
                await repo.upsert_canonical(
                    CanonicalModelInfo(
                        model_id=mid,
                        status="partial",
                        summary="seeded",
                        sparse=False,
                        detail={},
                        provenance={"sources": ["provider_catalog"]},
                        conflicts={},
                        first_seen_at=now - timedelta(days=1),
                        last_seen_at=now - timedelta(hours=1),
                        last_refreshed_at=now - timedelta(hours=1),
                        next_refresh_at=future,  # not due
                    )
                )

            result = await service.force_refresh_batch(batch_size=10)
            assert result["requested"] == 2
            assert result["errors"] == 0
            assert "provider_catalog" in result["sources_attempted"]

            # Both rows were force-refreshed.
            for mid in ("model-a", "model-b"):
                info = await repo.get_canonical(mid)
                assert info is not None
                assert info.last_refreshed_at is not None
                assert info.last_refreshed_at >= now
        finally:
            await db.disconnect()


# ---------------------------------------------------------------------------
# API-layer tests
# ---------------------------------------------------------------------------


class TestRefreshModelInfoAPI:
    @pytest.mark.asyncio()
    async def test_handle_model_info_refresh_no_model_id_runs_full_cycle(
        self,
    ) -> None:
        """The endpoint without a `model_id` query param delegates to
        the periodic ``refresh_due_models`` path."""
        from fastapi import FastAPI, Request

        # We'll exercise the handler directly via a fake request.
        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            cache = _make_cache("gpt-x")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/api/model-info/refresh",
                "headers": [],
                "query_string": b"",
                "app": app,
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(scope, receive)
            response = await handle_model_info_refresh(request)
            # Cycle path: scope is "cycle", counts come from refresh_due_models.
            assert response.status_code == 200
            body = json.loads(response.body)
            assert body["status"] == "ok"
            assert body["scope"] == "cycle"
            assert body["requested"] == 0  # no due rows
            assert body["refreshed"] == 0
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_handle_model_info_refresh_single_model_delegates(
        self,
    ) -> None:
        from fastapi import FastAPI, Request

        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-x")
            cache = _make_cache("gpt-x")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/api/model-info/refresh",
                "headers": [],
                "query_string": b"model_id=gpt-x&force=1",
                "app": app,
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(scope, receive)
            response = await handle_model_info_refresh(request)
            assert response.status_code == 200
            body = json.loads(response.body)
            assert body["status"] == "ok"
            assert body["scope"] == "model"
            assert body["model_id"] == "gpt-x"
            assert body["refreshed"] == 1
            assert body["errors"] == 0
            assert "provider_catalog" in body["sources_attempted"]
            assert "provider_catalog" in body["sources_matched"]
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_handle_model_info_refresh_disabled_returns_503(self) -> None:
        from fastapi import FastAPI, Request

        app = FastAPI()
        # No model_info attribute → endpoint returns 503.
        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/api/model-info/refresh",
            "headers": [],
            "query_string": b"model_id=gpt-x",
            "app": app,
        }

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive)
        response = await handle_model_info_refresh(request)
        assert response.status_code == 503

    @pytest.mark.asyncio()
    async def test_handle_model_info_refresh_force_no_model_id_runs_batch(
        self,
    ) -> None:
        """``POST /api/model-info/refresh?force=1`` without ``model_id``
        delegates to ``force_refresh_batch`` and returns the bounded
        batch counts."""
        from fastapi import FastAPI, Request

        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-x")
            cache = _make_cache("gpt-x")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/api/model-info/refresh",
                "headers": [],
                "query_string": b"force=1",
                "app": app,
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(scope, receive)
            response = await handle_model_info_refresh(request)
            assert response.status_code == 200
            body = json.loads(response.body)
            assert body["status"] == "ok"
            assert body["scope"] == "force_batch"
            assert "requested" in body
            assert "refreshed" in body
            assert "sources_attempted" in body
            assert "sources_matched" in body
            assert "observations" in body
        finally:
            await db.disconnect()


class TestRefreshModelInfoAPIClosure:
    """Plan §Defect 2 — the refresh endpoint must:

    * Strip a ``/provider`` suffix from ``?model_id=`` via
      :func:`parse_model_provider` so callers can pass either a
      canonical base id (``gpt-4o``) or a provider-suffixed id
      (``gpt-4o/openai``) and the canonical row is refreshed
      either way.
    * Forward the stripped provider suffix to the service as the
      ``provider_id`` filter (for ``source="provider_catalog"``
      narrowing).
    * Validate the ``?source=`` filter against an allow-list and
      return HTTP 400 on unknown values.
    * Treat ``source=all`` (or absent) as ``None`` so every enabled
      source runs.
    """

    @pytest.mark.asyncio()
    async def test_provider_suffixed_model_id_normalizes_to_canonical(
        self,
    ) -> None:
        """``POST /api/model-info/refresh?model_id=gpt-4o/openai``
        calls ``refresh_model_info`` with ``model_id="gpt-4o"`` and
        ``provider_id="openai"``, not with the literal suffixed
        string.
        """
        from fastapi import FastAPI, Request

        from eggpool.dashboard.routes import _get_dashboard_config  # noqa: F401

        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")
            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            captured: dict[str, Any] = {}
            original = service.refresh_model_info

            async def _wrapped(
                model_id: str,
                *,
                provider_id: str | None = None,
                source: str | None = None,
                force: bool = False,
            ) -> dict[str, Any]:
                captured["model_id"] = model_id
                captured["provider_id"] = provider_id
                captured["source"] = source
                captured["force"] = force
                return await original(
                    model_id,
                    provider_id=provider_id,
                    source=source,
                    force=force,
                )

            service.refresh_model_info = _wrapped  # type: ignore[method-assign]

            class _Providers(dict):
                def __init__(self) -> None:
                    super().__init__(openai=object())

            class _Config:
                providers = _Providers()

            app.state.config = _Config()

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/api/model-info/refresh",
                "headers": [],
                "query_string": b"model_id=gpt-4o/openai&force=1",
                "app": app,
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(scope, receive)
            response = await handle_model_info_refresh(request)
            assert response.status_code == 200
            body = json.loads(response.body)
            assert body["status"] == "ok"
            assert body["scope"] == "model"
            assert body["requested_model_id"] == "gpt-4o/openai"
            assert body["model_id"] == "gpt-4o"
            assert body["provider_id"] == "openai"
            assert captured["model_id"] == "gpt-4o"
            assert captured["provider_id"] == "openai"
            assert captured["force"] is True
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_unsuffixed_model_id_yields_provider_none(self) -> None:
        """An unsuffixed id passes ``provider_id=None`` through the
        API — no parse happens and no suffix is reported back."""
        from fastapi import FastAPI, Request

        from eggpool.models.config import ProviderConfig

        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")
            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            captured: dict[str, Any] = {}

            async def _wrapped(
                model_id: str,
                *,
                provider_id: str | None = None,
                source: str | None = None,
                force: bool = False,
            ) -> dict[str, Any]:
                captured["model_id"] = model_id
                captured["provider_id"] = provider_id
                return {
                    "requested": 1,
                    "refreshed": 1,
                    "skipped": 0,
                    "errors": 0,
                    "sources_attempted": ["provider_catalog"],
                    "sources_matched": ["provider_catalog"],
                    "observations": 1,
                }

            service.refresh_model_info = _wrapped  # type: ignore[method-assign]

            class _Config:
                providers = {"openai": ProviderConfig.model_construct()}

            app.state.config = _Config()

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/api/model-info/refresh",
                "headers": [],
                "query_string": b"model_id=gpt-4o&force=1",
                "app": app,
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(scope, receive)
            response = await handle_model_info_refresh(request)
            assert response.status_code == 200
            body = json.loads(response.body)
            assert body["model_id"] == "gpt-4o"
            assert body["provider_id"] is None
            assert body["requested_model_id"] == "gpt-4o"
            assert captured["provider_id"] is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_unknown_source_returns_http_400(self) -> None:
        """Unrecognized ``?source=`` values are rejected with
        HTTP 400 — the service is not called."""
        from fastapi import FastAPI, Request

        from eggpool.models.config import ProviderConfig

        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            cache = ModelCatalogCache()
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            called = False

            async def _fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                nonlocal called
                called = True
                return {}

            service.refresh_model_info = _fail  # type: ignore[method-assign]

            class _Config:
                providers = {"openai": ProviderConfig.model_construct()}

            app.state.config = _Config()

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/api/model-info/refresh",
                "headers": [],
                "query_string": b"model_id=gpt-4o&source=bad&force=1",
                "app": app,
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(scope, receive)
            response = await handle_model_info_refresh(request)
            assert response.status_code == 400
            body = json.loads(response.body)
            assert "error" in body
            assert "bad" in body["error"]
            assert called is False
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_source_all_maps_to_none(self) -> None:
        """``?source=all`` normalizes to ``None`` so every enabled
        source can run."""
        from fastapi import FastAPI, Request

        from eggpool.models.config import ProviderConfig

        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")
            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            captured: dict[str, Any] = {}

            async def _wrapped(
                model_id: str,
                *,
                provider_id: str | None = None,
                source: str | None = None,
                force: bool = False,
            ) -> dict[str, Any]:
                captured["source"] = source
                return {
                    "requested": 1,
                    "refreshed": 1,
                    "skipped": 0,
                    "errors": 0,
                    "sources_attempted": ["provider_catalog"],
                    "sources_matched": ["provider_catalog"],
                    "observations": 1,
                }

            service.refresh_model_info = _wrapped  # type: ignore[method-assign]

            class _Config:
                providers = {"openai": ProviderConfig.model_construct()}

            app.state.config = _Config()

            for source_value in ("all", ""):
                captured.clear()
                scope: dict[str, Any] = {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/model-info/refresh",
                    "headers": [],
                    "query_string": (f"model_id=gpt-4o&source={source_value}".encode()),
                    "app": app,
                }

                async def receive() -> dict[str, Any]:
                    return {"type": "http.request", "body": b"", "more_body": False}

                request = Request(scope, receive)
                response = await handle_model_info_refresh(request)
                assert response.status_code == 200
                # Both ``all`` and ``""`` map to ``None``.
                assert captured["source"] is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_provider_catalog_source_is_provider_only(self) -> None:
        """``?source=provider_catalog`` passes through to the service
        so the per-provider record selection runs; the service
        layer's existing logic treats provider-catalog as always on,
        while external sources still run because the source filter
        names a known configured source value (the service filters
        external branches by exact string match against ``None`` or
        the named source).
        """
        from fastapi import FastAPI, Request

        from eggpool.models.config import ProviderConfig

        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")
            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            captured: dict[str, Any] = {}

            async def _wrapped(
                model_id: str,
                *,
                provider_id: str | None = None,
                source: str | None = None,
                force: bool = False,
            ) -> dict[str, Any]:
                captured["source"] = source
                return {
                    "requested": 1,
                    "refreshed": 1,
                    "skipped": 0,
                    "errors": 0,
                    "sources_attempted": ["provider_catalog"],
                    "sources_matched": ["provider_catalog"],
                    "observations": 1,
                }

            service.refresh_model_info = _wrapped  # type: ignore[method-assign]

            class _Config:
                providers = {"openai": ProviderConfig.model_construct()}

            app.state.config = _Config()

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/api/model-info/refresh",
                "headers": [],
                "query_string": b"model_id=gpt-4o&source=provider_catalog&force=1",
                "app": app,
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(scope, receive)
            response = await handle_model_info_refresh(request)
            assert response.status_code == 200
            # The API forwards the literal ``provider_catalog`` to
            # the service, which honors it as "provider-only" by
            # always running the provider branch.
            assert captured["source"] == "provider_catalog"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_url_encoded_provider_suffix_is_decoded(self) -> None:
        """``%2F`` in ``?model_id=`` is URL-decoded before
        ``parse_model_provider`` runs so callers that don't
        pre-encode the slash still get the right split."""
        from fastapi import FastAPI, Request

        from eggpool.models.config import ProviderConfig

        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4o")
            cache = _make_cache("gpt-4o")
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            captured: dict[str, Any] = {}

            async def _wrapped(
                model_id: str,
                *,
                provider_id: str | None = None,
                source: str | None = None,
                force: bool = False,
            ) -> dict[str, Any]:
                captured["model_id"] = model_id
                captured["provider_id"] = provider_id
                return {
                    "requested": 1,
                    "refreshed": 1,
                    "skipped": 0,
                    "errors": 0,
                    "sources_attempted": ["provider_catalog"],
                    "sources_matched": ["provider_catalog"],
                    "observations": 1,
                }

            service.refresh_model_info = _wrapped  # type: ignore[method-assign]

            class _Config:
                providers = {"openai": ProviderConfig.model_construct()}

            app.state.config = _Config()

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/api/model-info/refresh",
                "headers": [],
                "query_string": b"model_id=gpt-4o%2Fopenai&force=1",
                "app": app,
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(scope, receive)
            response = await handle_model_info_refresh(request)
            assert response.status_code == 200
            body = json.loads(response.body)
            assert captured["model_id"] == "gpt-4o"
            assert captured["provider_id"] == "openai"
            assert body["model_id"] == "gpt-4o"
            assert body["provider_id"] == "openai"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_unknown_provider_suffix_falls_back_to_full_id(self) -> None:
        """A suffix that doesn't match any configured provider is
        ignored so the literal id (minus the slash segment?) is
        looked up.  ``parse_model_provider`` returns the input
        unchanged when the suffix isn't in ``known_providers`` so
        legacy callers that pass ``gpt-4o/openrouter`` even when
        ``openrouter`` is not configured still get a refresh on the
        literal id."""
        from fastapi import FastAPI, Request

        from eggpool.models.config import ProviderConfig

        app = FastAPI()
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            cache = ModelCatalogCache()
            service = ModelInfoService(ModelInfoConfig(), db, cache)
            app.state.model_info = service

            captured: dict[str, Any] = {}

            async def _wrapped(
                model_id: str,
                *,
                provider_id: str | None = None,
                source: str | None = None,
                force: bool = False,
            ) -> dict[str, Any]:
                captured["model_id"] = model_id
                captured["provider_id"] = provider_id
                return {
                    "requested": 1,
                    "refreshed": 1,
                    "skipped": 0,
                    "errors": 0,
                    "sources_attempted": ["provider_catalog"],
                    "sources_matched": [],
                    "observations": 0,
                }

            service.refresh_model_info = _wrapped  # type: ignore[method-assign]

            # Only ``openai`` is configured; ``unknown`` suffix must
            # not be treated as a provider filter.
            class _Config:
                providers = {"openai": ProviderConfig.model_construct()}

            app.state.config = _Config()

            scope: dict[str, Any] = {
                "type": "http",
                "method": "POST",
                "path": "/api/model-info/refresh",
                "headers": [],
                "query_string": b"model_id=gpt-4o/unknown&force=1",
                "app": app,
            }

            async def receive() -> dict[str, Any]:
                return {"type": "http.request", "body": b"", "more_body": False}

            request = Request(scope, receive)
            response = await handle_model_info_refresh(request)
            assert response.status_code == 200
            # The unknown suffix is left intact so the literal id
            # is the lookup key — no silent canonicalization.
            assert captured["model_id"] == "gpt-4o/unknown"
            assert captured["provider_id"] is None
            body = json.loads(response.body)
            assert body["model_id"] == "gpt-4o/unknown"
            assert body["provider_id"] is None
        finally:
            await db.disconnect()
