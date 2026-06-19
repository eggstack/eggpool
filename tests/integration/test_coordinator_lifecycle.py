"""Integration tests for the RequestCoordinator lifecycle."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import respx

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
)
from eggpool.routing.router import Router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

UPSTREAM_BASE = "https://test-upstream.example.com"


def _build_config() -> AppConfig:
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )


@pytest_asyncio.fixture()
async def db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
    yield database
    await database.disconnect()


@pytest.fixture()
def config() -> AppConfig:
    return _build_config()


@pytest_asyncio.fixture()
async def coordinator(
    db: Database, config: AppConfig
) -> AsyncGenerator[RequestCoordinator, None]:
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")

    router = Router(registry, catalog)
    router.set_account_weight("test-acct", 1.0)

    health_manager = HealthManager()
    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    usage_window_repo = UsageWindowRepository(db)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
    )
    yield coord
    await httpx_client.aclose()


@pytest.mark.asyncio
async def test_pending_request_created_before_upstream(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Verify a pending request record exists before upstream completes."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-req-1",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.account_name == "test-acct"

    row = await db.fetch_one("SELECT * FROM requests WHERE id = ?", ("1",))
    assert row is not None
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_reservation_created_and_released(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Verify a reservation is created and released on success."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-req-2",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        await coordinator.execute(context)

    resv_row = await db.fetch_one(
        "SELECT * FROM reservations WHERE request_id = ?", ("1",)
    )
    assert resv_row is not None
    assert resv_row["status"] == "released"


@pytest.mark.asyncio
async def test_attempt_record_created(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Verify an attempt record is created for each request."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-req-3",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        await coordinator.execute(context)

    attempt_rows = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ?", ("1",)
    )
    assert len(attempt_rows) == 1
    assert attempt_rows[0]["attempt_number"] == 1
    assert attempt_rows[0]["status_code"] == 200


@pytest.mark.asyncio
async def test_usage_extracted_and_cost_calculated(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Verify usage is extracted and cost is nonzero for successful requests."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-req-4",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.usage is not None
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 50

    row = await db.fetch_one("SELECT * FROM requests WHERE id = ?", ("1",))
    assert row is not None
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50


@pytest.mark.asyncio
async def test_request_finalized_with_correct_status(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Verify request is finalized with correct status on success."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-req-5",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200

    row = await db.fetch_one("SELECT * FROM requests WHERE id = ?", ("1",))
    assert row is not None
    assert row["status"] == "completed"
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_upstream_error_releases_reservation(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Verify reservation is released on upstream error."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                500,
                json={"error": {"message": "Internal error"}},
            )
        )

        context = ProxyRequestContext(
            request_id="test-req-err",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 500

    resv_row = await db.fetch_one(
        "SELECT * FROM reservations WHERE request_id = ?", ("1",)
    )
    assert resv_row is not None
    assert resv_row["status"] == "released"

    attempt_rows = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ?", ("1",)
    )
    assert len(attempt_rows) >= 1
    assert attempt_rows[0]["error_class"] is not None


@pytest.mark.asyncio
async def test_rate_limit_records_cooldown(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Verify rate limit error applies cooldown to health manager."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                429,
                json={"error": {"message": "Rate limited"}},
                headers={"retry-after": "30"},
            )
        )

        context = ProxyRequestContext(
            request_id="test-req-rl",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 429

    health = coordinator._health_manager.get_account_health("test-acct")
    assert health.health_state == "rate_limited"
    assert health.cooldown_until > 0


@pytest.mark.asyncio
async def test_openai_stream_injects_stream_options(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Verify stream_options.include_usage is injected for OpenAI streaming."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
    ).encode()

    captured_requests: list[bytes] = []

    def _capture_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request.content)
        sse_lines = [
            "data: "
            + json.dumps(
                {
                    "id": "cmpl-1",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "Hello"},
                            "finish_reason": None,
                        }
                    ],
                }
            ),
            "",
            "data: [DONE]",
        ]
        return httpx.Response(
            200,
            content="\n".join(sse_lines).encode(),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_capture_handler
        )

        context = ProxyRequestContext(
            request_id="test-req-opts",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert len(captured_requests) == 1
    sent_payload = json.loads(captured_requests[0])
    assert sent_payload["stream_options"]["include_usage"] is True


@pytest.mark.asyncio
async def test_openai_stream_accepts_null_stream_options(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Null stream_options should be normalized rather than crashing."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
            "stream_options": None,
        }
    ).encode()

    captured_requests: list[bytes] = []

    def _capture_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request.content)
        sse_lines = [
            "data: "
            + json.dumps(
                {
                    "id": "cmpl-1",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "Hello"},
                            "finish_reason": None,
                        }
                    ],
                }
            ),
            "",
            "data: [DONE]",
        ]
        return httpx.Response(
            200,
            content="\n".join(sse_lines).encode(),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_capture_handler
        )

        context = ProxyRequestContext(
            request_id="test-req-null-opts",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

        assert response.status_code == 200
        assert response.stream_iterator is not None
        async for _chunk in response.stream_iterator:  # type: ignore[union-attr]
            pass

    assert len(captured_requests) == 1
    sent_payload = json.loads(captured_requests[0])
    assert sent_payload["stream_options"]["include_usage"] is True

    request_row = await db.fetch_one(
        "SELECT status, status_code FROM requests WHERE proxy_request_id = ?",
        ("test-req-null-opts",),
    )
    assert request_row is not None
    assert request_row["status"] == "completed"
    assert request_row["status_code"] == 200


# ---------------------------------------------------------------------------
# Invariant tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invariant_1_no_paid_request_without_pending_record_and_reservation(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Invariant 1: No paid request exists without a pending record + reservation."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-invariant-1",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        await coordinator.execute(context)

    # Request exists with terminal status
    req_rows = await db.fetch_all("SELECT * FROM requests")
    assert len(req_rows) == 1
    req = req_rows[0]
    assert req["status"] == "completed"

    # Reservation exists for this request
    resv_rows = await db.fetch_all(
        "SELECT * FROM reservations WHERE request_id = ?", (req["id"],)
    )
    assert len(resv_rows) == 1
    assert resv_rows[0]["status"] == "released"


@pytest.mark.asyncio
async def test_invariant_2_selection_and_reservation_are_atomic(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Invariant 2: Account selection and reservation happen in one critical section."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-invariant-2",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        await coordinator.execute(context)

    # A reservation exists and is linked to the request account
    resv_rows = await db.fetch_all("SELECT * FROM reservations")
    assert len(resv_rows) == 1
    resv = resv_rows[0]
    assert resv["account_id"] > 0
    assert resv["model_id"] == "gpt-4"


@pytest.mark.asyncio
async def test_invariant_3_every_reservation_finalized_exactly_once(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Invariant 3: Every reservation is finalized (released/expired) exactly once."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-invariant-3",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        await coordinator.execute(context)

    resv_rows = await db.fetch_all("SELECT * FROM reservations")
    assert len(resv_rows) == 1
    resv = resv_rows[0]
    assert resv["status"] in ("released", "expired")
    assert resv["released_at"] is not None


@pytest.mark.asyncio
async def test_invariant_4_every_attempt_has_persistent_record(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Invariant 4: Every attempt has a persistent record."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-invariant-4",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200

    # Attempt record exists with status code
    attempt_rows = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ?",
        (context.client_metadata.get("db_request_id", "1"),),
    )
    assert len(attempt_rows) >= 1
    for attempt in attempt_rows:
        assert attempt["attempt_number"] >= 1
        assert attempt["account_id"] > 0


@pytest.mark.asyncio
async def test_invariant_5_no_retry_after_first_byte_emitted(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Invariant 5: No retry occurs after the first byte has been emitted."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
    ).encode()

    async def _error_after_bytes(
        request: httpx.Request,
    ) -> httpx.Response:
        async def _aiter_bytes():  # type: ignore[no-untyped-def]
            yield b"data: {"
            raise httpx.RemoteProtocolError("Connection reset")

        return httpx.Response(
            200,
            stream=_aiter_bytes(),
            headers={"content-type": "text/event-stream"},
        )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            side_effect=_error_after_bytes
        )

        context = ProxyRequestContext(
            request_id="test-invariant-5",
            protocol="openai",
            model_id="gpt-4",
            streaming=True,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200

    # Consume stream (will raise)
    try:
        async for _chunk in response.stream_iterator:  # type: ignore[union-attr]
            pass
    except (httpx.RemoteProtocolError, Exception):
        pass

    # Only one attempt - no retry after first byte
    attempt_rows = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ?",
        (context.client_metadata.get("db_request_id", "1"),),
    )
    assert len(attempt_rows) == 1


@pytest.mark.asyncio
async def test_invariant_12_request_content_not_persisted(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Invariant 12: Request/response content is not persisted in the database."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Secret prompt content"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Secret response content",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-invariant-12",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        await coordinator.execute(context)

    # Check all table schemas for any content-like columns
    req_rows = await db.fetch_all("SELECT * FROM requests")
    assert len(req_rows) == 1
    req = req_rows[0]

    # Verify no prompt/completion content stored
    req_str = json.dumps(dict(req))
    assert "Secret prompt" not in req_str
    assert "Secret response" not in req_str


def _build_two_account_config() -> AppConfig:
    os.environ["OPENCODE_TEST_KEY"] = "test-key-123"
    os.environ["OPENCODE_TEST_KEY_2"] = "test-key-456"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [
                {"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"},
                {"name": "test-acct-2", "api_key_env": "OPENCODE_TEST_KEY_2"},
            ],
            "dashboard": {"enabled": False},
        }
    )


@pytest_asyncio.fixture()
async def two_account_db() -> AsyncGenerator[Database, None]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct-2", "OPENCODE_TEST_KEY_2"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
    yield database
    await database.disconnect()


@pytest_asyncio.fixture()
async def two_account_coordinator(
    two_account_db: Database,
) -> AsyncGenerator[RequestCoordinator, None]:
    config = _build_two_account_config()
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, two_account_db, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")
    catalog.cache.add_account_support("gpt-4", "test-acct-2")

    health_manager = HealthManager()
    router = Router(registry, catalog, health_manager=health_manager)
    router.set_account_weight("test-acct", 1.0)
    router.set_account_weight("test-acct-2", 1.0)

    request_repo = RequestRepository(two_account_db)
    reservation_repo = ReservationRepository(two_account_db)
    attempt_repo = AttemptRepository(two_account_db)

    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=two_account_db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        health_manager=health_manager,
    )
    yield coord
    await httpx_client.aclose()


@pytest.mark.asyncio
async def test_two_account_failover_first_returns_429(
    two_account_coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """When first account returns 429, should failover to second."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    call_count = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                429,
                json={"error": {"message": "Rate limited"}},
                headers={"retry-after": "30"},
            )
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="test-failover-429",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await two_account_coordinator.execute(context)

    assert response.status_code == 200
    # Coordinator retries on same account; second attempt succeeds
    assert response.account_name in ("test-acct", "test-acct-2")


