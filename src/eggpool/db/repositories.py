"""Repository layer for database operations."""

from __future__ import annotations

import datetime as _dt
import logging
import time
from typing import TYPE_CHECKING, Any

from eggpool.catalog.pricing import (
    parse_microdollars_per_million,
    parse_price_per_1k,
)
from eggpool.constants import DEFAULT_PROVIDER_ID, DEPRECATED_MODEL_ID

if TYPE_CHECKING:
    from eggpool.db.connection import Database

logger = logging.getLogger(__name__)


class AccountRepository:
    """CRUD operations for accounts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def sync_from_config(
        self,
        config_accounts: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Upsert configured accounts, disable removed ones, return name->id map."""
        async with self._db.transaction():
            return await self._sync_from_config_locked(config_accounts)

    async def _sync_from_config_locked(
        self,
        config_accounts: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Upsert configured accounts inside the caller's transaction."""
        name_to_id: dict[str, int] = {}
        configured_names: set[str] = set()

        for acct in config_accounts:
            name = str(acct["name"])
            provider_id = str(acct.get("provider_id") or DEFAULT_PROVIDER_ID)
            configured_names.add(name)
            row = await self._db.fetch_one(
                "SELECT id, provider_id FROM accounts WHERE name = ?",
                (name,),
            )
            if row is not None:
                existing_provider_id = str(row["provider_id"])
                if existing_provider_id != provider_id:
                    logger.warning(
                        "Account %r provider_id changed from %r to %r; "
                        "subsequent routing decisions will use the new provider. "
                        "Run `eggpool logout` and `eggpool connect` to make "
                        "this an explicit, audited change.",
                        name,
                        existing_provider_id,
                        provider_id,
                    )
                await self._db.execute_write(
                    "UPDATE accounts SET api_key_env = ?, enabled = ?, "
                    "weight = ?, provider_id = ? WHERE name = ?",
                    (
                        str(acct["api_key_env"]),
                        int(acct.get("enabled", True)),
                        float(acct.get("weight", 1.0)),
                        provider_id,
                        name,
                    ),
                )
                name_to_id[name] = int(row["id"])
            else:
                last_id = await self._db.execute_insert(
                    "INSERT INTO accounts "
                    "(name, api_key_env, enabled, weight, provider_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        name,
                        str(acct["api_key_env"]),
                        int(acct.get("enabled", True)),
                        float(acct.get("weight", 1.0)),
                        provider_id,
                    ),
                )
                name_to_id[name] = last_id

        existing = await self._db.fetch_all(
            "SELECT id, name FROM accounts WHERE enabled = 1"
        )
        for row in existing:
            if row["name"] not in configured_names:
                await self._db.execute_write(
                    "UPDATE accounts SET enabled = 0 WHERE id = ?",
                    (row["id"],),
                )

        return name_to_id

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        """Fetch a single account by name."""
        row = await self._db.fetch_one(
            "SELECT * FROM accounts WHERE name = ?",
            (name,),
        )
        return dict(row) if row is not None else None

    async def get_id_by_name(self, name: str) -> int | None:
        """Fetch account id by name."""
        row = await self._db.fetch_one(
            "SELECT id FROM accounts WHERE name = ?",
            (name,),
        )
        return int(row["id"]) if row is not None else None

    async def get_name_by_id(self, account_id: int) -> str | None:
        """Fetch account name by id; ``None`` when not found."""
        row = await self._db.fetch_one(
            "SELECT name FROM accounts WHERE id = ?",
            (account_id,),
        )
        return str(row["name"]) if row is not None else None

    async def list_enabled(self) -> list[dict[str, Any]]:
        """List all enabled accounts."""
        rows = await self._db.fetch_all("SELECT * FROM accounts WHERE enabled = 1")
        return [dict(r) for r in rows]


