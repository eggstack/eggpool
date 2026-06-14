"""Repository layer for database operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database


class AccountRepository:
    """CRUD operations for accounts."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def sync_from_config(
        self,
        config_accounts: list[dict[str, Any]],
        db: Database,
    ) -> dict[str, int]:
        """Upsert configured accounts, disable removed ones, return name->id map."""
        name_to_id: dict[str, int] = {}
        configured_names: set[str] = set()

        for acct in config_accounts:
            name = str(acct["name"])
            configured_names.add(name)
            row = await db.fetch_one(
                "SELECT id FROM accounts WHERE name = ?",
                (name,),
            )
            if row is not None:
                await db.execute(
                    "UPDATE accounts SET api_key_env = ?, enabled = ?, "
                    "weight = ? WHERE name = ?",
                    (
                        str(acct["api_key_env"]),
                        int(acct.get("enabled", True)),
                        float(acct.get("weight", 1.0)),
                        name,
                    ),
                )
                name_to_id[name] = int(row["id"])
            else:
                cursor = await db.execute(
                    "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        name,
                        str(acct["api_key_env"]),
                        int(acct.get("enabled", True)),
                        float(acct.get("weight", 1.0)),
                    ),
                )
                last_id = cursor.lastrowid
                if last_id is None:
                    msg = "INSERT into accounts returned no lastrowid"
                    raise RuntimeError(msg)
                name_to_id[name] = last_id

        existing = await db.fetch_all("SELECT id, name FROM accounts WHERE enabled = 1")
        for row in existing:
            if row["name"] not in configured_names:
                await db.execute(
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
        account_id: int | None = None,
        reserved_microdollars: int = 0,
        started_at: float | None = None,
    ) -> str:
        """Insert a new pending request, return the id as string."""
        if started_at is not None:
            import datetime as _dt

            started_at_str = _dt.datetime.fromtimestamp(
                started_at, tz=_dt.UTC
            ).strftime("%Y-%m-%d %H:%M:%S")
            cursor = await self._db.execute(
                "INSERT INTO requests "
                "(account_id, model_id, started_at, status, protocol, "
                "streamed, reserved_microdollars) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
                (
                    account_id,
                    model_id,
                    started_at_str,
                    protocol,
                    int(streamed),
                    reserved_microdollars,
                ),
            )
        else:
            cursor = await self._db.execute(
                "INSERT INTO requests "
                "(account_id, model_id, status, protocol, streamed, "
                "reserved_microdollars) VALUES (?, ?, 'pending', ?, ?, ?)",
                (
                    account_id,
                    model_id,
                    protocol,
                    int(streamed),
                    reserved_microdollars,
                ),
            )
        last_id = cursor.lastrowid
        return str(last_id) if last_id is not None else request_id

    async def update_after_selection(
        self,
        request_id: str,
        account_id: int,
        reserved_microdollars: int,
    ) -> None:
        """Set the selected account after routing decision."""
        await self._db.execute(
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
        await self._db.execute(
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
        await self._db.execute(
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
        ttl_seconds: int = 300,
    ) -> str:
        """Create a new reservation with expiry, return the id."""
        cursor = await self._db.execute(
            "INSERT INTO reservations "
            "(request_id, account_id, model_id, estimated_tokens, "
            "estimated_microdollars, expires_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', ?))",
            (
                request_id,
                account_id,
                model_id,
                estimated_tokens,
                estimated_microdollars,
                f"+{ttl_seconds} seconds",
            ),
        )
        last_id = cursor.lastrowid
        return str(last_id) if last_id is not None else ""

    async def release(self, reservation_id: str, reason: str) -> None:
        """Mark a reservation as released."""
        await self._db.execute(
            "UPDATE reservations SET status = 'released', "
            "released_at = CURRENT_TIMESTAMP, release_reason = ? "
            "WHERE id = ? AND status = 'active'",
            (reason, reservation_id),
        )

    async def release_for_request(
        self,
        request_id: str,
        reason: str,
    ) -> None:
        """Release all active reservations for a request."""
        await self._db.execute(
            "UPDATE reservations SET status = 'released', "
            "released_at = CURRENT_TIMESTAMP, release_reason = ? "
            "WHERE request_id = ? AND status = 'active'",
            (reason, request_id),
        )

    async def reconcile_expired(self) -> int:
        """Release all reservations past their expiry, return count."""
        cursor = await self._db.execute(
            "UPDATE reservations SET status = 'expired', "
            "released_at = CURRENT_TIMESTAMP, release_reason = 'expired' "
            "WHERE status = 'active' AND expires_at IS NOT NULL "
            "AND expires_at < CURRENT_TIMESTAMP",
        )
        return cursor.rowcount

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
        cursor = await self._db.execute(
            "INSERT INTO request_attempts "
            "(request_id, attempt_number, account_id) "
            "VALUES (?, ?, ?)",
            (request_id, attempt_number, account_id),
        )
        last_id = cursor.lastrowid
        if last_id is None:
            msg = "INSERT into request_attempts returned no lastrowid"
            raise RuntimeError(msg)
        return last_id

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
            await self._db.execute(
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
            await self._db.execute(
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
        """Return aggregated costs for 5h, 7d, 30d windows."""
        windows: dict[str, str] = {
            "5h": (
                "SELECT COALESCE(SUM(cost_microdollars), 0) FROM requests "
                "WHERE account_id = ? "
                "AND started_at >= datetime(?, '-5 hours') "
                "AND status != 'cancelled'"
            ),
            "7d": (
                "SELECT COALESCE(SUM(cost_microdollars), 0) FROM requests "
                "WHERE account_id = ? "
                "AND started_at >= datetime(?, '-7 days') "
                "AND status != 'cancelled'"
            ),
            "30d": (
                "SELECT COALESCE(SUM(cost_microdollars), 0) FROM requests "
                "WHERE account_id = ? "
                "AND started_at >= datetime(?, '-30 days') "
                "AND status != 'cancelled'"
            ),
        }
        result: dict[str, int] = {}
        for key, sql in windows.items():
            row = await self._db.fetch_one(sql, (account_id, now_iso))
            result[key] = int(row[0]) if row is not None else 0
        return result


class PriceSnapshotRepository:
    """CRUD operations for model_price_snapshots."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_latest(self, model_id: str) -> dict[str, Any] | None:
        """Get the most recent price snapshot for a model."""
        row = await self._db.fetch_one(
            "SELECT * FROM model_price_snapshots "
            "WHERE model_id = ? ORDER BY captured_at DESC LIMIT 1",
            (model_id,),
        )
        return dict(row) if row is not None else None

    async def record(
        self,
        model_id: str,
        input_price_per_1k: float | None,
        output_price_per_1k: float | None,
    ) -> None:
        """Record a new price snapshot."""
        await self._db.execute(
            "INSERT INTO model_price_snapshots "
            "(model_id, input_price_per_1k, output_price_per_1k) "
            "VALUES (?, ?, ?)",
            (model_id, input_price_per_1k, output_price_per_1k),
        )

    async def record_from_dict(
        self,
        model_id: str,
        prices_dict: dict[str, float | None],
    ) -> None:
        """Record prices from a dictionary with input/output keys."""
        await self.record(
            model_id,
            input_price_per_1k=prices_dict.get("input_price_per_1k"),
            output_price_per_1k=prices_dict.get("output_price_per_1k"),
        )
