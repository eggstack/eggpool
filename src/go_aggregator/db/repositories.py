"""Repository layer for database operations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from go_aggregator.catalog.pricing import (
    parse_microdollars_per_million,
    parse_price_per_1k,
)

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database

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
            provider_id = str(acct.get("provider_id", "opencode-go"))
            configured_names.add(name)
            row = await self._db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?",
                (name,),
            )
            if row is not None:
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
        provider_id: str = "opencode-go",
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
                "provider_id) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
                (
                    account_id,
                    model_id,
                    started_at_str,
                    protocol,
                    int(streamed),
                    reserved_microdollars,
                    request_id,
                    provider_id,
                ),
            )
        else:
            last_id = await self._db.execute_insert(
                "INSERT INTO requests "
                "(account_id, model_id, status, protocol, streamed, "
                "reserved_microdollars, proxy_request_id, provider_id) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)",
                (
                    account_id,
                    model_id,
                    protocol,
                    int(streamed),
                    reserved_microdollars,
                    request_id,
                    provider_id,
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
            "retry_count = ?, status_code = ? "
            "WHERE id = ?",
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
            "retry_count = ?, status_code = ? "
            "WHERE id = ?",
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
            "bytes_received = ?, bytes_emitted = ? "
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
            "AND expires_at < CURRENT_TIMESTAMP "
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
    ) -> int:
        """Create a new attempt row, return its id."""
        return await self._db.execute_insert(
            "INSERT INTO request_attempts "
            "(request_id, attempt_number, account_id) "
            "VALUES (?, ?, ?)",
            (request_id, attempt_number, account_id),
        )

    async def update(
        self,
        attempt_id: int,
        status_code: int | None = None,
        error_class: str | None = None,
        error_detail: str | None = None,
        upstream_request_id: str | None = None,
        bytes_emitted: int = 0,
        completed: bool = True,
    ) -> None:
        """Update an attempt with outcome fields."""
        if completed:
            await self._db.execute_write(
                "UPDATE request_attempts SET "
                "status_code = ?, error_class = ?, error_detail = ?, "
                "upstream_request_id = ?, bytes_emitted = ?, "
                "completed_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (
                    status_code,
                    error_class,
                    error_detail,
                    upstream_request_id,
                    bytes_emitted,
                    attempt_id,
                ),
            )
        else:
            await self._db.execute_write(
                "UPDATE request_attempts SET "
                "status_code = ?, error_class = ?, error_detail = ?, "
                "upstream_request_id = ?, bytes_emitted = ?, "
                "completed_at = NULL "
                "WHERE id = ?",
                (
                    status_code,
                    error_class,
                    error_detail,
                    upstream_request_id,
                    bytes_emitted,
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
    ) -> bool:
        """Finalize an attempt only if it is still incomplete.

        Returns True if the row was updated (transition performed).
        """
        rowcount = await self._db.execute_write(
            "UPDATE request_attempts SET "
            "status_code = ?, error_class = ?, error_detail = ?, "
            "upstream_request_id = ?, bytes_emitted = ?, "
            "completed_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND completed_at IS NULL",
            (
                status_code,
                error_class,
                error_detail,
                upstream_request_id,
                bytes_emitted,
                attempt_id,
            ),
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
            "WHERE account_id = ? AND status != 'pending'",
            (now_iso, now_iso, now_iso, account_id),
        )
        if row is None:
            return {"5h": 0, "7d": 0, "30d": 0}
        return {
            "5h": int(row[0]),
            "7d": int(row[1]),
            "30d": int(row[2]),
        }


class PriceSnapshotRepository:
    """CRUD operations for model_price_snapshots."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_latest(self, model_id: str) -> dict[str, Any] | None:
        """Get the most recent price snapshot for a model."""
        row = await self._db.fetch_one(
            "SELECT model_id, input_price_per_1k, output_price_per_1k, "
            "captured_at, input_per_million_microdollars, "
            "output_per_million_microdollars, "
            "cache_read_per_million_microdollars, "
            "cache_write_per_million_microdollars, source, provider_id "
            "FROM model_price_snapshots "
            "WHERE model_id = ? ORDER BY captured_at DESC, id DESC LIMIT 1",
            (model_id,),
        )
        return dict(row) if row is not None else None

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
        provider_id: str = "opencode-go",
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
            "cache_write_per_million_microdollars, source, provider_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )

    async def record_from_dict(
        self,
        model_id: str,
        prices_dict: dict[str, float | int | str | None],
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

    async def upsert(
        self,
        provider_id: str,
        base_url: str,
        protocols: list[str],
    ) -> None:
        """Insert or update a provider record."""
        import json

        await self._db.execute_write(
            "INSERT INTO providers (provider_id, base_url, protocols) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(provider_id) DO UPDATE SET "
            "base_url = excluded.base_url, protocols = excluded.protocols",
            (provider_id, base_url, json.dumps(protocols)),
        )

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
        """Per-provider aggregate: avg/min/max latency, success rate, last ping."""
        sql = """
        SELECT
            pp.provider_id,
            COUNT(*) as ping_count,
            COALESCE(AVG(pp.latency_ms), 0) as avg_latency_ms,
            COALESCE(MIN(pp.latency_ms), 0) as min_latency_ms,
            COALESCE(MAX(pp.latency_ms), 0) as max_latency_ms,
            SUM(CASE WHEN pp.error IS NULL THEN 1 ELSE 0 END) as success_count,
            SUM(CASE WHEN pp.error IS NOT NULL THEN 1 ELSE 0 END) as failure_count,
            ROUND(
                100.0 * SUM(CASE WHEN pp.error IS NULL THEN 1 ELSE 0 END) / COUNT(*),
                1
            ) as success_rate,
            MAX(pp.probed_at) as last_ping_at,
            (SELECT pp2.latency_ms FROM provider_pings pp2
             WHERE pp2.provider_id = pp.provider_id
             ORDER BY pp2.probed_at DESC LIMIT 1) as last_latency_ms,
            (SELECT pp3.model_count FROM provider_pings pp3
             WHERE pp3.provider_id = pp.provider_id
             ORDER BY pp3.probed_at DESC LIMIT 1) as last_model_count
        FROM provider_pings pp
        WHERE pp.probed_at >= ? AND pp.probed_at < ?
        GROUP BY pp.provider_id
        ORDER BY pp.provider_id
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
