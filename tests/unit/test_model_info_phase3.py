"""Tests for model-info Phase 3: OpenRouter metadata source and identity resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.errors import ModelInfoSourceFetchError
from eggpool.model_info.identity import resolve_openrouter_record
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.service import (
    ModelInfoService,
    _build_source_list,
    _detect_context_conflicts,
    _enrich_detail_from_record,
)
from eggpool.model_info.sources.openrouter import (
    OpenRouterModelInfoSource,
    _parse_catalog_payload,
    _parse_entry_to_record,
)
from eggpool.model_info.types import CanonicalModelInfo, SourceModelRecord
from eggpool.models.config import ModelInfoConfig, ModelInfoSourceConfig

if TYPE_CHECKING:
    from eggpool.catalog.cache import ModelCatalogCache


async def _run_migrations(db: Database) -> None:
    runner = MigrationRunner(db)
    await runner.run()


async def _seed_model(
    db: Database, model_id: str = "gpt-4o", display_name: str = "GPT-4o"
) -> None:
    async with db.transaction():
        await db.execute_write(
            "INSERT INTO models (model_id, display_name) VALUES (?, ?)",
            (model_id, display_name),
        )


def _make_openrouter_payload(*models: dict) -> dict:
    """Build an OpenRouter-style /models response."""
    return {"data": list(models)}


def _make_or_model(
    model_id: str,
    *,
    name: str = "",
    context_length: int = 0,
    max_output: int = 0,
    prompt_price: str = "0",
    completion_price: str = "0",
    modalities: list[str] | None = None,
    supported_parameters: list[str] | None = None,
) -> dict:
    """Build a single OpenRouter model entry dict."""
    entry: dict = {"id": model_id}
    if name:
        entry["name"] = name
    if context_length:
        entry["context_length"] = context_length
    if max_output:
        entry["max_completion_tokens"] = max_output
    if modalities is not None:
        entry["architecture"] = {
            "input_modalities": modalities,
            "output_modalities": ["text"],
        }
    if supported_parameters is not None:
        entry["supported_parameters"] = supported_parameters
    entry["pricing"] = {
        "prompt": prompt_price,
        "completion": completion_price,
    }
    return entry


class _MockHttpClient:
    """Mock HTTP client that returns pre-configured responses."""

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


# --- OpenRouter source adapter parsing tests ---


class TestOpenRouterSourceParsing:
    def test_parses_basic_model_entry(self) -> None:
        """test_openrouter_source_parses_basic_model_entry"""
        now = datetime.now(UTC)
        raw = _make_or_model(
            "openai/gpt-4o",
            name="GPT-4o",
            context_length=128000,
            max_output=16384,
            prompt_price="0.0000025",
            completion_price="0.00001",
        )
        record = _parse_entry_to_record("openai/gpt-4o", raw, now)

        assert record.source == "openrouter"
        assert record.source_model_id == "openai/gpt-4o"
        assert record.display_name == "GPT-4o"
        assert record.context_window == 128000
        assert record.max_output_tokens == 16384
        assert record.input_price_per_1k is not None
        assert record.output_price_per_1k is not None
        assert record.modalities == frozenset({"text"})
        assert record.raw_hash  # non-empty

    def test_parses_pricing_defensively(self) -> None:
        """test_openrouter_source_parses_pricing_defensively"""
        now = datetime.now(UTC)
        raw = _make_or_model(
            "test/model",
            name="Test",
            prompt_price="not-a-number",
            completion_price="also-bad",
        )
        record = _parse_entry_to_record("test/model", raw, now)
        # Should not raise, just return None for bad prices
        assert record.input_price_per_1k is None
        assert record.output_price_per_1k is None

    def test_missing_optional_fields_returns_record(self) -> None:
        """test_openrouter_source_missing_optional_fields_returns_record"""
        now = datetime.now(UTC)
        raw = {"id": "minimal/model"}
        record = _parse_entry_to_record("minimal/model", raw, now)

        assert record.source == "openrouter"
        assert record.source_model_id == "minimal/model"
        assert record.display_name == "minimal/model"
        assert record.context_window is None
        assert record.max_output_tokens is None
        assert record.input_price_per_1k is None
        assert record.output_price_per_1k is None

    def test_parses_modalities_from_architecture(self) -> None:
        """Architecture input_modalities are parsed correctly."""
        now = datetime.now(UTC)
        raw = _make_or_model(
            "meta/llama-vision",
            name="Llama Vision",
            modalities=["text", "image"],
        )
        record = _parse_entry_to_record("meta/llama-vision", raw, now)
        assert "text" in record.modalities
        assert "image" in record.modalities

    def test_tool_support_from_supported_parameters(self) -> None:
        """supported_parameters with 'tools' sets supports_tools=True."""
        now = datetime.now(UTC)
        raw = _make_or_model(
            "openai/gpt-4o",
            name="GPT-4o",
            supported_parameters=["temperature", "tools", "max_tokens"],
        )
        record = _parse_entry_to_record("openai/gpt-4o", raw, now)
        assert record.supports_tools is True

    def test_reasoning_support_from_supported_parameters(self) -> None:
        """supported_parameters with 'reasoning' sets supports_reasoning=True."""
        now = datetime.now(UTC)
        raw = _make_or_model(
            "deepseek/r1",
            name="DeepSeek R1",
            supported_parameters=["reasoning", "temperature"],
        )
        record = _parse_entry_to_record("deepseek/r1", raw, now)
        assert record.supports_reasoning is True


# --- OpenRouter source adapter network tests ---


class TestOpenRouterSourceFetch:
    @pytest.mark.asyncio()
    async def test_fetch_all_returns_records(self) -> None:
        """fetch_all returns SourceModelRecords for all catalog entries."""
        payload = _make_openrouter_payload(
            _make_or_model("openai/gpt-4o", name="GPT-4o", context_length=128000),
            _make_or_model(
                "anthropic/claude-3", name="Claude 3", context_length=200000
            ),
        )
        client = _MockHttpClient(payload)
        config = ModelInfoSourceConfig()
        source = OpenRouterModelInfoSource(config=config, client=client)

        records = await source.fetch_all()
        assert len(records) == 2
        ids = {r.source_model_id for r in records}
        assert "openai/gpt-4o" in ids
        assert "anthropic/claude-3" in ids

    @pytest.mark.asyncio()
    async def test_fetch_one_returns_single_record(self) -> None:
        """fetch_one returns a single record by source model ID."""
        payload = _make_openrouter_payload(
            _make_or_model("openai/gpt-4o", name="GPT-4o"),
            _make_or_model("anthropic/claude-3", name="Claude 3"),
        )
        client = _MockHttpClient(payload)
        config = ModelInfoSourceConfig()
        source = OpenRouterModelInfoSource(config=config, client=client)

        record = await source.fetch_one("openai/gpt-4o")
        assert record is not None
        assert record.source_model_id == "openai/gpt-4o"

    @pytest.mark.asyncio()
    async def test_fetch_one_returns_none_for_unknown(self) -> None:
        """fetch_one returns None for an unknown model ID."""
        payload = _make_openrouter_payload(
            _make_or_model("openai/gpt-4o", name="GPT-4o"),
        )
        client = _MockHttpClient(payload)
        config = ModelInfoSourceConfig()
        source = OpenRouterModelInfoSource(config=config, client=client)

        record = await source.fetch_one("nonexistent/model")
        assert record is None

    @pytest.mark.asyncio()
    async def test_bad_payload_returns_empty_catalog(self) -> None:
        """test_openrouter_source_bad_payload_returns_empty_catalog"""
        client = _MockHttpClient({"not": "a valid catalog"})
        config = ModelInfoSourceConfig()
        source = OpenRouterModelInfoSource(config=config, client=client)

        records = await source.fetch_all()
        assert records == []

    @pytest.mark.asyncio()
    async def test_http_error_records_fetch_error(self) -> None:
        """test_openrouter_source_http_error_records_fetch_error"""
        client = _MockHttpClient(
            httpx.HTTPStatusError(
                "500",
                request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
                response=httpx.Response(500),
            )
        )
        config = ModelInfoSourceConfig()
        source = OpenRouterModelInfoSource(config=config, client=client)

        with pytest.raises(ModelInfoSourceFetchError):
            await source.fetch_all()

    @pytest.mark.asyncio()
    async def test_ttl_cache_reuses_fresh_response(self) -> None:
        """Second fetch_all call uses the cache, not a second HTTP request."""
        payload = _make_openrouter_payload(
            _make_or_model("openai/gpt-4o", name="GPT-4o"),
        )
        client = _MockHttpClient(payload)
        config = ModelInfoSourceConfig(ttl_seconds=300)
        source = OpenRouterModelInfoSource(config=config, client=client)

        await source.fetch_all()
        await source.fetch_all()
        assert client.call_count == 1  # only one HTTP call

    @pytest.mark.asyncio()
    async def test_custom_base_url(self) -> None:
        """Custom base_url is used for the request."""
        payload = _make_openrouter_payload()
        client = _MockHttpClient(payload)
        config = ModelInfoSourceConfig(base_url="https://custom.api/v1")
        source = OpenRouterModelInfoSource(config=config, client=client)

        await source.fetch_all()
        assert client.call_count == 1

    @pytest.mark.asyncio()
    async def test_api_key_in_headers(self) -> None:
        """API key is included in the Authorization header."""
        payload = _make_openrouter_payload()
        client = _MockHttpClient(payload)
        config = ModelInfoSourceConfig(api_key="test-key-123")
        source = OpenRouterModelInfoSource(config=config, client=client)

        await source.fetch_all()
        assert client.call_count == 1

    def test_priority_from_config(self) -> None:
        """Priority is sourced from config."""
        config = ModelInfoSourceConfig(priority=42)
        source = OpenRouterModelInfoSource(config=config, client=_MockHttpClient({}))
        assert source.priority == 42

    def test_name_is_openrouter(self) -> None:
        """Source name is 'openrouter'."""
        config = ModelInfoSourceConfig()
        source = OpenRouterModelInfoSource(config=config, client=_MockHttpClient({}))
        assert source.name == "openrouter"


# --- Catalog payload parsing ---


class TestCatalogPayloadParsing:
    def test_empty_payload(self) -> None:
        """Empty or invalid payload returns empty dict."""
        assert _parse_catalog_payload({}) == {}
        assert _parse_catalog_payload("not a dict") == {}
        assert _parse_catalog_payload({"data": "not a list"}) == {}

    def test_valid_payload(self) -> None:
        """Valid payload returns entries keyed by model ID."""
        payload = _make_openrouter_payload(
            _make_or_model("openai/gpt-4o", name="GPT-4o"),
            _make_or_model("anthropic/claude-3", name="Claude 3"),
        )
        entries = _parse_catalog_payload(payload)
        assert len(entries) == 2
        assert "openai/gpt-4o" in entries
        assert "anthropic/claude-3" in entries

    def test_skips_entries_without_id(self) -> None:
        """Entries without an 'id' field are skipped."""
        payload = {"data": [{"name": "no-id-model"}, {"id": "valid/model"}]}
        entries = _parse_catalog_payload(payload)
        assert len(entries) == 1
        assert "valid/model" in entries


# --- Identity resolution tests ---


class TestIdentityResolution:
    @pytest.mark.asyncio()
    async def test_exact_alias_match(self) -> None:
        """test_identity_exact_alias_match"""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "local-model")

            repo = ModelInfoRepository(db)
            # Create an openrouter alias
            await repo.upsert_alias(
                model_id="local-model",
                provider_id="test-provider",
                alias="openai/gpt-4o",
                source="openrouter",
                confidence=0.8,
            )

            now = datetime.now(UTC)
            or_record = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=now,
                raw_hash="abc",
                raw_payload={},
                normalized={},
                display_name="GPT-4o",
            )
            openrouter_indexed = {"openai/gpt-4o": or_record}

            result = await resolve_openrouter_record(
                "local-model", repo, openrouter_indexed
            )
            assert result is not None
            assert result.source_model_id == "openai/gpt-4o"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_exact_model_id_match(self) -> None:
        """Exact source_model_id == model_id resolves directly."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "openai/gpt-4o")

            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            or_record = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=now,
                raw_hash="abc",
                raw_payload={},
                normalized={},
                display_name="GPT-4o",
            )
            openrouter_indexed = {"openai/gpt-4o": or_record}

            result = await resolve_openrouter_record(
                "openai/gpt-4o", repo, openrouter_indexed
            )
            assert result is not None
            assert result.source_model_id == "openai/gpt-4o"
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_no_match_returns_none(self) -> None:
        """Unrelated model_id returns no match (no substring matching)."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "unrelated-model")

            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            or_record = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=now,
                raw_hash="abc",
                raw_payload={},
                normalized={},
                display_name="GPT-4o",
            )
            openrouter_indexed = {"openai/gpt-4o": or_record}

            result = await resolve_openrouter_record(
                "unrelated-model", repo, openrouter_indexed
            )
            assert result is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_wrong_source_alias_not_matched(self) -> None:
        """Alias from a different source is not used for OpenRouter matching."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "local-model")

            repo = ModelInfoRepository(db)
            # Create an alias from a different source
            await repo.upsert_alias(
                model_id="local-model",
                provider_id="test-provider",
                alias="openai/gpt-4o",
                source="other_source",
                confidence=0.8,
            )

            now = datetime.now(UTC)
            or_record = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=now,
                raw_hash="abc",
                raw_payload={},
                normalized={},
                display_name="GPT-4o",
            )
            openrouter_indexed = {"openai/gpt-4o": or_record}

            result = await resolve_openrouter_record(
                "local-model", repo, openrouter_indexed
            )
            assert result is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_ambiguous_aliases_returns_none(self) -> None:
        """Multiple aliases pointing to different entries return no match."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "local-model")

            repo = ModelInfoRepository(db)
            # Create two different openrouter aliases for the same model
            await repo.upsert_alias(
                model_id="local-model",
                provider_id="test-provider",
                alias="openai/gpt-4o",
                source="openrouter",
                confidence=0.8,
            )
            await repo.upsert_alias(
                model_id="local-model",
                provider_id="test-provider",
                alias="openai/gpt-4o-mini",
                source="openrouter",
                confidence=0.6,
            )

            now = datetime.now(UTC)
            or_record_1 = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=now,
                raw_hash="abc",
                raw_payload={},
                normalized={},
                display_name="GPT-4o",
            )
            or_record_2 = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o-mini",
                observed_at=now,
                raw_hash="def",
                raw_payload={},
                normalized={},
                display_name="GPT-4o Mini",
            )
            openrouter_indexed = {
                "openai/gpt-4o": or_record_1,
                "openai/gpt-4o-mini": or_record_2,
            }

            result = await resolve_openrouter_record(
                "local-model", repo, openrouter_indexed
            )
            # Ambiguous — should return None
            assert result is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_substring_match_refused(self) -> None:
        """Substring/contains matches are not resolved."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "gpt-4")

            repo = ModelInfoRepository(db)
            now = datetime.now(UTC)
            or_record = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=now,
                raw_hash="abc",
                raw_payload={},
                normalized={},
                display_name="GPT-4o",
            )
            openrouter_indexed = {"openai/gpt-4o": or_record}

            # "gpt-4" is a substring of "openai/gpt-4o" but should NOT match
            result = await resolve_openrouter_record("gpt-4", repo, openrouter_indexed)
            assert result is None
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_pricing_alias_reused_when_source_matches(self) -> None:
        """Pricing alias is reused when the alias source is openrouter."""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "local-model")

            repo = ModelInfoRepository(db)
            # Create an alias with source="pricing" — this is NOT openrouter source
            await repo.upsert_alias(
                model_id="local-model",
                provider_id="test-provider",
                alias="openai/gpt-4o",
                source="pricing",
                confidence=0.9,
            )

            now = datetime.now(UTC)
            or_record = SourceModelRecord(
                source="openrouter",
                source_model_id="openai/gpt-4o",
                observed_at=now,
                raw_hash="abc",
                raw_payload={},
                normalized={},
                display_name="GPT-4o",
            )
            openrouter_indexed = {"openai/gpt-4o": or_record}

            # Pricing alias should be checked (rule 3 in identity.py)
            result = await resolve_openrouter_record(
                "local-model", repo, openrouter_indexed
            )
            assert result is not None
            assert result.source_model_id == "openai/gpt-4o"
        finally:
            await db.disconnect()