class RequestRepository:
    """CRUD operations for requests."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_pending(
        self,
        request_id: str,
        model_id: str,
        protocol: str,
        streamed: bool,
        account_id: int,
        reserved_microdollars: int = 0,
        started_at: float | None = None,
        provider_id: str = DEFAULT_PROVIDER_ID,
        client_ip: str = "",
    ) -> str:
        """Insert a new pending request, return the id as string.

        The id is the SQLite rowid for the new row. It is returned as
        a string for parity with the rest of the persistence layer
        (callers store it in client_metadata and pass it to other
        repositories that accept a string request id).

        ``account_id`` is required: the requests table declares it
        NOT NULL because every durable request must be associated with
        a concrete account. Callers that need to track a request
        before selection must defer the INSERT until after the
        account has been chosen.
        """
        if started_at is not None:
            import datetime as _dt

            started_at_str = _dt.datetime.fromtimestamp(
                started_at, tz=_dt.UTC
            ).strftime("%Y-%m-%d %H:%M:%S")
            last_id = await self._db.execute_insert(
                "INSERT INTO requests "
                "(account_id, model_id, started_at, status, protocol, "
                "streamed, reserved_microdollars, proxy_request_id, "
                "provider_id, client_ip) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    model_id,
                    started_at_str,
                    protocol,
                    int(streamed),
                    reserved_microdollars,
                    request_id,
                    provider_id,
                    client_ip,
                ),
            )
        else:
            last_id = await self._db.execute_insert(
                "INSERT INTO requests "
                "(account_id, model_id, status, protocol, streamed, "
                "reserved_microdollars, proxy_request_id, provider_id, "
                "client_ip) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    model_id,
                    protocol,
                    int(streamed),
                    reserved_microdollars,
                    request_id,
                    provider_id,
                    client_ip,
                ),
            )
        return str(last_id)

    async def update_after_selection(
        self,
        request_id: str,
        account_id: int,
        reserved_microdollars: int,
    ) -> None:
        """Set the selected account after routing decision."""
        await self._db.execute_write(
            "UPDATE requests SET account_id = ?, reserved_microdollars = ? "
            "WHERE id = ?",
            (account_id, reserved_microdollars, request_id),
        )

    async def update_after_completion(
        self,
        request_id: str,
        status: str,
        status_code: int | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_microdollars: int = 0,
        exactness: str = "unknown",
        upstream_latency_ms: float = 0,
        first_byte_ms: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        upstream_request_id: str | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        thinking_characters: int | None = None,
        retry_count: int = 0,
        provider_cost_microdollars: int | None = None,
        provider_cost_source: str | None = None,
        local_cost_microdollars: int | None = None,
        local_cost_exactness: str | None = None,
    ) -> None:
        """Update request after completion (non-streaming)."""
        await self._db.execute_write(
            "UPDATE requests SET "
            "status = ?, completed_at = CURRENT_TIMESTAMP, "
            "input_tokens = ?, output_tokens = ?, cost_microdollars = ?, "
            "exactness = ?, upstream_latency_ms = ?, first_byte_ms = ?, "
            "error_class = ?, error_detail = ?, upstream_request_id = ?, "
            "cache_read_tokens = ?, cache_write_tokens = ?, "
            "reasoning_tokens = ?, thinking_characters = ?, "
            "retry_count = ?, status_code = ?, "
            "provider_cost_microdollars = ?, provider_cost_source = ?, "
            "local_cost_microdollars = ?, local_cost_exactness = ? "
            "WHERE id = ? AND status = 'pending'",
            (
                status,
                input_tokens,
                output_tokens,
                cost_microdollars,
                exactness,
                upstream_latency_ms,
                first_byte_ms,
                error_class,
                error_detail,
                upstream_request_id,
                cache_read_tokens,
                cache_write_tokens,
                reasoning_tokens,
                thinking_characters,
                retry_count,
                status_code,
                provider_cost_microdollars,
                provider_cost_source,
                local_cost_microdollars,
                local_cost_exactness,
                request_id,
            ),
        )

    async def update_streaming_final(
        self,
        request_id: str,
        status: str,
        status_code: int | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_microdollars: int = 0,
        exactness: str = "unknown",
        first_byte_ms: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        upstream_request_id: str | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        thinking_characters: int | None = None,
        retry_count: int = 0,
        provider_cost_microdollars: int | None = None,
        provider_cost_source: str | None = None,
        local_cost_microdollars: int | None = None,
        local_cost_exactness: str | None = None,
    ) -> None:
        """Update request after streaming completes."""
        await self._db.execute_write(
            "UPDATE requests SET "
            "status = ?, completed_at = CURRENT_TIMESTAMP, "
            "input_tokens = ?, output_tokens = ?, cost_microdollars = ?, "
            "exactness = ?, first_byte_ms = ?, "
            "error_class = ?, error_detail = ?, upstream_request_id = ?, "
            "cache_read_tokens = ?, cache_write_tokens = ?, "
            "reasoning_tokens = ?, thinking_characters = ?, "
            "retry_count = ?, status_code = ?, "
            "provider_cost_microdollars = ?, provider_cost_source = ?, "
            "local_cost_microdollars = ?, local_cost_exactness = ? "
            "WHERE id = ? AND status = 'pending'",
            (
                status,
                input_tokens,
                output_tokens,
                cost_microdollars,
                exactness,
                first_byte_ms,
                error_class,
                error_detail,
                upstream_request_id,
                cache_read_tokens,
                cache_write_tokens,
                reasoning_tokens,
                thinking_characters,
                retry_count,
                status_code,
                provider_cost_microdollars,
                provider_cost_source,
                local_cost_microdollars,
                local_cost_exactness,
                request_id,
            ),
        )

    async def finalize_if_pending(
        self,
        request_id: str,
        status: str,
        status_code: int | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_microdollars: int = 0,
        exactness: str = "unknown",
        first_byte_ms: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        upstream_request_id: str | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        thinking_characters: int | None = None,
        retry_count: int = 0,
        upstream_latency_ms: float = 0,
        bytes_received: int = 0,
        bytes_emitted: int = 0,
        upstream_connect_ms: int | None = None,
        upstream_read_ms: int | None = None,
        coordinator_overhead_ms: int | None = None,
        provider_cost_microdollars: int | None = None,
        provider_cost_source: str | None = None,
        local_cost_microdollars: int | None = None,
        local_cost_exactness: str | None = None,
        upstream_protocol: str | None = None,
    ) -> bool:
        """Finalize a request only if it is still pending.

        Returns True if the row was updated (transition performed),
        False if the request was already terminal (idempotent).
        """
        rowcount = await self._db.execute_write(
            "UPDATE requests SET "
            "status = ?, completed_at = CURRENT_TIMESTAMP, "
            "input_tokens = ?, output_tokens = ?, cost_microdollars = ?, "
            "exactness = ?, first_byte_ms = ?, "
            "error_class = ?, error_detail = ?, upstream_request_id = ?, "
            "cache_read_tokens = ?, cache_write_tokens = ?, "
            "reasoning_tokens = ?, thinking_characters = ?, "
            "retry_count = ?, status_code = ?, upstream_latency_ms = ?, "
            "bytes_received = ?, bytes_emitted = ?, "
            "upstream_connect_ms = ?, upstream_read_ms = ?, "
            "coordinator_overhead_ms = ?, "
            "provider_cost_microdollars = ?, provider_cost_source = ?, "
            "local_cost_microdollars = ?, local_cost_exactness = ?, "
            "upstream_protocol = ? "
            "WHERE id = ? AND status = 'pending'",
            (
                status,
                input_tokens,
                output_tokens,
                cost_microdollars,
                exactness,
                first_byte_ms,
                error_class,
                error_detail,
                upstream_request_id,
                cache_read_tokens,
                cache_write_tokens,
                reasoning_tokens,
                thinking_characters,
                retry_count,
                status_code,
                upstream_latency_ms,
                bytes_received,
                bytes_emitted,
                upstream_connect_ms,
                upstream_read_ms,
                coordinator_overhead_ms,
                provider_cost_microdollars,
                provider_cost_source,
                local_cost_microdollars,
                local_cost_exactness,
                upstream_protocol,
                request_id,
            ),
        )
        return rowcount > 0

    async def get_by_id(self, request_id: str) -> dict[str, Any] | None:
        """Fetch a request by id."""
        row = await self._db.fetch_one(
            "SELECT * FROM requests WHERE id = ?",
            (request_id,),
        )
        return dict(row) if row is not None else None

    async def list_pending_since(self, cutoff_iso: str) -> list[dict[str, Any]]:
        """List requests still pending since a cutoff timestamp."""
        rows = await self._db.fetch_all(
            "SELECT * FROM requests WHERE status = 'pending' AND started_at >= ?",
            (cutoff_iso,),
        )
        return [dict(r) for r in rows]


class ReservationRepository:
    """CRUD operations for reservations."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        request_id: str,
        account_id: int,
        model_id: str,
        estimated_tokens: int,
        estimated_microdollars: int,
        ttl_seconds: int = 900,
    ) -> str:
        """Create a new reservation with expiry, return the id."""
        last_id = await self._db.execute_insert(
            "INSERT INTO reservations "
            "(request_id, account_id, model_id, reserved_microdollars, "
            "estimated_tokens, expires_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', ?))",
            (
                request_id,
                account_id,
                model_id,
                estimated_microdollars,
                estimated_tokens,
                f"+{ttl_seconds} seconds",
            ),
        )
        return str(last_id)

    async def release(self, reservation_id: str, reason: str) -> bool:
        """Mark a reservation as released.

        Returns True if a reservation was actually released.
        """
        rowcount = await self._db.execute_write(
            "UPDATE reservations SET status = 'released', "
            "released_at = CURRENT_TIMESTAMP, release_reason = ? "
            "WHERE id = ? AND status = 'active'",
            (reason, reservation_id),
        )
        return rowcount > 0

    async def release_for_request(
        self,
        request_id: str,
        reason: str,
    ) -> None:
        """Release all active reservations for a request."""
        await self._db.execute_write(
            "UPDATE reservations SET status = 'released', "
            "released_at = CURRENT_TIMESTAMP, release_reason = ? "
            "WHERE request_id = ? AND status = 'active'",
            (reason, request_id),
        )

    async def reconcile_expired(self) -> int:
        """Release all reservations past their expiry, return count."""
        return await self._db.execute_write(
            "UPDATE reservations SET status = 'expired', "
            "released_at = CURRENT_TIMESTAMP, release_reason = 'expired' "
            "WHERE status = 'active' AND expires_at IS NOT NULL "
            "AND expires_at <= CURRENT_TIMESTAMP "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM requests"
            "  WHERE requests.id = reservations.request_id"
            "    AND requests.status = 'pending'"
            ")"
        )

    async def get_active_for_account(
        self,
        account_id: int,
    ) -> list[dict[str, Any]]:
        """Get all active reservations for an account."""
        rows = await self._db.fetch_all(
            "SELECT * FROM reservations WHERE account_id = ? AND status = 'active'",
            (account_id,),
        )
        return [dict(r) for r in rows]


