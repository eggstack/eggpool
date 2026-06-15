# Phase 15: Concurrency and Accounting Correctness

## Purpose

Phase 14 removed deployment blockers and brought GoRouter close to usable beta quality. Phase 15 addresses the remaining defects concentrated in shared SQLite connection safety, in-memory reservation reconciliation, cooldown recovery, long-running request expiry semantics, and accounting consistency.

## Changes

### 1. Serialized SQLite Connection Operations

All SQL operations on the shared `aiosqlite.Connection` are now serialized through a single `_connection_lock`. The previous `_wait_for_connection_access()` TOCTOU check was replaced with:

- `_current_task_owns_transaction()`: Checks if the current task owns the active transaction (both task identity and ContextVar depth must match).
- `_connection_access()`: Context manager that acquires the connection lock for non-transaction callers, or is a no-op when the current task already holds the lock.
- `execute()`, `fetch_one()`, `fetch_all()`: All guarded by `_connection_access()`.
- `transaction()`: Holds `_connection_lock` for the entire outer transaction lifetime, preventing any other task from interleaving SQL.

Child tasks with inherited ContextVar depth but different task identity must wait on the lock and start a fresh outer transaction.

### 2. Conditional Reservation and Active-Count Cleanup

`RequestFinalizer.finalize()` now captures whether `ReservationRepository.release()` actually transitioned a row (`reservation_released`). In-memory cleanup (`remove_reservation`, `decrement_active_request_count`) only runs when both the request transitioned AND the reservation was released.

`AttemptFinalizer.finalize_failed_attempt()` returns an `AttemptFinalizeResult` dataclass with `attempt_transitioned` and `reservation_released` fields, replacing the ambiguous boolean.

The coordinator uses these structured results to gate in-memory cleanup, preventing double-decrement when `AttemptFinalizer` already released the reservation.

### 3. Quota Cooldown Recovery

`HealthManager._refresh_transient_state()` restores `quota_exhausted` and `rate_limited` accounts to `healthy` after their cooldown expires. Called at the start of `is_account_healthy()` and `is_model_healthy()`.

`AccountRuntimeState.refresh_transient_state()` clears expired cooldown for `quota_exhausted`, `rate_limited`, and `cooldown` states. Called from `is_eligible()`.

Authentication-failed accounts do NOT auto-recover.

### 4. Pending-Request-Safe Reservation Expiry

`reconcile_expired_reservations()` and `ReservationRepository.reconcile_expired()` now exclude reservations whose parent request has `status = 'pending'`. Active long-running requests cannot lose reservations to background cleanup.

Default reservation TTL increased from 300s to 900s to exceed typical upstream timeouts.

### 5. Cancelled Request Usage-Window Persistence

Usage window SQL replaced `status != 'cancelled'` with `status != 'pending' AND cost_microdollars > 0`. Cancelled requests with nonzero proxy-observed cost now appear in 5h/7d/30d quota totals. Zero-cost terminal requests don't inflate totals.

### 6. Cache-Only Price Snapshots

`CatalogService._maybe_insert_price_snapshot()` now creates snapshots when only cache rates are available (not just input/output). Source changes also trigger new snapshots even when numeric values are equal.

### 7. Cache-Only Cost Calculation

`RequestFinalizer.finalize()` triggers cost calculation whenever ANY token category is nonzero (`any((input, output, cache_read, cache_write))`), not just input/output tokens.

### 8. Normalized Health Categories

`FailureCategory` StrEnum and `classify_failure_category()` function provide a shared vocabulary for `HealthManager`, `AccountRuntimeState`, routing eligibility, and coordinator health transitions. Raw exception class strings are no longer passed as mutable health-state vocabulary.

### 9. Resolution Status Integration

`models.resolution_status` is explicitly set to `'resolved'` during catalog persistence for all models with resolved protocols.

## Key Files

| File | Changes |
|------|---------|
| `src/go_aggregator/db/connection.py` | Single connection lock, `_connection_access()`, task ownership tracking |
| `src/go_aggregator/request/finalizer.py` | Reservation release gating, cache-only cost trigger, normalized health categories |
| `src/go_aggregator/request/attempt_finalizer.py` | `AttemptFinalizeResult` dataclass |
| `src/go_aggregator/request/coordinator.py` | Structured attempt results, `FailureCategory` usage |
| `src/go_aggregator/health/health_manager.py` | `FailureCategory`, `classify_failure_category()`, `_refresh_transient_state()` |
| `src/go_aggregator/accounts/state.py` | `refresh_transient_state()`, normalized category handling |
| `src/go_aggregator/background/cleanup.py` | Pending-request exclusion in expiry SQL |
| `src/go_aggregator/db/repositories.py` | Pending-request exclusion, TTL default 900s, usage window filters |
| `src/go_aggregator/catalog/service.py` | Cache-only snapshot support, `resolution_status` integration |
| `tests/integration/test_phase15_end_to_end.py` | Integration matrix (17 tests) |