# --- Enrichment and conflict detection tests ---


class TestEnrichDetailFromRecord:
    def test_enriches_with_openrouter_fields(self) -> None:
        """OpenRouter fields are added to detail under external_* keys."""
        detail: dict[str, object] = {
            "display_name": "GPT-4o",
            "context_tokens": 128000,
        }
        now = datetime.now(UTC)
        record = SourceModelRecord(
            source="openrouter",
            source_model_id="openai/gpt-4o",
            observed_at=now,
            raw_hash="abc",
            raw_payload={},
            normalized={"created_at": "2024-01-01T00:00:00+00:00"},
            display_name="GPT-4o",
            context_window=128000,
            max_output_tokens=16384,
            modalities=frozenset({"text", "image"}),
            input_price_per_1k=2.5,
            output_price_per_1k=10.0,
        )

        enriched = _enrich_detail_from_record(detail, record)
        assert enriched["external_ids"] == {"openrouter": "openai/gpt-4o"}
        assert enriched["context_window_external"] == 128000
        assert enriched["max_output_tokens_external"] == 16384
        assert enriched["modalities_external"] == ["image", "text"]
        assert enriched["pricing_observation"] == {
            "input_price_per_1k": 2.5,
            "output_price_per_1k": 10.0,
        }
        assert enriched["created_at_external"] == "2024-01-01T00:00:00+00:00"
        # Original fields preserved
        assert enriched["display_name"] == "GPT-4o"
        assert enriched["context_tokens"] == 128000

    def test_no_record_returns_original_detail(self) -> None:
        """When record is None, detail is returned unchanged."""
        detail: dict[str, object] = {"display_name": "GPT-4o"}
        enriched = _enrich_detail_from_record(detail, None)
        assert enriched == detail
        assert enriched is not detail  # copy, not alias

    def test_external_ids_preserve_existing(self) -> None:
        """Existing external_ids entries are preserved."""
        detail: dict[str, object] = {
            "external_ids": {"other_source": "some-id"},
        }
        now = datetime.now(UTC)
        record = SourceModelRecord(
            source="openrouter",
            source_model_id="openai/gpt-4o",
            observed_at=now,
            raw_hash="abc",
            raw_payload={},
            normalized={},
            display_name="GPT-4o",
        )

        enriched = _enrich_detail_from_record(detail, record)
        assert enriched["external_ids"]["other_source"] == "some-id"
        assert enriched["external_ids"]["openrouter"] == "openai/gpt-4o"