class AttemptRepository:
    """CRUD operations for request_attempts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        request_id: str,
        attempt_number: int,
        account_id: int,
        provider_id: str | None = None,
        model_id: str | None = None,
        protocol: str | None = None,
        streamed: bool = False,
    ) -> int:
        """Create a new attempt row, return its id.

        On ``attempt_number == 1`` the parent request's
        ``first_attempt_at`` column is also stamped with the current
        timestamp. This anchors coordinator-overhead analytics without
        requiring the coordinator to issue a second UPDATE.
        """
        attempt_id = await self._db.execute_insert(
            "INSERT INTO request_attempts "
            "(request_id, attempt_number, account_id, "
            "provider_id, model_id, protocol, streamed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                attempt_number,
                account_id,
                provider_id,
                model_id,
                protocol,
                1 if streamed else 0,
            ),
        )
        if attempt_number == 1:
            await self._db.execute_write(
                "UPDATE requests SET first_attempt_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND first_attempt_at IS NULL",
                (request_id,),
            )
        return attempt_id

    async def update(
        self,
        attempt_id: int,
        status_code: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        upstream_request_id: str | None = None,
        bytes_emitted: int = 0,
        bytes_received: int = 0,
        latency_ms: int = 0,
        retry_category: str | None = None,
        release_reason: str | None = None,
        is_retry_outcome: bool = False,
        completed: bool = True,
    ) -> None:
        """Update an attempt with outcome fields.

        Also records the attempt as the parent request's
        ``last_attempt_id`` so the trace endpoint can resolve the
        winning attempt without re-scanning the attempts table.
        """
        retry_flag = 1 if is_retry_outcome else 0
        if completed:
            await self._db.execute_write(
                "UPDATE request_attempts SET "
                "status_code = ?, error_class = ?, error_detail = ?, "
                "upstream_request_id = ?, bytes_emitted = ?, "
                "bytes_received = ?, latency_ms = ?, "
                "retry_category = ?, release_reason = ?, "
                "is_retry_outcome = ?, "
                "completed_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (
                    status_code,
                    error_class,
                    error_detail,
                    upstream_request_id,
                    bytes_emitted,
                    bytes_received,
                    latency_ms,
                    retry_category,
                    release_reason,
                    retry_flag,
                    attempt_id,
                ),
            )
        else:
            await self._db.execute_write(
                "UPDATE request_attempts SET "
                "status_code = ?, error_class = ?, error_detail = ?, "
                "upstream_request_id = ?, bytes_emitted = ?, "
                "bytes_received = ?, latency_ms = ?, "
                "retry_category = ?, release_reason = ?, "
                "is_retry_outcome = ?, "
                "completed_at = NULL "
                "WHERE id = ?",
                (
                    status_code,
                    error_class,
                    error_detail,
                    upstream_request_id,
                    bytes_emitted,
                    bytes_received,
                    latency_ms,
                    retry_category,
                    release_reason,
                    retry_flag,
                    attempt_id,
                ),
            )

    async def finalize_if_incomplete(
        self,
        attempt_id: int,
        status_code: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        upstream_request_id: str | None = None,
        bytes_emitted: int = 0,
        bytes_received: int = 0,
        latency_ms: int = 0,
        retry_category: str | None = None,
        release_reason: str | None = None,
        is_retry_outcome: bool = False,
    ) -> bool:
        """Finalize an attempt only if it is still incomplete.

        Returns True if the row was updated (transition performed).
        When the transition occurs the attempt is also stamped as
        the parent request's ``last_attempt_id`` so the trace
        endpoint can resolve the winning attempt without scanning.
        """
        retry_flag = 1 if is_retry_outcome else 0
        async with self._db.transaction():
            rowcount = await self._db.execute_write(
                "UPDATE request_attempts SET "
                "status_code = ?, error_class = ?, error_detail = ?, "
                "upstream_request_id = ?, bytes_emitted = ?, "
                "bytes_received = ?, latency_ms = ?, "
                "retry_category = ?, release_reason = ?, "
                "is_retry_outcome = ?, "
                "completed_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND completed_at IS NULL",
                (
                    status_code,
                    error_class,
                    error_detail,
                    upstream_request_id,
                    bytes_emitted,
                    bytes_received,
                    latency_ms,
                    retry_category,
                    release_reason,
                    retry_flag,
                    attempt_id,
                ),
            )
            if rowcount > 0:
                await self._db.execute_write(
                    "UPDATE requests SET last_attempt_id = ? "
                    "WHERE id = (SELECT request_id FROM request_attempts "
                    "WHERE id = ?)",
                    (attempt_id, attempt_id),
                )
        return rowcount > 0

    async def get_for_request(self, request_id: str) -> list[dict[str, Any]]:
        """Get all attempts for a request, ordered by attempt number."""
        rows = await self._db.fetch_all(
            "SELECT * FROM request_attempts "
            "WHERE request_id = ? ORDER BY attempt_number",
            (request_id,),
        )
        return [dict(r) for r in rows]


class OperationalEventRepository:
    """CRUD operations for operational_events.

    Records rows emitted by safety-net and periodic cleanup tasks
    (crash recovery, stale-request finalizer, reservation reconcile,
    streaming cancel-timeout fallback).  Each row carries an
    ``event_type`` and a JSON ``details`` blob so the dashboard can
    chart "how often is the safety net firing?" without instrumenting
    every background task separately.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        event_type: str,
        details: dict[str, Any] | None = None,
    ) -> int:
        """Insert one operational event, return its id."""
        import json

        details_json = json.dumps(details or {})
        return await self._db.execute_insert(
            "INSERT INTO operational_events (event_type, details_json) VALUES (?, ?)",
            (event_type, details_json),
        )


