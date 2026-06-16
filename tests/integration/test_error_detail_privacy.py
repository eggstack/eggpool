"""End-to-end privacy regression for persisted error detail.

Phase 17 makes ``persist_redacted_error_detail`` opt-in. The
default (fail-closed) writes ``NULL`` for ``error_detail`` so
arbitrary provider detail never reaches the database. When
explicitly enabled, the strengthened redactor must cover common
JSON credential forms. Phase 18 switches structured sanitization
to a strict allowlist policy so arbitrary provider payload keys
(e.g. ``payload``, ``body``, ``context``, ``data``, ``details``,
``debug``) cannot be retained even when diagnostic persistence
is explicitly enabled.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio

from go_aggregator.db.connection import Database
from go_aggregator.db.migrations import MigrationRunner
from go_aggregator.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from go_aggregator.request.attempt_finalizer import (
    AttemptFinalizationData,
    AttemptFinalizer,
)
from go_aggregator.request.finalizer import (
    FinalizationData,
    FinalizationOutcome,
    RequestFinalizer,
)

SECRET_BEARING_INPUT: dict[str, Any] = {
    "api_key": "sk-supersecret-1",
    "authorization": "Bearer topsecret-token",
    "password": "hunter2",
    "token": "another-secret",
    "messages": [{"role": "user", "content": "private prompt"}],
    "input": "private input",
    "prompt": "private prompt text",
    "type": "api_error",
    "code": "rate_limit",
    "message": "limit reached",
}

FORBIDDEN_MARKERS = (
    "sk-supersecret-1",
    "topsecret-token",
    "hunter2",
    "another-secret",
    "private prompt",
    "private input",
    "private prompt text",
)


class _Selected:
    def __init__(
        self,
        *,
        db_request_id: str,
        attempt_id: int,
        reservation_id: str,
    ) -> None:
        self.db_request_id = db_request_id
        self.account_name = "test-acct"
        self.model_id = "gpt-4"
        self.attempt_id = attempt_id
        self.reservation_id = reservation_id
        self.estimated_microdollars = 100_000
        self.attempt_number = 1


@pytest_asyncio.fixture()
async def db_with_seed() -> Any:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "TEST_KEY"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
    yield database
    await database.disconnect()


class TestFailClosedByDefault:
    @pytest.mark.asyncio
    async def test_request_finalizer_writes_null_by_default(
        self, db_with_seed: Database
    ) -> None:
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="closed-req",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db_with_seed,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
        )
        selected = _Selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_class="UpstreamError",
                error_detail=str(SECRET_BEARING_INPUT),
            ),
        )

        row = await db_with_seed.fetch_one(
            "SELECT error_detail, error_class FROM requests WHERE id = ?",
            (db_id,),
        )
        assert row is not None
        assert row["error_detail"] is None
        # error_class is still set so health and stats can classify.
        assert row["error_class"] == "UpstreamError"

    @pytest.mark.asyncio
    async def test_attempt_finalizer_writes_null_by_default(
        self, db_with_seed: Database
    ) -> None:
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="closed-attempt",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        af = AttemptFinalizer(
            db=db_with_seed,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
        )
        await af.finalize_failed_attempt(
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            data=AttemptFinalizationData(
                error_detail=str(SECRET_BEARING_INPUT),
                error_class="UpstreamError",
            ),
        )

        row = await db_with_seed.fetch_one(
            "SELECT error_detail, error_class FROM request_attempts WHERE id = ?",
            (attempt_id,),
        )
        assert row is not None
        assert row["error_detail"] is None


class TestPersistsRedactedWhenEnabled:
    @pytest.mark.asyncio
    async def test_request_finalizer_persists_redacted_detail(
        self, db_with_seed: Database
    ) -> None:
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="enabled-req",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db_with_seed,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=True,
        )
        selected = _Selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )
        import json as _json

        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_detail=_json.dumps(SECRET_BEARING_INPUT),
            ),
        )

        row = await db_with_seed.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        for marker in FORBIDDEN_MARKERS:
            assert marker not in detail, f"Marker {marker!r} found in persisted detail"

    @pytest.mark.asyncio
    async def test_attempt_finalizer_persists_redacted_detail(
        self, db_with_seed: Database
    ) -> None:
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="enabled-attempt",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        af = AttemptFinalizer(
            db=db_with_seed,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=True,
        )
        import json as _json

        await af.finalize_failed_attempt(
            attempt_id=attempt_id,
            reservation_id=reservation_id,
            data=AttemptFinalizationData(
                error_detail=_json.dumps(SECRET_BEARING_INPUT),
                error_class="UpstreamError",
            ),
        )

        row = await db_with_seed.fetch_one(
            "SELECT error_detail FROM request_attempts WHERE id = ?",
            (attempt_id,),
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        for marker in FORBIDDEN_MARKERS:
            assert marker not in detail, f"Marker {marker!r} found in persisted detail"


class TestAllowlistPolicyPersists:
    """Phase 18: allowlist-safe structured persistence.

    The optional diagnostic persistence path is restricted to a
    strict allowlist of diagnostic keys. Arbitrary provider payload
    keys (``payload``, ``body``, ``context``, ``data``, ``details``,
    ``debug``) are dropped. Sensitive and user-content keys are
    retained as ``[REDACTED]`` to preserve diagnostic shape.
    """

    @pytest.mark.asyncio
    async def test_default_finalizer_persists_null_when_disabled(
        self, db_with_seed: Database
    ) -> None:
        """Default finalizer behavior persists NULL."""
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="allowlist-default",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db_with_seed,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=False,
        )
        selected = _Selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_class="UpstreamError",
                error_detail=json.dumps(
                    {
                        "type": "invalid_request",
                        "message": "bad token sk-secret",
                        "payload": "private source code body",
                    }
                ),
            ),
        )
        row = await db_with_seed.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        assert row["error_detail"] is None

    @pytest.mark.asyncio
    async def test_payload_with_private_source_code_is_dropped(
        self, db_with_seed: Database
    ) -> None:
        """``payload`` is dropped entirely even when persistence is enabled."""
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="allowlist-payload",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db_with_seed,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=True,
        )
        selected = _Selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_class="UpstreamError",
                error_detail=json.dumps(
                    {
                        "type": "invalid_request",
                        "message": "bad token sk-supersecret-payload",
                        "payload": "private source code body",
                    }
                ),
            ),
        )
        row = await db_with_seed.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        assert "payload" not in detail
        assert "private source code body" not in detail
        assert "sk-supersecret-payload" not in detail
        parsed = json.loads(detail)
        assert parsed == {
            "type": "invalid_request",
            "message": "bad token [REDACTED]",
        }

    @pytest.mark.asyncio
    async def test_data_details_and_nested_unknown_keys_are_dropped(
        self, db_with_seed: Database
    ) -> None:
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="allowlist-data",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db_with_seed,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=True,
        )
        selected = _Selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_class="UpstreamError",
                error_detail=json.dumps(
                    {
                        "type": "api_error",
                        "code": "rate_limit",
                        "data": {"api_key": "sk-private-data"},
                        "details": {"context": "private debug info"},
                        "debug": "internal trace",
                        "error": {
                            "type": "nested",
                            "context": "private context",
                        },
                    }
                ),
            ),
        )
        row = await db_with_seed.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        for forbidden in (
            '"data"',
            '"details"',
            '"debug"',
            '"error"',
            '"context"',
            "sk-private-data",
            "private debug info",
            "private context",
            "internal trace",
        ):
            assert forbidden not in detail, f"{forbidden!r} present in persisted detail"
        parsed = json.loads(detail)
        assert parsed == {"type": "api_error", "code": "rate_limit"}

    @pytest.mark.asyncio
    async def test_safe_diagnostic_keys_are_retained(
        self, db_with_seed: Database
    ) -> None:
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="allowlist-safe",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db_with_seed,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=True,
        )
        selected = _Selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_class="UpstreamError",
                error_detail=json.dumps(
                    {
                        "type": "api_error",
                        "code": "rate_limit",
                        "status": 429,
                        "status_code": 429,
                        "error_type": "rate_limit_error",
                        "kind": "rate_limit",
                        "param": "max_tokens",
                        "request_id": "req-abc",
                        "trace_id": "trace-xyz",
                    }
                ),
            ),
        )
        row = await db_with_seed.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        parsed = json.loads(detail)
        assert parsed == {
            "type": "api_error",
            "code": "rate_limit",
            "status": 429,
            "status_code": 429,
            "error_type": "rate_limit_error",
            "kind": "rate_limit",
            "param": "max_tokens",
            "request_id": "req-abc",
            "trace_id": "trace-xyz",
        }

    @pytest.mark.asyncio
    async def test_message_is_redacted_and_bounded(
        self, db_with_seed: Database
    ) -> None:
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="allowlist-message",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db_with_seed,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=True,
        )
        selected = _Selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )
        long_message = "sk-supersecret-message " * 500
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_class="UpstreamError",
                error_detail=json.dumps({"type": "api_error", "message": long_message}),
            ),
        )
        row = await db_with_seed.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        # The persisted string itself is bounded.
        assert len(detail) <= 2048
        assert "sk-supersecret-message" not in detail
        parsed = json.loads(detail)
        # ``message`` was redacted token-by-token and per-string bounded.
        assert len(parsed["message"]) <= 1024 + 3

    @pytest.mark.asyncio
    async def test_top_level_array_is_fail_closed(self, db_with_seed: Database) -> None:
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="allowlist-array",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db_with_seed,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=True,
        )
        selected = _Selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_class="UpstreamError",
                error_detail=json.dumps(
                    [
                        {"api_key": "sk-array-1"},
                        {"token": "sk-array-2"},
                    ]
                ),
            ),
        )
        row = await db_with_seed.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        # Top-level array is collapsed to a single REDACTED marker.
        assert detail == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_prompt_completion_input_api_key_token_auth_markers_absent(
        self, db_with_seed: Database
    ) -> None:
        """Sensitive and user-content keys appear only as REDACTED."""
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)
        async with db_with_seed.transaction():
            db_id = await request_repo.create_pending(
                request_id="allowlist-markers",
                model_id="gpt-4",
                protocol="openai",
                streamed=False,
                account_id=1,
            )
            attempt_id = await attempt_repo.create(
                request_id=db_id, attempt_number=1, account_id=1
            )
            reservation_id = await reservation_repo.create(
                request_id=db_id,
                account_id=1,
                model_id="gpt-4",
                estimated_tokens=1000,
                estimated_microdollars=100_000,
                ttl_seconds=300,
            )

        finalizer = RequestFinalizer(
            db=db_with_seed,
            request_repo=request_repo,
            attempt_repo=attempt_repo,
            reservation_repo=reservation_repo,
            persist_error_detail=True,
        )
        selected = _Selected(
            db_request_id=db_id,
            attempt_id=attempt_id,
            reservation_id=reservation_id,
        )
        await finalizer.finalize(
            selected,
            FinalizationData(
                outcome=FinalizationOutcome.UPSTREAM_ERROR,
                error_class="UpstreamError",
                error_detail=json.dumps(
                    {
                        "type": "api_error",
                        "code": "rate_limit",
                        "prompt": "private prompt",
                        "completion": "private completion",
                        "messages": "private messages",
                        "input": "private input",
                        "api_key": "private-api-key",
                        "token": "private-token",
                        "authorization": "Bearer private-auth",
                    }
                ),
            ),
        )
        row = await db_with_seed.fetch_one(
            "SELECT error_detail FROM requests WHERE id = ?", (db_id,)
        )
        assert row is not None
        detail = row["error_detail"]
        assert detail is not None
        for forbidden in (
            "private prompt",
            "private completion",
            "private messages",
            "private input",
            "private-api-key",
            "private-token",
            "private-auth",
        ):
            assert forbidden not in detail, (
                f"Marker {forbidden!r} present in persisted detail"
            )
        parsed = json.loads(detail)
        assert parsed["prompt"] == "[REDACTED]"
        assert parsed["completion"] == "[REDACTED]"
        assert parsed["messages"] == "[REDACTED]"
        assert parsed["input"] == "[REDACTED]"
        assert parsed["api_key"] == "[REDACTED]"
        assert parsed["token"] == "[REDACTED]"
        assert parsed["authorization"] == "[REDACTED]"

    @pytest.mark.asyncio
    async def test_database_wide_secret_scan_passes(
        self, db_with_seed: Database
    ) -> None:
        """A full database scan must find no secret markers after
        ``persist_error_detail=True`` has been used to persist a
        variety of inputs.
        """
        request_repo = RequestRepository(db_with_seed)
        attempt_repo = AttemptRepository(db_with_seed)
        reservation_repo = ReservationRepository(db_with_seed)

        secret_payloads: list[dict[str, Any]] = [
            {
                "type": "invalid_request",
                "message": "bad token sk-supersecret-1",
                "payload": "private source code body",
                "data": {"api_key": "sk-private-2"},
                "details": {"context": "private debug info"},
            },
            {
                "type": "api_error",
                "code": "rate_limit",
                "prompt": "private prompt",
                "completion": "private completion",
                "input": "private input",
                "messages": "private messages",
                "api_key": "sk-private-3",
                "token": "sk-private-4",
                "authorization": "Bearer sk-private-5",
            },
        ]

        for index, payload in enumerate(secret_payloads):
            async with db_with_seed.transaction():
                db_id = await request_repo.create_pending(
                    request_id=f"scan-{index}",
                    model_id="gpt-4",
                    protocol="openai",
                    streamed=False,
                    account_id=1,
                )
                attempt_id = await attempt_repo.create(
                    request_id=db_id, attempt_number=1, account_id=1
                )
                reservation_id = await reservation_repo.create(
                    request_id=db_id,
                    account_id=1,
                    model_id="gpt-4",
                    estimated_tokens=1000,
                    estimated_microdollars=100_000,
                    ttl_seconds=300,
                )

            finalizer = RequestFinalizer(
                db=db_with_seed,
                request_repo=request_repo,
                attempt_repo=attempt_repo,
                reservation_repo=reservation_repo,
                persist_error_detail=True,
            )
            selected = _Selected(
                db_request_id=db_id,
                attempt_id=attempt_id,
                reservation_id=reservation_id,
            )
            await finalizer.finalize(
                selected,
                FinalizationData(
                    outcome=FinalizationOutcome.UPSTREAM_ERROR,
                    error_class="UpstreamError",
                    error_detail=json.dumps(payload),
                ),
            )

        forbidden_markers = (
            "sk-supersecret-1",
            "private source code body",
            "sk-private-2",
            "private debug info",
            "private prompt",
            "private completion",
            "private input",
            "private messages",
            "sk-private-3",
            "sk-private-4",
            "sk-private-5",
        )
        for table in ("requests", "request_attempts"):
            rows = await db_with_seed.fetch_all(
                f"SELECT error_detail FROM {table} WHERE error_detail IS NOT NULL"
            )
            assert rows, f"No persisted details in {table}"
            for row in rows:
                detail = row["error_detail"]
                for marker in forbidden_markers:
                    assert marker not in detail, (
                        f"Marker {marker!r} found in {table}: {detail!r}"
                    )
