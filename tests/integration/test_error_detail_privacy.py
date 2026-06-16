"""End-to-end privacy regression for persisted error detail.

Phase 17 makes ``persist_redacted_error_detail`` opt-in. The
default (fail-closed) writes ``NULL`` for ``error_detail`` so
arbitrary provider detail never reaches the database. When
explicitly enabled, the strengthened redactor must cover common
JSON credential forms.
"""

from __future__ import annotations

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
            "INSERT OR IGNORE INTO models (model_id, protocol) "
            "VALUES (?, ?)",
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
            "SELECT error_detail, error_class FROM request_attempts "
            "WHERE id = ?",
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
            assert marker not in detail, (
                f"Marker {marker!r} found in persisted detail"
            )

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
            assert marker not in detail, (
                f"Marker {marker!r} found in persisted detail"
            )
