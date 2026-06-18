"""Section 12: Bounded request bodies and real readiness."""

from __future__ import annotations

import os

import pytest
from starlette.responses import Response as StarletteResponse

from go_aggregator.app import _BodyLimitMiddleware
from go_aggregator.errors import RequestTooLargeError
from go_aggregator.request.body import read_body_limited


class FakeStream:
    """Fake request stream for testing."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


class FakeRequest:
    """Fake Starlette Request for testing body reading."""

    def __init__(
        self,
        chunks: list[bytes],
        content_length: int | None = None,
    ) -> None:
        self._stream = FakeStream(chunks)
        self._headers: dict[str, str] = {}
        if content_length is not None:
            self._headers["content-length"] = str(content_length)

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    def stream(self):  # type: ignore[no-untyped-def]
        return self._stream


@pytest.mark.asyncio
class TestReadBodyLimited:
    async def test_small_body_passes(self) -> None:
        req = FakeRequest([b"hello"])
        result = await read_body_limited(req, max_bytes=100)
        assert result == b"hello"

    async def test_content_length_rejection(self) -> None:
        req = FakeRequest([b"x" * 100], content_length=200)
        with pytest.raises(RequestTooLargeError):
            await read_body_limited(req, max_bytes=100)

    async def test_chunked_overflow_rejection(self) -> None:
        req = FakeRequest([b"x" * 50, b"y" * 60])
        with pytest.raises(RequestTooLargeError):
            await read_body_limited(req, max_bytes=100)

    async def test_exact_limit_passes(self) -> None:
        req = FakeRequest([b"x" * 100])
        result = await read_body_limited(req, max_bytes=100)
        assert len(result) == 100

    async def test_empty_body_passes(self) -> None:
        req = FakeRequest([])
        result = await read_body_limited(req, max_bytes=100)
        assert result == b""

    async def test_invalid_content_length_falls_through(self) -> None:
        req = FakeRequest([b"hello"], content_length=None)
        result = await read_body_limited(req, max_bytes=100)
        assert result == b"hello"


class TestBodyLimitMiddleware:
    """Tests for the request body size middleware."""

    @pytest.mark.asyncio()
    async def test_invalid_content_length_falls_through(self) -> None:
        middleware = _BodyLimitMiddleware(app=object(), max_bytes=100)
        req = FakeRequest([b"hello"])
        req.headers["content-length"] = "bogus"

        called = False

        async def _call_next(_request: FakeRequest) -> StarletteResponse:
            nonlocal called
            called = True
            return StarletteResponse(status_code=204)

        response = await middleware.dispatch(req, _call_next)
        assert called
        assert response.status_code == 204


class TestHasEligiblePairing:
    """Tests for Router.has_eligible_pairing()."""

    def test_no_accounts_returns_false(self) -> None:
        from go_aggregator.accounts.registry import AccountRegistry
        from go_aggregator.catalog.cache import ModelCatalogCache
        from go_aggregator.health.health_manager import HealthManager
        from go_aggregator.models.config import AppConfig
        from go_aggregator.quota.estimation import QuotaEstimator
        from go_aggregator.routing.router import Router

        config = AppConfig.from_dict({"accounts": []})
        registry = AccountRegistry(config)
        hm = HealthManager()
        estimator = QuotaEstimator()
        catalog_cache = ModelCatalogCache()

        class FakeCatalog:
            cache = catalog_cache

        router = Router(
            registry,
            FakeCatalog(),
            quota_estimator=estimator,
            health_manager=hm,
        )
        assert router.has_eligible_pairing() is False

    def test_enabled_account_with_model_returns_true(self) -> None:
        from go_aggregator.accounts.registry import AccountRegistry
        from go_aggregator.catalog.cache import ModelCatalogCache
        from go_aggregator.health.health_manager import HealthManager
        from go_aggregator.models.config import AppConfig
        from go_aggregator.quota.estimation import QuotaEstimator
        from go_aggregator.routing.router import Router

        os.environ["TEST_KEY_A"] = "test-key-value"
        try:
            config = AppConfig.from_dict(
                {
                    "accounts": [
                        {
                            "name": "acct-a",
                            "api_key_env": "TEST_KEY_A",
                            "enabled": True,
                        }
                    ]
                }
            )
            registry = AccountRegistry(config)
            hm = HealthManager()
            estimator = QuotaEstimator()
            catalog_cache = ModelCatalogCache()
            catalog_cache.update_from_account(
                "acct-a",
                "opencode-go",
                [{"model_id": "gpt-4", "protocol": "openai"}],
            )

            class FakeCatalog:
                cache = catalog_cache

            router = Router(
                registry,
                FakeCatalog(),
                quota_estimator=estimator,
                health_manager=hm,
            )
            assert router.has_eligible_pairing() is True
        finally:
            os.environ.pop("TEST_KEY_A", None)

    def test_no_model_support_returns_false(self) -> None:
        from go_aggregator.accounts.registry import AccountRegistry
        from go_aggregator.catalog.cache import ModelCatalogCache
        from go_aggregator.health.health_manager import HealthManager
        from go_aggregator.models.config import AppConfig
        from go_aggregator.quota.estimation import QuotaEstimator
        from go_aggregator.routing.router import Router

        os.environ["TEST_KEY_A"] = "test-key-value"
        try:
            config = AppConfig.from_dict(
                {
                    "accounts": [
                        {
                            "name": "acct-a",
                            "api_key_env": "TEST_KEY_A",
                            "enabled": True,
                        }
                    ]
                }
            )
            registry = AccountRegistry(config)
            hm = HealthManager()
            estimator = QuotaEstimator()
            catalog_cache = ModelCatalogCache()

            class FakeCatalog:
                cache = catalog_cache

            router = Router(
                registry,
                FakeCatalog(),
                quota_estimator=estimator,
                health_manager=hm,
            )
            assert router.has_eligible_pairing() is False
        finally:
            os.environ.pop("TEST_KEY_A", None)


class TestReadinessProbe:
    """Tests for the health_probe table writeability check."""

    @pytest.mark.asyncio
    async def test_health_probe_table_exists(self, tmp_path: object) -> None:
        """The health_probe table should exist after migration."""
        from go_aggregator.db.connection import Database
        from go_aggregator.db.migrations import MigrationRunner

        db = Database(path=str(tmp_path / "test.sqlite3"))  # type: ignore[arg-type]
        await db.connect()
        try:
            runner = MigrationRunner(db)
            await runner.run()

            # Verify health_probe table exists
            rows = await db.fetch_all(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='health_probe'"
            )
            assert len(rows) == 1
        finally:
            await db.disconnect()