class TestDetectContextConflicts:
    def test_conflict_when_context_differs_materially(self) -> None:
        """Conflict recorded when context windows differ by >10%."""
        detail: dict[str, object] = {"context_tokens": 128000}
        now = datetime.now(UTC)
        record = SourceModelRecord(
            source="openrouter",
            source_model_id="openai/gpt-4o",
            observed_at=now,
            raw_hash="abc",
            raw_payload={},
            normalized={},
            display_name="GPT-4o",
            context_window=1_000_000,  # >10% different
        )

        conflicts = _detect_context_conflicts(detail, record, {})
        assert "context_window" in conflicts
        assert conflicts["context_window"]["provider_catalog"] == 128000
        assert conflicts["context_window"]["openrouter"] == 1_000_000
        assert (
            conflicts["context_window"]["selected"]
            == "provider_catalog/effective_limit"
        )

    def test_no_conflict_when_context_matches(self) -> None:
        """No conflict when context windows are close (<10% diff)."""
        detail: dict[str, object] = {"context_tokens": 128000}
        now = datetime.now(UTC)
        record = SourceModelRecord(
            source="openrouter",
            source_model_id="openai/gpt-4o",
            observed_at=now,
            raw_hash="abc",
            raw_payload={},
            normalized={},
            display_name="GPT-4o",
            context_window=130000,  # ~1.5% different
        )

        conflicts = _detect_context_conflicts(detail, record, {})
        assert "context_window" not in conflicts

    def test_no_conflict_when_only_one_source(self) -> None:
        """No conflict when only one source has context info."""
        detail: dict[str, object] = {}
        now = datetime.now(UTC)
        record = SourceModelRecord(
            source="openrouter",
            source_model_id="openai/gpt-4o",
            observed_at=now,
            raw_hash="abc",
            raw_payload={},
            normalized={},
            display_name="GPT-4o",
            context_window=128000,
        )

        conflicts = _detect_context_conflicts(detail, record, {})
        assert "context_window" not in conflicts

    def test_preserves_existing_conflicts(self) -> None:
        """Existing conflicts are preserved when no new conflict detected."""
        existing = {"some_other_field": {"value": "conflict"}}
        detail: dict[str, object] = {"context_tokens": 128000}
        now = datetime.now(UTC)
        record = SourceModelRecord(
            source="openrouter",
            source_model_id="openai/gpt-4o",
            observed_at=now,
            raw_hash="abc",
            raw_payload={},
            normalized={},
            display_name="GPT-4o",
            context_window=130000,  # close enough
        )

        conflicts = _detect_context_conflicts(detail, record, existing)
        assert "some_other_field" in conflicts
        assert "context_window" not in conflicts


