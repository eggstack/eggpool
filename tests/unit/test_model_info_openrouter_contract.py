"""Contract tests for the OpenRouter model-info source adapter.

Pins the outbound request shape, header contract, error handling,
and payload-count health recording.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.errors import ModelInfoSourceFetchError
from eggpool.model_info.repository import ModelInfoRepository
from eggpool.model_info.sources.base import SourceTTLCache
from eggpool.model_info.sources.openrouter import (
    OpenRouterModelInfoSource,
    _parse_catalog_payload,
)
from eggpool.models.config import ModelInfoSourceConfig

# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class RecordingClient:
    """Minimal fake async HTTP client that records calls."""

    def __init__(
        self, payload: dict[str, Any] | None = None, status_code: int = 200
    ) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.payload = payload
        self.status_code = status_code

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        self.calls.append((url, headers))
        response = _FakeResponse(self.status_code, self.payload)
        return response


class _FakeResponse:
    """Fake httpx.Response for testing."""

    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=_FakeRequest(),
                response=self,  # type: ignore[arg-type]
            )

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("No JSON payload")
        return self._payload


class _FakeRequest:
    """Minimal fake request for exception construction."""

    def __init__(self) -> None:
        self.method = "GET"
        self.url = "https://openrouter.ai/api/v1/models"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    client: RecordingClient | None = None,
    cache: SourceTTLCache | None = None,
) -> tuple[OpenRouterModelInfoSource, RecordingClient]:
    """Create an OpenRouterModelInfoSource with a recording client."""
    config = ModelInfoSourceConfig(
        base_url=base_url,
        api_key=api_key,
        ttl_seconds=3600,
    )
    c = client or RecordingClient()
    source = OpenRouterModelInfoSource(config=config, client=c, cache=cache)
    return source, c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestURLContract:
    def test_url_is_base_models_endpoint(self) -> None:
        """_url() returns {base_url}/models with trailing slash stripped."""
        source, _ = _make_source(base_url="https://openrouter.ai/api/v1")
        assert source._url() == "https://openrouter.ai/api/v1/models"

    def test_url_strips_trailing_slash(self) -> None:
        """Trailing slash on base_url is stripped before /models."""
        source, _ = _make_source(base_url="https://openrouter.ai/api/v1/")
        assert source._url() == "https://openrouter.ai/api/v1/models"

    def test_url_default_base(self) -> None:
        """Default base URL produces the expected endpoint."""
        source, _ = _make_source(base_url=None)
        assert source._url() == "https://openrouter.ai/api/v1/models"


class TestHeadersContract:
    def test_user_agent_header_set(self) -> None:
        """_headers() always includes User-Agent: eggpool/1.0."""
        source, _ = _make_source()
        headers = source._headers()
        assert headers["User-Agent"] == "eggpool/1.0"

    def test_authorization_header_when_api_key_set(self) -> None:
        """Authorization header is Bearer <key> when resolved_api_key is set."""
        source, _ = _make_source(api_key="test-key-123")
        headers = source._headers()
        assert headers["Authorization"] == "Bearer test-key-123"

    def test_no_authorization_header_when_no_api_key(self) -> None:
        """Authorization header is absent when no API key is configured."""
        source, _ = _make_source(api_key=None)
        headers = source._headers()
        assert "Authorization" not in headers


class TestErrorHandling:
    @pytest.mark.asyncio()
    async def test_non_2xx_raises_model_info_source_fetch_error(self) -> None:
        """A 500 response raises ModelInfoSourceFetchError."""
        client = RecordingClient(status_code=500)
        source, _ = _make_source(client=client)

        with pytest.raises(ModelInfoSourceFetchError, match="fetch failed"):
            await source.fetch_all()

        assert len(client.calls) == 1
        assert client.calls[0][0] == "https://openrouter.ai/api/v1/models"

    @pytest.mark.asyncio()
    async def test_invalid_json_raises_model_info_source_fetch_error(self) -> None:
        """A response with invalid JSON raises ModelInfoSourceFetchError."""
        client = RecordingClient(payload=None, status_code=200)
        source, _ = _make_source(client=client)

        with pytest.raises(ModelInfoSourceFetchError, match="fetch failed"):
            await source.fetch_all()

    @pytest.mark.asyncio()
    async def test_4xx_raises_model_info_source_fetch_error(self) -> None:
        """A 403 response raises ModelInfoSourceFetchError."""
        client = RecordingClient(status_code=403)
        source, _ = _make_source(client=client)

        with pytest.raises(ModelInfoSourceFetchError, match="fetch failed"):
            await source.fetch_all()


class TestPayloadRecording:
    @pytest.mark.asyncio()
    async def test_payload_count_recorded_in_source_health(self) -> None:
        """After a successful fetch, record_source_success with payload_count
        shows up in the health snapshot."""
        payload = {
            "data": [
                {
                    "id": "test/model-a",
                    "name": "Model A",
                    "context_length": 128000,
                },
                {
                    "id": "test/model-b",
                    "name": "Model B",
                    "context_length": 64000,
                },
            ]
        }
        client = RecordingClient(payload=payload, status_code=200)
        cache = SourceTTLCache(ttl_seconds=3600)
        source, _ = _make_source(client=client, cache=cache)

        records = await source.fetch_all()
        assert len(records) == 2

        db = Database(path=":memory:")
        await db.connect()
        try:
            await MigrationRunner(db).run()
            repo = ModelInfoRepository(db)
            await repo.record_source_success("openrouter", payload_count=len(records))

            snapshot = await repo.source_health_snapshot()
            assert "openrouter" in snapshot
            assert snapshot["openrouter"]["last_payload_count"] == 2
        finally:
            await db.disconnect()


class TestParseCatalogPayload:
    def test_valid_payload(self) -> None:
        """Valid OpenRouter payload is parsed into indexed dict."""
        payload = {
            "data": [
                {"id": "model-a", "name": "Model A"},
                {"id": "model-b", "name": "Model B"},
            ]
        }
        entries = _parse_catalog_payload(payload)
        assert "model-a" in entries
        assert "model-b" in entries
        assert entries["model-a"]["name"] == "Model A"

    def test_non_dict_payload(self) -> None:
        """Non-dict payload returns empty dict."""
        assert _parse_catalog_payload([]) == {}
        assert _parse_catalog_payload("string") == {}
        assert _parse_catalog_payload(42) == {}

    def test_missing_data_key(self) -> None:
        """Dict without 'data' key returns empty dict."""
        assert _parse_catalog_payload({"models": []}) == {}

    def test_empty_data_list(self) -> None:
        """Empty data list returns empty dict."""
        assert _parse_catalog_payload({"data": []}) == {}

    def test_entries_without_id_skipped(self) -> None:
        """Entries without 'id' are skipped."""
        payload = {"data": [{"name": "No ID"}, {"id": "valid", "name": "Valid"}]}
        entries = _parse_catalog_payload(payload)
        assert len(entries) == 1
        assert "valid" in entries