class RoutingDecisionRepository:
    """CRUD operations for routing_decisions.

    Each row captures one routing decision: which account was
    chosen, how many candidates were considered, what scoring
    tier the chosen account sat in, and which accounts were
    excluded (with reason).  Persisted inside the same transaction
    as the request_attempts INSERT so the trace and the attempt
    can never disagree.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        request_id: int,
        attempt_number: int,
        model_id: str,
        *,
        provider_id: str | None,
        protocol: str | None,
        selected_account_id: int | None,
        selected_account_name: str | None,
        selected_tier: int | None,
        selected_score: float | None,
        eligible_count: int,
        scored_count: int,
        attempted_excluded_count: int,
        top_score: float | None,
        top_score_account_name: str | None,
        exclude_reasons_json: str,
    ) -> int:
        """Persist a routing decision row, return its id."""
        return await self._db.execute_insert(
            "INSERT INTO routing_decisions ("
            "request_id, attempt_number, model_id, provider_id, protocol, "
            "selected_account_id, selected_account_name, "
            "selected_tier, selected_score, "
            "eligible_count, scored_count, attempted_excluded_count, "
            "top_score, top_score_account_name, "
            "exclude_reasons_json"
            ") VALUES ("
            "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
            ")",
            (
                request_id,
                attempt_number,
                model_id,
                provider_id,
                protocol,
                selected_account_id,
                selected_account_name,
                selected_tier,
                selected_score,
                eligible_count,
                scored_count,
                attempted_excluded_count,
                top_score,
                top_score_account_name,
                exclude_reasons_json,
            ),
        )

    async def get_for_request(self, request_id: int) -> list[dict[str, Any]]:
        """Get all routing decisions for one request, ordered by attempt."""
        rows = await self._db.fetch_all(
            "SELECT * FROM routing_decisions "
            "WHERE request_id = ? ORDER BY attempt_number",
            (request_id,),
        )
        return [dict(row) for row in rows]


class UsageWindowRepository:
    """Aggregate cost microdollars across usage windows."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_usage_windows(
        self,
        account_id: int,
        now_iso: str,
    ) -> dict[str, int]:
        """Return aggregated costs for 5h, 7d, 30d windows in a single query."""
        row = await self._db.fetch_one(
            "SELECT "
            "COALESCE(SUM(CASE WHEN started_at >= datetime(?, '-5 hours') "
            "THEN cost_microdollars ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN started_at >= datetime(?, '-7 days') "
            "THEN cost_microdollars ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN started_at >= datetime(?, '-30 days') "
            "THEN cost_microdollars ELSE 0 END), 0) "
            "FROM requests "
            "WHERE account_id = ? AND status != 'pending' "
            "AND started_at >= datetime(?, '-30 days')",
            (now_iso, now_iso, now_iso, account_id, now_iso),
        )
        if row is None:
            return {"5h": 0, "7d": 0, "30d": 0}
        return {
            "5h": int(row[0]),
            "7d": int(row[1]),
            "30d": int(row[2]),
        }

    async def get_all_usage_windows(self, now_iso: str) -> dict[int, dict[str, int]]:
        """Return 5h/7d/30d costs for every active account in one scan."""
        rows = await self._db.fetch_all(
            "SELECT account_id, "
            "COALESCE(SUM(CASE WHEN started_at >= datetime(?, '-5 hours') "
            "THEN cost_microdollars ELSE 0 END), 0) AS cost_5h, "
            "COALESCE(SUM(CASE WHEN started_at >= datetime(?, '-7 days') "
            "THEN cost_microdollars ELSE 0 END), 0) AS cost_7d, "
            "COALESCE(SUM(cost_microdollars), 0) AS cost_30d "
            "FROM requests "
            "WHERE status != 'pending' "
            "AND started_at >= datetime(?, '-30 days') "
            "GROUP BY account_id",
            (now_iso, now_iso, now_iso),
        )
        return {
            int(row["account_id"]): {
                "5h": int(row["cost_5h"]),
                "7d": int(row["cost_7d"]),
                "30d": int(row["cost_30d"]),
            }
            for row in rows
        }