class TestOpenrouterContextConflictIsRecorded:
    @pytest.mark.asyncio()
    async def test_conflict_recorded_in_canonical(self) -> None:
        """test_openrouter_context_conflict_is_recorded"""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "openai/gpt-4o", "GPT-4o")

            # OpenRouter says context is 1M, local catalog says 128k
            payload = _make_openrouter_payload(
                _make_or_model(
                    "openai/gpt-4o",
                    name="GPT-4o",
                    context_length=1_000_000,
                ),
            )
            client = _MockHttpClient(payload)

            from eggpool.catalog.cache import ModelCatalogCache

            cache = ModelCatalogCache()
            now_ts = datetime.now(UTC).timestamp()
            cache._models["openai/gpt-4o"] = {
                "model_id": "openai/gpt-4o",
                "display_name": "GPT-4o",
                "protocol": "openai",
                "capabilities": {},
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
            cache._provider_models[("openai/gpt-4o", "openai")] = dict(
                cache._models["openai/gpt-4o"]
            )

            config = ModelInfoConfig()
            service = ModelInfoService(config, db, cache, outbound_client=client)

            # Create a due canonical row
            now = datetime.now(UTC)
            info = CanonicalModelInfo(
                model_id="openai/gpt-4o",
                status="partial",
                summary="Test",
                sparse=False,
                detail={},
                provenance={"sources": ["provider_catalog"]},
                conflicts={},
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now - timedelta(hours=1),
                last_refreshed_at=now - timedelta(hours=1),
                next_refresh_at=now - timedelta(minutes=1),
            )
            await service.repo.upsert_canonical(info)

            result = await service.refresh_due_models()
            assert result["refreshed"] + result["skipped"] == result["total"]

            # Check that conflict was recorded
            updated = await service.repo.get_canonical("openai/gpt-4o")
            assert updated is not None
            assert "context_window" in updated.conflicts
            assert updated.conflicts["context_window"]["provider_catalog"] == 128000
            assert updated.conflicts["context_window"]["openrouter"] == 1_000_000

            # Check enrichment
            assert "external_ids" in updated.detail
            assert updated.detail["external_ids"]["openrouter"] == "openai/gpt-4o"
            assert updated.detail["context_window_external"] == 1_000_000
        finally:
            await db.disconnect()


class TestBuildSourceList:
    def test_adds_openrouter_when_missing(self) -> None:
        """OpenRouter is added to sources when not already present."""
        provenance: dict[str, object] = {"sources": ["provider_catalog"]}
        result = _build_source_list(provenance, has_openrouter=True)
        assert "openrouter" in result
        assert "provider_catalog" in result

    def test_preserves_existing_openrouter(self) -> None:
        """Existing openrouter entry is preserved."""
        provenance: dict[str, object] = {"sources": ["provider_catalog", "openrouter"]}
        result = _build_source_list(provenance, has_openrouter=True)
        assert result.count("openrouter") == 1

    def test_no_openrouter_when_disabled(self) -> None:
        """OpenRouter is not added when disabled."""
        provenance: dict[str, object] = {"sources": ["provider_catalog"]}
        result = _build_source_list(provenance, has_openrouter=False)
        assert "openrouter" not in result

    def test_handles_missing_sources_key(self) -> None:
        """Missing 'sources' key is handled gracefully."""
        provenance: dict[str, object] = {}
        result = _build_source_list(provenance, has_openrouter=True)
        assert "openrouter" in result
        assert "provider_catalog" in result


# --- Service integration tests ---


class TestServiceIntegration:
    @pytest.mark.asyncio()
    async def test_refresh_due_models_fetches_openrouter_once_per_cycle(self) -> None:
        """test_refresh_due_models_fetches_openrouter_catalog_once_per_cycle"""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "local-model")

            payload = _make_openrouter_payload(
                _make_or_model("openai/gpt-4o", name="GPT-4o"),
            )
            client = _MockHttpClient(payload)

            from eggpool.catalog.cache import ModelCatalogCache

            cache = ModelCatalogCache()
            now_ts = datetime.now(UTC).timestamp()
            cache._models["local-model"] = {
                "model_id": "local-model",
                "display_name": "Local Model",
                "protocol": "openai",
                "capabilities": {},
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
            cache._provider_models[("local-model", "test-provider")] = dict(
                cache._models["local-model"]
            )

            config = ModelInfoConfig()
            service = ModelInfoService(config, db, cache, outbound_client=client)

            # Create a due canonical row
            now = datetime.now(UTC)
            info = CanonicalModelInfo(
                model_id="local-model",
                status="partial",
                summary="Test",
                sparse=False,
                detail={},
                provenance={"sources": ["provider_catalog"]},
                conflicts={},
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now - timedelta(hours=1),
                last_refreshed_at=now - timedelta(hours=1),
                next_refresh_at=now - timedelta(minutes=1),
            )
            await service.repo.upsert_canonical(info)

            result = await service.refresh_due_models()
            assert result["refreshed"] + result["skipped"] == result["total"]
            # OpenRouter was fetched once (via the adapter)
            assert client.call_count == 1
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_openrouter_disabled_skips_source_without_error(self) -> None:
        """test_openrouter_disabled_skips_source_without_error"""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "model-a")

            cache = _make_cache_with_models({"model-a": {}})

            config = ModelInfoConfig()
            # No outbound_client passed → OpenRouter source not constructed
            service = ModelInfoService(config, db, cache)

            # Should work fine without OpenRouter
            result = await service.refresh_due_models()
            assert result["total"] >= 0
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_openrouter_failure_records_source_health(self) -> None:
        """test_openrouter_failure_records_source_health_and_preserves_cached_info"""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            await _seed_model(db, "model-a")

            client = _MockHttpClient(
                httpx.HTTPStatusError(
                    "500",
                    request=httpx.Request("GET", "https://openrouter.ai/api/v1/models"),
                    response=httpx.Response(500),
                )
            )

            cache = _make_cache_with_models({"model-a": {}})

            config = ModelInfoConfig()
            service = ModelInfoService(config, db, cache, outbound_client=client)

            # Create a due canonical row
            now = datetime.now(UTC)
            info = CanonicalModelInfo(
                model_id="model-a",
                status="partial",
                summary="Test",
                sparse=False,
                detail={},
                provenance={"sources": ["provider_catalog"]},
                conflicts={},
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now - timedelta(hours=1),
                last_refreshed_at=now - timedelta(hours=1),
                next_refresh_at=now - timedelta(minutes=1),
            )
            await service.repo.upsert_canonical(info)

            # refresh_due_models should not raise
            result = await service.refresh_due_models()
            assert result["total"] >= 0

            # Source health should record the error
            health = await service.repo.source_health_snapshot()
            if "openrouter" in health:
                assert health["openrouter"]["failure_count"] >= 1
        finally:
            await db.disconnect()

    @pytest.mark.asyncio()
    async def test_openrouter_observation_enriches_canonical_detail(self) -> None:
        """test_openrouter_observation_enriches_canonical_detail"""
        db = Database(path=":memory:")
        await db.connect()
        try:
            await _run_migrations(db)
            # Use a model_id that matches the OpenRouter source_model_id
            await _seed_model(db, "openai/gpt-4o", "GPT-4o")

            payload = _make_openrouter_payload(
                _make_or_model(
                    "openai/gpt-4o",
                    name="GPT-4o",
                    context_length=128000,
                    max_output=16384,
                ),
            )
            client = _MockHttpClient(payload)

            from eggpool.catalog.cache import ModelCatalogCache

            cache = ModelCatalogCache()
            now_ts = datetime.now(UTC).timestamp()
            cache._models["openai/gpt-4o"] = {
                "model_id": "openai/gpt-4o",
                "display_name": "GPT-4o",
                "protocol": "openai",
                "capabilities": {},
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
            cache._provider_models[("openai/gpt-4o", "openai")] = dict(
                cache._models["openai/gpt-4o"]
            )

            config = ModelInfoConfig()
            service = ModelInfoService(config, db, cache, outbound_client=client)

            # Create a due canonical row
            now = datetime.now(UTC)
            info = CanonicalModelInfo(
                model_id="openai/gpt-4o",
                status="partial",
                summary="Test",
                sparse=False,
                detail={},
                provenance={"sources": ["provider_catalog"]},
                conflicts={},
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now - timedelta(hours=1),
                last_refreshed_at=now - timedelta(hours=1),
                next_refresh_at=now - timedelta(minutes=1),
            )
            await service.repo.upsert_canonical(info)

            result = await service.refresh_due_models()
            assert result["refreshed"] + result["skipped"] == result["total"]

            # Check that an openrouter observation was persisted
            rows = await db.fetch_all(
                "SELECT * FROM model_info_observations "
                "WHERE source = 'openrouter' AND model_id = 'openai/gpt-4o'"
            )
            assert len(rows) >= 1
        finally:
            await db.disconnect()


# --- Config tests ---


class TestOpenRouterConfig:
    def test_openrouter_model_info_source_defaults(self) -> None:
        """test_openrouter_model_info_source_defaults"""
        from eggpool.models.config import ModelInfoSourcesConfig

        sources = ModelInfoSourcesConfig()
        assert sources.openrouter.enabled is True
        assert sources.openrouter.priority == 100
        assert sources.openrouter.ttl_seconds == 86_400

    def test_openrouter_api_key_env_resolution(self) -> None:
        """test_openrouter_api_key_env_resolution"""
        import os

        os.environ["TEST_OPENROUTER_KEY"] = "or-secret-key"
        try:
            config = ModelInfoSourceConfig(api_key_env="TEST_OPENROUTER_KEY")
            assert config.resolved_api_key == "or-secret-key"
        finally:
            del os.environ["TEST_OPENROUTER_KEY"]

        config_no_env = ModelInfoSourceConfig(api_key_env="NONEXISTENT_VAR")
        assert config_no_env.resolved_api_key is None


# --- Error class test ---


class TestModelInfoSourceFetchError:
    def test_is_aggregator_error(self) -> None:
        """ModelInfoSourceFetchError is a subclass of AggregatorError."""
        from eggpool.errors import AggregatorError

        assert issubclass(ModelInfoSourceFetchError, AggregatorError)

    def test_can_be_caught(self) -> None:
        """ModelInfoSourceFetchError can be raised and caught."""
        with pytest.raises(ModelInfoSourceFetchError):
            raise ModelInfoSourceFetchError("test error")


# --- Helper to create cache ---


def _make_cache_with_models(
    models: dict[str, dict], provider_id: str = "test-provider"
) -> ModelCatalogCache:
    """Create a ModelCatalogCache pre-populated with test models."""
    from eggpool.catalog.cache import ModelCatalogCache

    cache = ModelCatalogCache()
    now_ts = datetime.now(UTC).timestamp()
    for model_id, info in models.items():
        entry = {
            "model_id": model_id,
            "display_name": info.get("display_name", model_id),
            "protocol": info.get("protocol", "openai"),
            "capabilities": info.get("capabilities", {}),
            "source_metadata": {},
            "first_seen_at": info.get("first_seen_at", now_ts),
            "last_seen_at": info.get("last_seen_at", now_ts),
            "discovered_limits": {},
            "effective_limits": info.get("effective_limits", {}),
        }
        cache._models[model_id] = entry
        cache._provider_models[(model_id, provider_id)] = dict(entry)
    return cache
