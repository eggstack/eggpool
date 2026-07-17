"""Database consistency audit tests (Workstream G7).

Tests the ConsistencyAuditor for correct invariant checking across
various lifecycle states.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from eggpool.db.consistency_audit import ConsistencyAuditor
from eggpool.request.coordinator import ProxyRequestContext, RequestCoordinator

if TYPE_CHECKING:
    from eggpool.db.connection import Database

pytestmark = [pytest.mark.soak, pytest.mark.db_consistency]

UPSTREAM_BASE = "https://soak-test-upstream.example.com"


async def _non_stream_handler(request: httpx.Request) -> httpx.Response:
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


async def _stream_handler(request: httpx.Request) -> httpx.Response:
    async def _aiter_bytes():  # type: ignore[no-untyped-def]
        yield b"data: "
        yield json.dumps(
            {
                "id": "cmpl-1",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Hi"},
                        "finish_reason": None,
                    }
                ],
            }
        ).encode()
        yield b"\n\n"
        yield b"data: [DONE]\n\n"

    return httpx.Response(
        200,
        stream=_aiter_bytes(),
        headers={"content-type": "text/event-stream"},
    )


async def _consume_stream(stream_iter: object) -> None:
    async for _chunk in stream_iter:  # type: ignore[misc]
        pass


class TestConsistencyAudit:
    """Test the ConsistencyAuditor checks."""

    @pytest.mark.asyncio
    async def test_clean_database_passes_audit(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """A clean database with completed requests should pass audit."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_non_stream_handler
            )
            for i in range(5):
                context = ProxyRequestContext(
                    request_id=f"audit-clean-{i}",
                    protocol="openai",
                    model_id="gpt-4",
                    streaming=False,
                    original_body=json.dumps(
                        {
                            "model": "gpt-4",
                            "messages": [{"role": "user", "content": f"Msg {i}"}],
                        }
                    ).encode(),
                    incoming_headers={"content-type": "application/json"},
                )
                response = await soak_coordinator.execute(context)
                assert response.status_code == 200

        auditor = ConsistencyAuditor(soak_db)
        result = await auditor.run_full_audit()
        assert result.passed, (
            f"Audit failed with {result.failed_count} violations: "
            + "; ".join(v.description for v in result.violations)
        )
        assert result.checks_run > 0

    @pytest.mark.asyncio
    async def test_active_reservation_for_completed_detected(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Audit should detect active reservations on completed requests."""
        with respx.mock:
            respx.post(f"{UPSTREAM_BASE}/chat/completions").mock(
                side_effect=_non_stream_handler
            )
            context = ProxyRequestContext(
                request_id="audit-orphan-resv",
                protocol="openai",
                model_id="gpt-4",
                streaming=False,
                original_body=json.dumps(
                    {
                        "model": "gpt-4",
                        "messages": [{"role": "user", "content": "test"}],
                    }
                ).encode(),
                incoming_headers={"content-type": "application/json"},
            )
            response = await soak_coordinator.execute(context)
            assert response.status_code == 200

        # Manually insert an active reservation for a completed request
        # (simulates a leaked reservation).  Find the integer rowid of
        # the request we just completed via its proxy_request_id.
        row = await soak_db.fetch_one(
            "SELECT id FROM requests WHERE proxy_request_id = ?",
            ("audit-orphan-resv",),
        )
        assert row is not None
        request_rowid: int = row["id"]
        async with soak_db.transaction():
            await soak_db.execute_write(
                """
                INSERT INTO reservations
                    (request_id, account_id, model_id, status,
                     reserved_microdollars)
                VALUES (?, ?, 'gpt-4', 'active', 0)
                """,
                (request_rowid, 1),
            )

        auditor = ConsistencyAuditor(soak_db)
        result = await auditor.run_full_audit()
        assert not result.passed
        violation_names = [v.check_name for v in result.violations]
        assert "active_reservation_for_non_pending" in violation_names

    @pytest.mark.asyncio
    async def test_to_dict_format(
        self,
        soak_coordinator: RequestCoordinator,
        soak_db: Database,
    ) -> None:
        """Audit result should serialize to dict correctly."""
        auditor = ConsistencyAuditor(soak_db)
        result = await auditor.run_full_audit()
        d = result.to_dict()
        assert "passed" in d
        assert "checks_run" in d
        assert "checks_passed" in d
        assert "violations" in d
        assert isinstance(d["violations"], list)

    @pytest.mark.asyncio
    async def test_audit_with_no_rows(
        self,
        soak_db: Database,
    ) -> None:
        """Audit should pass on an empty database."""
        auditor = ConsistencyAuditor(soak_db)
        result = await auditor.run_full_audit()
        assert result.passed
        assert result.checks_run > 0