class PriceSnapshotRepository:
    """CRUD operations for model_price_snapshots."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_latest(
        self, model_id: str, provider_id: str | None = None
    ) -> dict[str, Any] | None:
        """Get the latest snapshot, optionally scoped to one provider."""
        provider_clause = "" if provider_id is None else " AND provider_id = ?"
        params = (model_id,) if provider_id is None else (model_id, provider_id)
        row = await self._db.fetch_one(
            "SELECT model_id, input_price_per_1k, output_price_per_1k, "
            "captured_at, input_per_million_microdollars, "
            "output_per_million_microdollars, "
            "cache_read_per_million_microdollars, "
            "cache_write_per_million_microdollars, source, provider_id, "
            "source_detail, source_confidence, catalog_source "
            "FROM model_price_snapshots WHERE model_id = ?"
            f"{provider_clause} ORDER BY captured_at DESC, id DESC LIMIT 1",
            params,
        )
        return dict(row) if row is not None else None

    async def get_all_latest(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Return the latest price snapshot for every model/provider pair."""
        rows = await self._db.fetch_all(
            "SELECT model_id, input_price_per_1k, output_price_per_1k, "
            "captured_at, input_per_million_microdollars, "
            "output_per_million_microdollars, "
            "cache_read_per_million_microdollars, "
            "cache_write_per_million_microdollars, source, provider_id, "
            "source_detail, source_confidence, catalog_source "
            "FROM ("
            "  SELECT model_price_snapshots.*, "
            "  ROW_NUMBER() OVER ("
            "    PARTITION BY model_id, provider_id "
            "    ORDER BY captured_at DESC, id DESC"
            "  ) AS snapshot_rank "
            "  FROM model_price_snapshots"
            ") WHERE snapshot_rank = 1"
        )
        return {
            (str(row["model_id"]), str(row["provider_id"])): dict(row) for row in rows
        }

    async def record(
        self,
        model_id: str,
        input_price_per_1k: float | None,
        output_price_per_1k: float | None,
        *,
        input_per_million_microdollars: int | None = None,
        output_per_million_microdollars: int | None = None,
        cache_read_per_million_microdollars: int | None = None,
        cache_write_per_million_microdollars: int | None = None,
        source: str = "upstream",
        provider_id: str = DEFAULT_PROVIDER_ID,
        source_detail: str | None = None,
        source_confidence: str | None = None,
        catalog_source: str | None = None,
    ) -> None:
        """Record a new price snapshot.

        Auto-converts legacy float prices to integer microdollars when
        integer fields are not provided.
        """
        # Auto-convert legacy floats to integer microdollars
        if input_per_million_microdollars is None and input_price_per_1k is not None:
            input_per_million_microdollars = int(
                round(input_price_per_1k * 1_000_000_000)
            )
        if output_per_million_microdollars is None and output_price_per_1k is not None:
            output_per_million_microdollars = int(
                round(output_price_per_1k * 1_000_000_000)
            )

        await self._db.execute_write(
            "INSERT INTO model_price_snapshots "
            "(model_id, input_price_per_1k, output_price_per_1k, "
            "input_per_million_microdollars, output_per_million_microdollars, "
            "cache_read_per_million_microdollars, "
            "cache_write_per_million_microdollars, source, provider_id, "
            "source_detail, source_confidence, catalog_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                model_id,
                input_price_per_1k,
                output_price_per_1k,
                input_per_million_microdollars,
                output_per_million_microdollars,
                cache_read_per_million_microdollars,
                cache_write_per_million_microdollars,
                source,
                provider_id,
                source_detail,
                source_confidence,
                catalog_source,
            ),
        )

    async def record_from_dict(
        self,
        model_id: str,
        prices_dict: dict[str, float | int | str | None],
        *,
        provider_id: str = DEFAULT_PROVIDER_ID,
    ) -> None:
        """Record prices from a dictionary with input/output keys."""
        input_micro = prices_dict.get("input_per_million_microdollars")
        output_micro = prices_dict.get("output_per_million_microdollars")
        cache_read = prices_dict.get("cache_read_per_million_microdollars")
        cache_write = prices_dict.get("cache_write_per_million_microdollars")
        input_price_per_1k = prices_dict.get("input_price_per_1k")
        output_price_per_1k = prices_dict.get("output_price_per_1k")
        source_value = prices_dict.get("source", "upstream")
        await self.record(
            model_id,
            input_price_per_1k=parse_price_per_1k(input_price_per_1k),
            output_price_per_1k=parse_price_per_1k(output_price_per_1k),
            input_per_million_microdollars=parse_microdollars_per_million(input_micro),
            output_per_million_microdollars=parse_microdollars_per_million(
                output_micro
            ),
            cache_read_per_million_microdollars=parse_microdollars_per_million(
                cache_read
            ),
            cache_write_per_million_microdollars=parse_microdollars_per_million(
                cache_write
            ),
            source=source_value if isinstance(source_value, str) else "upstream",
            provider_id=provider_id,
        )


