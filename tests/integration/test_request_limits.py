"""Section 12: Bounded request bodies and real readiness."""

from __future__ import annotations

import os

import pytest

from eggpool.app import _BodyLimitMiddleware
from eggpool.errors import RequestTooLargeError
from eggpool.request.body import read_body_limited


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


class FailingDrainStream:
    """Stream that fails only while draining after an oversized chunk."""

    def __init__(self) -> None:
        self._sent = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._sent:
            self._sent = True
            return b"x" * 101
        raise RuntimeError("transport closed")


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

    async def test_oversized_chunk_is_not_added_to_bounded_body(self) -> None:
        req = FakeRequest([b"x" * 101])
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

    async def test_malformed_content_length_still_enforces_stream_limit(self) -> None:
        req = FakeRequest([b"x" * 60, b"y" * 50])
        req._headers["content-length"] = "not-a-number"
        with pytest.raises(RequestTooLargeError):
            await read_body_limited(req, max_bytes=100)

    async def test_failed_drain_is_logged_and_size_error_is_preserved(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        req = FakeRequest([])
        req._stream = FailingDrainStream()

        with (
            caplog.at_level("DEBUG", logger="eggpool.request.body"),
            pytest.raises(RequestTooLargeError),
        ):
            await read_body_limited(req, max_bytes=100)

        assert "Request body drain aborted: transport closed" in caplog.text


class TestBodyLimitMiddleware:
    """Tests for the request body size middleware (raw ASGI)."""

    @pytest.mark.asyncio()
    async def test_invalid_content_length_falls_through(self) -> None:
        """Invalid Content-Length passes through to the downstream app."""
        called = False

        async def _app(scope: object, receive: object, send: object) -> None:
            nonlocal called
            called = True

        middleware = _BodyLimitMiddleware(app=_app, max_bytes=100)
        scope: dict[str, object] = {
            "type": "http",
            "path": "/v1/chat/completions",
            "headers": [[b"content-length", b"bogus"]],
        }
        await middleware(scope, None, None)  # type: ignore[arg-type]
        assert called

    @pytest.mark.asyncio()
    async def test_oversized_request_returns_413(self) -> None:
        """Content-Length exceeding the limit returns a 413 response."""

        async def _app(scope: object, receive: object, send: object) -> None:
            raise AssertionError("downstream app should not be called")

        middleware = _BodyLimitMiddleware(app=_app, max_bytes=100)
        scope: dict[str, object] = {
            "type": "http",
            "path": "/v1/chat/completions",
            "headers": [[b"content-length", b"200"]],
        }
        sent: list[dict[str, object]] = []

        async def _send(message: dict[str, object]) -> None:
            sent.append(message)

        await middleware(scope, None, _send)  # type: ignore[arg-type]
        assert len(sent) == 2
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 413  # type: ignore[index]
        assert sent[1]["type"] == "http.response.body"

    @pytest.mark.asyncio()
    async def test_oversized_messages_path_returns_anthropic_format(self) -> None:
        """413 for /messages uses Anthropic error format."""
        import json as _json

        async def _app(scope: object, receive: object, send: object) -> None:
            raise AssertionError("downstream app should not be called")

        middleware = _BodyLimitMiddleware(app=_app, max_bytes=100)
        scope: dict[str, object] = {
            "type": "http",
            "path": "/v1/messages",
            "headers": [[b"content-length", b"200"]],
        }
        sent: list[dict[str, object]] = []

        async def _send(message: dict[str, object]) -> None:
            sent.append(message)

        await middleware(scope, None, _send)  # type: ignore[arg-type]
        body = sent[1]["body"]
        assert isinstance(body, bytes)
        parsed = _json.loads(body)
        assert parsed["type"] == "error"
        assert parsed["error"]["type"] == "invalid_request_error"

    @pytest.mark.asyncio()
    async def test_no_content_length_falls_through(self) -> None:
        """Requests without Content-Length pass through."""
        called = False

        async def _app(scope: object, receive: object, send: object) -> None:
            nonlocal called
            called = True

        middleware = _BodyLimitMiddleware(app=_app, max_bytes=100)
        scope: dict[str, object] = {
            "type": "http",
            "path": "/v1/chat/completions",
            "headers": [],
        }
        await middleware(scope, None, None)  # type: ignore[arg-type]
        assert called

    @pytest.mark.asyncio()
    async def test_non_http_scope_passthrough(self) -> None:
        """Non-HTTP scopes (websocket, lifespan) pass through unchanged."""
        called = False

        async def _app(scope: object, receive: object, send: object) -> None:
            nonlocal called
            called = True

        middleware = _BodyLimitMiddleware(app=_app, max_bytes=100)
        await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
        assert called


class TestHasEligiblePairing:
    """Tests for Router.has_eligible_pairing()."""

    def test_no_accounts_returns_false(self) -> None:
        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.health.health_manager import HealthManager
        from eggpool.models.config import AppConfig
        from eggpool.quota.estimation import QuotaEstimator
        from eggpool.routing.router import Router

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
        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.health.health_manager import HealthManager
        from eggpool.models.config import AppConfig
        from eggpool.quota.estimation import QuotaEstimator
        from eggpool.routing.router import Router

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
        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.health.health_manager import HealthManager
        from eggpool.models.config import AppConfig
        from eggpool.quota.estimation import QuotaEstimator
        from eggpool.routing.router import Router

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
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

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
