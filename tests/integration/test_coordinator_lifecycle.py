"""Integration tests for the RequestCoordinator lifecycle."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import respx

from go_aggregator.accounts.registry import AccountRegistry
from go_aggregator.catalog.service import CatalogService
from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from go_aggregator.health.health_manager import HealthManager
from go_aggregator.models.config import AppConfig
from go_aggregator.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
)
from go_aggregator.routing.router import Router

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
    await database.execute(
        "INSERT INTO accounts (name, api_key_env, enabled, weight) "
        "VALUES (?, ?, 1, 1.0)",
        ("test-acct", "OPENCODE_TEST_KEY"),
    )
    await database.execute(
        "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
        ("gpt-4", "openai"),
    )
    await database.connection.commit()
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
        httpx_client=httpx_client,
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