class AccountEventRepository:
    """CRUD operations for account_events."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(
        self,
        account_id: int,
        event_type: str,
        details: str = "{}",
    ) -> None:
        """Record an account event."""
        await self._db.execute_write(
            "INSERT INTO account_events (account_id, event_type, details) "
            "VALUES (?, ?, ?)",
            (account_id, event_type, details),
        )


class ProviderRepository:
    """CRUD operations for providers."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list_enabled(self) -> list[dict[str, Any]]:
        """List all enabled providers."""
        rows = await self._db.fetch_all("SELECT * FROM providers WHERE enabled = 1")
        return [dict(r) for r in rows]

    async def get_by_provider_id(self, provider_id: str) -> dict[str, Any] | None:
        """Fetch a provider by provider_id."""
        row = await self._db.fetch_one(
            "SELECT * FROM providers WHERE provider_id = ?",
            (provider_id,),
        )
        return dict(row) if row is not None else None

    async def sync_from_config(
        self,
        configured_providers: dict[str, dict[str, Any]],
    ) -> None:
        """Upsert configured providers and disable removed ones."""
        async with self._db.transaction():
            await self._sync_from_config_locked(configured_providers)

    async def _sync_from_config_locked(
        self,
        configured_providers: dict[str, dict[str, Any]],
    ) -> None:
        """Upsert configured providers inside the caller's transaction."""
        import json as _json

        configured_ids = set(configured_providers.keys())
        for provider_id, cfg in configured_providers.items():
            await self._db.execute_write(
                "INSERT INTO providers (provider_id, base_url, protocols) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(provider_id) DO UPDATE SET "
                "base_url = excluded.base_url, "
                "protocols = excluded.protocols, "
                "enabled = 1",
                (provider_id, cfg["base_url"], _json.dumps(cfg["protocols"])),
            )

        # Disable providers no longer in config
        existing = await self._db.fetch_all(
            "SELECT provider_id FROM providers WHERE enabled = 1"
        )
        for row in existing:
            if row["provider_id"] not in configured_ids:
                await self._db.execute_write(
                    "UPDATE providers SET enabled = 0 WHERE provider_id = ?",
                    (row["provider_id"],),
                )