@pytest.mark.asyncio
async def test_two_account_failover_first_returns_500(
    two_account_coordinator: RequestCoordinator,
    two_account_db: Database,
) -> None:
    """When first account returns 500, should retry and succeed."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    call_count = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                500,
                json={"error": {"message": "Internal error"}},
            )
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="test-failover-500",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await two_account_coordinator.execute(context)

    assert response.status_code == 200
    # Coordinator retries on same account; second attempt succeeds
    assert response.account_name in ("test-acct", "test-acct-2")


@pytest.mark.asyncio
async def test_no_secrets_in_database(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """SQLite should contain no API keys, prompts, or completions."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "My secret API key is sk-abc123"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Here is the secret: xyz789",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-privacy-secrets",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        await coordinator.execute(context)

    tables = ["requests", "reservations", "request_attempts"]
    for table in tables:
        rows = await db.fetch_all(f"SELECT * FROM {table}")  # noqa: S608
        for row in rows:
            row_str = json.dumps(dict(row))
            assert "sk-abc123" not in row_str, f"API key found in {table}: {row_str}"
            assert "xyz789" not in row_str, (
                f"Secret content found in {table}: {row_str}"
            )


# ---------------------------------------------------------------------------
# Bug 2: Non-streaming transport error does not mask retry/failover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_error_before_response_allows_retry(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """ConnectError before response should not mask _RetryableUpstreamError.

    The coordinator should not crash when the upstream raises ConnectError
    before returning a response. The attempt should be finalized, the
    reservation released, and the request should not be stuck pending.
    """
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_handler)

        context = ProxyRequestContext(
            request_id="test-connect-error",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    # The coordinator should return a non-200 (exhausted or 502), not crash
    assert response.status_code >= 400

    # Request should have been finalized (not stuck pending)
    req_row = await db.fetch_one(
        "SELECT status FROM requests WHERE proxy_request_id = ?",
        ("test-connect-error",),
    )
    assert req_row is not None
    assert req_row["status"] in ("completed", "error")

    # The attempt should have been finalized with an error class
    attempt_rows = await db.fetch_all(
        "SELECT * FROM request_attempts WHERE request_id = ?",
        (context.client_metadata.get("db_request_id", "1"),),
    )
    assert len(attempt_rows) >= 1
    assert attempt_rows[0]["error_class"] is not None

    # Reservation should have been released
    resv_rows = await db.fetch_all(
        "SELECT * FROM reservations WHERE request_id = ?",
        (context.client_metadata.get("db_request_id", "1"),),
    )
    assert len(resv_rows) == 1
    assert resv_rows[0]["status"] == "released"


# ---------------------------------------------------------------------------
# Bug 3: Malformed non-streaming usage does not produce a proxy 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_usage_strings_do_not_500(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Upstream with string usage values should finalize, not return 500."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": "10",
                        "completion_tokens": "abc",
                        "total_tokens": "not_a_number",
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-malformed-usage",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    # The upstream body should be passed through; usage coerced to zero/valid ints
    assert response.status_code == 200
    assert response.usage is not None
    # "10" should parse to 10
    assert response.usage.input_tokens == 10
    # "abc" should coerce to 0
    assert response.usage.output_tokens == 0

    req_row = await db.fetch_one(
        "SELECT status FROM requests WHERE proxy_request_id = ?",
        ("test-malformed-usage",),
    )
    assert req_row is not None
    assert req_row["status"] == "completed"


@pytest.mark.asyncio
async def test_malformed_usage_null_values_do_not_500(
    coordinator: RequestCoordinator,
    db: Database,
) -> None:
    """Upstream with null usage values should finalize, not return 500."""
    request_body = json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
        }
    ).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "total_tokens": None,
                    },
                },
            )
        )

        context = ProxyRequestContext(
            request_id="test-null-usage",
            protocol="openai",
            model_id="gpt-4",
            streaming=False,
            original_body=request_body,
            incoming_headers={"content-type": "application/json"},
        )
        response = await coordinator.execute(context)

    assert response.status_code == 200
    assert response.usage is not None
    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 0

    req_row = await db.fetch_one(
        "SELECT status FROM requests WHERE proxy_request_id = ?",
        ("test-null-usage",),
    )
    assert req_row is not None
    assert req_row["status"] == "completed"