class PingRepository:
    """Repository for provider ping probe results."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_ping(
        self,
        provider_id: str,
        account_name: str,
        latency_ms: int | None,
        status_code: int | None,
        error: str | None,
        model_count: int = 0,
    ) -> None:
        """Record a single ping result from a catalog refresh."""
        await self._db.execute_write(
            """
            INSERT INTO provider_pings
                (provider_id, account_name, latency_ms, status_code, error, model_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (provider_id, account_name, latency_ms, status_code, error, model_count),
        )

    async def get_provider_ping_summary(
        self,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        """Per-provider aggregate: avg/min/max latency, success rate, last ping.

        Uses a single window function to identify the most recent ping per
        provider instead of two correlated subqueries, so SQLite scans
        ``provider_pings`` once and aggregates in a single pass.
        """
        sql = """
        WITH ranked AS (
            SELECT
                pp.provider_id,
                pp.latency_ms,
                pp.error,
                pp.model_count,
                pp.probed_at,
                ROW_NUMBER() OVER (
                    PARTITION BY pp.provider_id ORDER BY pp.probed_at DESC
                ) as rn
            FROM provider_pings pp
            WHERE pp.probed_at >= ? AND pp.probed_at < ?
        )
        SELECT
            provider_id,
            COUNT(*) as ping_count,
            COALESCE(AVG(latency_ms), 0) as avg_latency_ms,
            COALESCE(MIN(latency_ms), 0) as min_latency_ms,
            COALESCE(MAX(latency_ms), 0) as max_latency_ms,
            SUM(CASE WHEN error IS NULL THEN 1 ELSE 0 END) as success_count,
            SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as failure_count,
            ROUND(
                100.0 * SUM(CASE WHEN error IS NULL THEN 1 ELSE 0 END) / COUNT(*),
                1
            ) as success_rate,
            MAX(probed_at) as last_ping_at,
            MAX(CASE WHEN rn = 1 THEN latency_ms END) as last_latency_ms,
            MAX(CASE WHEN rn = 1 THEN model_count END) as last_model_count
        FROM ranked
        GROUP BY provider_id
        ORDER BY provider_id
        """
        rows = await self._db.fetch_all(sql, (start, end))
        return [dict(row) for row in rows]

    async def get_ping_timeseries(
        self,
        provider_id: str,
        start: str,
        end: str,
        bucket: str = "hour",
    ) -> list[dict[str, Any]]:
        """Per-bucket ping latency trend for one provider."""
        if bucket not in ("hour", "day"):
            bucket = "hour"
        fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
        sql = """
        SELECT
            strftime(?, pp.probed_at) as bucket,
            COUNT(*) as ping_count,
            COALESCE(AVG(pp.latency_ms), 0) as avg_latency_ms,
            COALESCE(MIN(pp.latency_ms), 0) as min_latency_ms,
            COALESCE(MAX(pp.latency_ms), 0) as max_latency_ms,
            SUM(CASE WHEN pp.error IS NULL THEN 1 ELSE 0 END) as success_count,
            SUM(CASE WHEN pp.error IS NOT NULL THEN 1 ELSE 0 END) as failure_count
        FROM provider_pings pp
        WHERE pp.provider_id = ?
          AND pp.probed_at >= ? AND pp.probed_at < ?
        GROUP BY bucket
        ORDER BY bucket
        """
        rows = await self._db.fetch_all(sql, (fmt, provider_id, start, end))
        return [dict(row) for row in rows]

    async def get_ping_recent(
        self,
        provider_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Most recent pings, optionally filtered by provider."""
        params: list[Any] = []
        provider_filter = ""
        if provider_id:
            provider_filter = " WHERE pp.provider_id = ?"
            params.append(provider_id)
        params.append(limit)
        sql = f"""
        SELECT
            pp.provider_id,
            pp.account_name,
            pp.probed_at,
            pp.latency_ms,
            pp.status_code,
            pp.error,
            pp.model_count
        FROM provider_pings pp
        {provider_filter}
        ORDER BY pp.probed_at DESC
        LIMIT ?
        """
        rows = await self._db.fetch_all(sql, tuple(params))
        return [dict(row) for row in rows]

    async def cleanup_old_pings(self, retain_days: int = 7) -> int:
        """Delete pings older than the retention period."""
        async with self._db.transaction():
            count = await self._db.execute_write(
                """
                DELETE FROM provider_pings
                WHERE probed_at < datetime('now', ? || ' days')
                """,
                (f"-{retain_days}",),
            )
        if count > 0:
            logger.info(
                "Deleted %d old provider pings (retention=%d days)",
                count,
                retain_days,
            )
        return count


class CatalogReconciliationRepository:
    """Operations for aligning the durable catalog with the live cache.

    When a provider drops a model, the live cache is pruned but the
    durable ``models`` row may still be referenced by historical
    ``requests`` and ``reservations``.  This repository relinks the
    FK pointers to :data:`DEPRECATED_MODEL_ID` and preserves the
    original id in ``original_model_id`` so stats queries can still
    filter by the real model name.  Once the relink is complete the
    original row can be deleted without losing usage history.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def relink_model(self, model_id: str) -> dict[str, int]:
        """Relink all FK references from ``model_id`` to the placeholder.

        Idempotent. Returns the count of relinked rows so callers can
        emit a diagnostic log line. Must run inside the caller's
        transaction; the surrounding code holds the connection lock
        so the relink is atomic with the subsequent DELETE.
        """
        requests_relinked = await self._db.execute_write(
            """
            UPDATE requests
            SET original_model_id = COALESCE(original_model_id, model_id),
                model_id = ?
            WHERE model_id = ?
              AND model_id <> ?
            """,
            (DEPRECATED_MODEL_ID, model_id, DEPRECATED_MODEL_ID),
        )
        reservations_relinked = await self._db.execute_write(
            """
            UPDATE reservations
            SET original_model_id = COALESCE(original_model_id, model_id),
                model_id = ?
            WHERE model_id = ?
              AND model_id <> ?
            """,
            (DEPRECATED_MODEL_ID, model_id, DEPRECATED_MODEL_ID),
        )
        return {
            "requests": int(requests_relinked),
            "reservations": int(reservations_relinked),
        }

    async def ensure_placeholder(self) -> None:
        """Insert the placeholder ``models`` row if missing.

        Migrations also do this, but a fresh server can race a
        reconciliation pass before migrations are applied if the
        cache was hydrated from a previous run. Idempotent.
        """
        await self._db.execute_write(
            """
            INSERT OR IGNORE INTO models (
                model_id, display_name, protocol,
                resolution_status, provider_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                DEPRECATED_MODEL_ID,
                "Deprecated models",
                "openai",
                "resolved",
                DEFAULT_PROVIDER_ID,
            ),
        )


def _epoch_to_iso(value: float) -> str:
    """Convert a POSIX timestamp to the SQLite ``YYYY-MM-DD HH:MM:SS`` format."""
    return _dt.datetime.fromtimestamp(value, tz=_dt.UTC).strftime("%Y-%m-%d %H:%M:%S")


def _iso_to_epoch(value: str | None) -> float | None:
    """Convert an ISO timestamp from SQLite back to a POSIX epoch (UTC)."""
    if value is None:
        return None
    try:
        parsed = _dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_dt.UTC
        )
    except ValueError:
        return None
    return parsed.timestamp()


class AccountBackoffRepository:
    """Persistence for upstream-observed backoffs.

    Only stores authoritative upstream signals (429/402/5xx,
    transport failures, auth failures, model unavailability). Local
    estimated quota overage MUST NOT flow through this repository;
    those values remain advisory in the in-memory estimator and the
    routing scorer.

    Writes are always wrapped in ``async with db.transaction():``;
    readers use ``fetch_one``/``fetch_all`` which acquire the
    connection lock independently.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def upsert_failure(
        self,
        *,
        account_id: int,
        model_id: str | None,
        reason: str,
        status_code: int | None,
        error_class: str | None,
        backoff_until: float | None,
        consecutive_failures: int,
    ) -> None:
        """Record or refresh an active backoff for an (account, model, reason).

        ``backoff_until`` is a POSIX epoch in seconds; ``None`` records
        a terminal disable that does not auto-expire. ``model_id``
        ``None`` means account-wide suppression.

        Implementation note: SQLite UNIQUE constraints treat two NULLs
        as distinct, so the table-level UNIQUE on
        ``(account_id, model_id, reason)`` does not deduplicate rows
        with ``model_id IS NULL``. This method performs an explicit
        existence check and chooses INSERT vs UPDATE accordingly. Both
        paths share the same transaction boundary so the row remains
        consistent with concurrent reads.
        """
        backoff_iso = (
            _epoch_to_iso(backoff_until) if backoff_until is not None else None
        )
        async with self._db.transaction():
            if model_id is None:
                existing = await self._db.fetch_one(
                    "SELECT id FROM account_backoffs "
                    "WHERE account_id = ? AND model_id IS NULL AND reason = ?",
                    (account_id, reason),
                )
            else:
                existing = await self._db.fetch_one(
                    "SELECT id FROM account_backoffs "
                    "WHERE account_id = ? AND model_id = ? AND reason = ?",
                    (account_id, model_id, reason),
                )
            if existing is None:
                await self._db.execute_insert(
                    """
                    INSERT INTO account_backoffs (
                        account_id, model_id, reason, status_code, error_class,
                        consecutive_failures, backoff_until,
                        last_failure_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?,
                              CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (
                        account_id,
                        model_id,
                        reason,
                        status_code,
                        error_class,
                        consecutive_failures,
                        backoff_iso,
                    ),
                )
            else:
                await self._db.execute_write(
                    """
                    UPDATE account_backoffs SET
                        status_code = ?,
                        error_class = ?,
                        consecutive_failures = ?,
                        backoff_until = ?,
                        last_failure_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        status_code,
                        error_class,
                        consecutive_failures,
                        backoff_iso,
                        int(existing["id"]),
                    ),
                )

    async def clear_success(
        self,
        *,
        account_id: int,
        model_id: str | None = None,
        reasons: list[str] | None = None,
    ) -> int:
        """Remove backoff rows after a successful request.

        When ``model_id`` is given, only the matching pair is cleared.
        When ``model_id`` is ``None``, all rows for the account whose
        ``model_id`` is either ``NULL`` (account-wide) or equal to
        ``model_id`` are removed.

        ``reasons`` optionally filters the deletion to a subset of
        reasons (e.g. ``["rate_limited"]``). Returns the rowcount.
        """
        clauses = ["account_id = ?"]
        params: list[Any] = [account_id]
        if model_id is None:
            clauses.append("(model_id IS NULL)")
        else:
            clauses.append("(model_id IS NULL OR model_id = ?)")
            params.append(model_id)
        if reasons:
            placeholders = ",".join("?" for _ in reasons)
            clauses.append(f"reason IN ({placeholders})")
            params.extend(reasons)
        where_sql = " AND ".join(clauses)
        async with self._db.transaction():
            return int(
                await self._db.execute_write(
                    f"DELETE FROM account_backoffs WHERE {where_sql}",
                    tuple(params),
                )
            )

    async def list_active(
        self,
        *,
        now: float | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Return currently-active backoff rows.

        Active means: ``backoff_until`` is NULL (terminal/indefinite)
        or ``backoff_until`` is strictly greater than the cutoff.
        ``now`` defaults to ``time.time()``. Returns a list of plain
        dicts with epoch floats for ``backoff_until`` so callers do
        not need to parse SQLite timestamps.
        """
        if now is None:
            now = time.time()
        now_iso = _epoch_to_iso(now)
        rows = await self._db.fetch_all(
            """
            SELECT id, account_id, account_id AS acct_id, model_id, reason,
                   status_code, error_class, consecutive_failures,
                   backoff_until, last_failure_at, updated_at
            FROM account_backoffs
            WHERE backoff_until IS NULL OR backoff_until > ?
            ORDER BY account_id, model_id, reason
            LIMIT ?
            """,
            (now_iso, limit),
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            entry["backoff_until_epoch"] = _iso_to_epoch(entry.get("backoff_until"))
            entry.pop("acct_id", None)
            results.append(entry)
        return results

    async def expire_old(self, *, now: float | None = None) -> int:
        """Delete expired backoff rows; returns count removed.

        Expired means: ``backoff_until`` is non-NULL and not strictly
        greater than the cutoff. Terminal rows (``backoff_until IS
        NULL``) are preserved.
        """
        if now is None:
            now = time.time()
        now_iso = _epoch_to_iso(now)
        async with self._db.transaction():
            return int(
                await self._db.execute_write(
                    """
                    DELETE FROM account_backoffs
                    WHERE backoff_until IS NOT NULL
                      AND backoff_until <= ?
                    """,
                    (now_iso,),
                )
            )

    async def clear_account(self, *, account_id: int) -> int:
        """Delete every backoff row for an account; returns count removed."""
        async with self._db.transaction():
            return int(
                await self._db.execute_write(
                    "DELETE FROM account_backoffs WHERE account_id = ?",
                    (account_id,),
                )
            )

    async def get_for_account_model(
        self,
        *,
        account_id: int,
        model_id: str | None,
    ) -> list[dict[str, Any]]:
        """Return all backoff rows for an (account, model) pair."""
        if model_id is None:
            rows = await self._db.fetch_all(
                """
                SELECT id, account_id, model_id, reason, status_code,
                       error_class, consecutive_failures, backoff_until,
                       last_failure_at, updated_at
                FROM account_backoffs
                WHERE account_id = ? AND model_id IS NULL
                ORDER BY reason
                """,
                (account_id,),
            )
        else:
            rows = await self._db.fetch_all(
                """
                SELECT id, account_id, model_id, reason, status_code,
                       error_class, consecutive_failures, backoff_until,
                       last_failure_at, updated_at
                FROM account_backoffs
                WHERE account_id = ? AND (model_id IS NULL OR model_id = ?)
                ORDER BY reason
                """,
                (account_id, model_id),
            )
        results: list[dict[str, Any]] = []
        for row in rows:
            entry = dict(row)
            entry["backoff_until_epoch"] = _iso_to_epoch(entry.get("backoff_until"))
            results.append(entry)
        return results
