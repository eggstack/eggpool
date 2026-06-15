# Phase 15: Concurrency and Accounting Correctness

## Purpose

Phase 14 removed the previous deployment blockers and brought GoRouter close to usable beta quality. The remaining defects are concentrated in shared SQLite connection safety, in-memory reservation reconciliation, cooldown recovery, long-running request expiry semantics, and accounting consistency.

This plan is intentionally explicit so a smaller coding model can implement it without making architectural decisions. Follow the sections in order. Do not introduce unrelated features.

---

## Rules for the implementing model

1. Complete sections in order.
2. Add focused tests with each behavioral change.
3. Run the focused test file after each task.
4. Run the full test suite before beginning the next section.
5. Do not edit old migrations unless this plan explicitly requires a new migration.
6. Do not store prompts, completions, tool arguments, API keys, or authorization headers.
7. Do not add a second independent database abstraction.
8. Do not replace SQLite with PostgreSQL, Redis, or another service.
9. Keep the single-process Raspberry Pi deployment model.
10. Prefer deleting obsolete code after replacement rather than leaving two competing paths.

---

## Phase exit outcomes

Phase 15 is complete when:

1. No task can execute SQL on the shared connection while another task owns a transaction.
2. Child tasks cannot inherit transaction ownership accidentally.
3. Exhausted retries cannot remove a reservation or active count twice.
4. A quota-exhausted account becomes eligible again after cooldown expiration.
5. Pending long-running requests are never expired by background reservation cleanup.
6. Cancelled requests with nonzero estimated cost remain in persisted usage windows.
7. Cache-only price updates are stored.
8. Cache-only usage invokes cost calculation.
9. Runtime health state and `HealthManager` use the same normalized failure categories.
10. The unused `resolution_status` schema field is either integrated or explicitly documented as reserved.
11. CI and the full Phase 15 integration matrix pass.

---

# 1. Serialize all SQLite connection operations

## Goal

Eliminate the remaining time-of-check/time-of-use race on the single shared `aiosqlite.Connection`.

The current `_wait_for_connection_access()` check is insufficient because it releases control before the actual SQL statement executes. A transaction can begin between the check and the SQL operation.

## Files

```text
src/go_aggregator/db/connection.py
tests/integration/test_database_transactions.py
tests/integration/test_connection_operation_serialization.py
```

## Required design

Use one connection gate for all operations.

Add:

```python
self._connection_lock = asyncio.Lock()
self._transaction_owner: asyncio.Task[object] | None = None
self._transaction_depth: ContextVar[int] = ContextVar(
    "database_transaction_depth",
    default=0,
)
```

The same lock must protect:

- `execute()`
- `fetch_one()`
- `fetch_all()`
- outermost `transaction()`

The transaction owner may execute SQL without reacquiring the lock because it already owns it.

## Task 1.1: Replace `_wait_for_connection_access()`

Delete `_wait_for_connection_access()`.

Add:

```python
def _current_task_owns_transaction(self) -> bool:
    return (
        self._transaction_owner is not None
        and self._transaction_owner is asyncio.current_task()
        and self._transaction_depth.get() > 0
    )
```

## Task 1.2: Add one operation helper

Add:

```python
@asynccontextmanager
async def _connection_access(self):
    if self._current_task_owns_transaction():
        yield
        return

    async with self._connection_lock:
        yield
```

Do not use `_transaction_lock` and `_connection_lock` separately. Use one lock for both connection operations and transactions. Rename the existing transaction lock to `_connection_lock` if practical.

## Task 1.3: Guard every SQL operation

Implement:

```python
async def execute(...):
    async with self._connection_access():
        return await self.connection.execute(sql, params)
```

Do the same for:

```text
fetch_one
fetch_all
```

Do not hold the lock after returning a cursor that another task may consume later. Therefore:

- `fetch_one()` and `fetch_all()` must execute and fetch while holding the lock.
- `execute()` may return a cursor only for transaction-owner code or immediate rowcount use.

Preferred correction:

Add explicit helpers:

```python
async def execute_write(...) -> int
async def execute_returning(...) -> list[aiosqlite.Row]
```

Use these for non-transaction callers so cursor lifetime does not escape the lock.

Minimum acceptable behavior:

- Audit all `execute()` callers.
- Confirm returned cursors are consumed before another operation can interleave.

## Task 1.4: Replace `transaction()`

Use the connection lock for the entire outer transaction:

```python
@asynccontextmanager
async def transaction(self):
    owner = asyncio.current_task()
    depth = self._transaction_depth.get()

    if depth > 0 and self._transaction_owner is owner:
        token = self._transaction_depth.set(depth + 1)
        try:
            yield
        finally:
            self._transaction_depth.reset(token)
        return

    async with self._connection_lock:
        token = self._transaction_depth.set(1)
        self._transaction_owner = owner
        try:
            await self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self.connection.rollback()
                raise
            else:
                await self.connection.commit()
        finally:
            self._transaction_owner = None
            self._transaction_depth.reset(token)
```

Do not retain the current special branch that acquires and releases the lock before `BEGIN IMMEDIATE`.

## Task 1.5: Child-task isolation

A child task created inside a transaction may inherit `ContextVar` depth. The transaction check must require both:

```text
depth > 0
same asyncio task owns transaction
```

A child task with inherited depth but different task identity must wait on `_connection_lock` and begin a new outer transaction after the owner completes.

## Required tests

### Test A: ordinary read races transaction start

1. Pause Task A after it is scheduled but before `fetch_one()` executes.
2. Task B begins a transaction and inserts uncommitted data.
3. Resume Task A.
4. Assert Task A waits until Task B commits.
5. Assert Task A never observes uncommitted data.

### Test B: ordinary write races transaction start

1. Task A begins transaction and pauses.
2. Task B calls a write method outside `transaction()`.
3. Assert Task B waits.
4. Assert Task B's write is not committed or rolled back as part of Task A.

### Test C: child task inherited context

1. Start transaction in parent task.
2. Spawn child task that starts `transaction()`.
3. Assert child does not enter until parent completes.

### Test D: readiness probe concurrency

1. Start durable request selection transaction.
2. Call `probe_writable()` concurrently.
3. Assert probe waits and cannot affect the request transaction.

## Section 1 acceptance criteria

- Every operation on the shared connection is serialized.
- No SQL statement can enter another task's transaction.
- Child tasks cannot inherit transaction ownership.

---

# 2. Prevent duplicate reservation and active-count cleanup

## Goal

Make in-memory cleanup conditional on actual database state transition.

The final failed attempt may already have released its reservation through `AttemptFinalizer`. `RequestFinalizer` must not remove that reservation or decrement the active count again.

## Files

```text
src/go_aggregator/request/finalizer.py
src/go_aggregator/request/attempt_finalizer.py
src/go_aggregator/db/repositories.py
tests/integration/test_exhaustion_lifecycle.py
tests/integration/test_reservation_cleanup_idempotency.py
```

## Task 2.1: Capture reservation release result

In `RequestFinalizer.finalize()`:

```python
reservation_released = False
```

Inside the transaction:

```python
if transitioned:
    reservation_released = await self._reservation_repo.release(
        selected.reservation_id,
        reason=status,
    )
```

## Task 2.2: Gate in-memory cleanup

After commit:

```python
if transitioned and reservation_released:
    quota_estimator.remove_reservation(...)
    router.decrement_active_request_count(...)
```

Do not remove or decrement when the reservation was already released by `AttemptFinalizer` or expiry cleanup.

## Task 2.3: Preserve request-level finalization

Even when `reservation_released` is false, the request row still needs terminal transition and final cost persistence.

Do not return early merely because the reservation was already released.

## Task 2.4: Mirror the same rule in `AttemptFinalizer`

`AttemptFinalizer` already returns whether the attempt transitioned. Also capture whether reservation release changed one row.

Only perform in-memory cleanup when both conditions are appropriate:

```text
attempt transitioned
reservation changed from active to released
```

Return a structured result if needed:

```python
@dataclass(frozen=True)
class AttemptFinalizeResult:
    attempt_transitioned: bool
    reservation_released: bool
```

Use this instead of one ambiguous boolean.

## Required tests

### Exhausted final attempt with another active request

1. Request A and Request B use the same account.
2. Both add active reservations.
3. Request A's final attempt fails and is attempt-finalized.
4. Request A is then request-finalized as exhausted.
5. Assert Request B's reserved amount and active count remain intact.

### Duplicate finalization

1. Finalize one request twice.
2. Assert reserved amount and active count change only once.

### Attempt finalizer then request finalizer

1. Attempt finalizer releases reservation.
2. Request finalizer transitions request.
3. Assert no second in-memory decrement.

## Section 2 acceptance criteria

- Database release result controls in-memory cleanup.
- One reservation can never decrement aggregate state twice.

---

# 3. Restore quota-exhausted accounts after cooldown

## Goal

Ensure a 402 account automatically becomes eligible after cooldown expiration.

## Files

```text
src/go_aggregator/health/health_manager.py
src/go_aggregator/accounts/state.py
src/go_aggregator/routing/eligibility.py
tests/integration/test_quota_cooldown.py
```

## Task 3.1: Centralize cooldown recovery

Add:

```python
def _refresh_transient_state(self, health: AccountHealth) -> None:
```

Behavior:

```python
if health.cooldown_until > 0 and time.time() >= health.cooldown_until:
    if health.health_state in {"quota_exhausted", "rate_limited"}:
        health.health_state = "healthy"
        health.is_healthy = True
        health.cooldown_until = 0
```

Do not restore accounts in permanent authentication-failed state.

## Task 3.2: Call recovery from both health checks

At the beginning of:

```text
is_account_healthy
is_model_healthy
```

call `_refresh_transient_state()`.

`is_model_healthy()` should use:

```python
if not self.is_account_healthy(account_name):
    return False
```

then apply model and circuit-breaker checks.

## Task 3.3: Normalize runtime state recovery

`AccountRuntimeState` should also clear transient cooldown status when it expires.

Add a method:

```python
def refresh_transient_state(self, now: float | None = None) -> None:
```

Call it from eligibility checks if runtime state remains authoritative for dashboard/readiness.

## Required tests

1. Record quota exhaustion for account A.
2. Assert A is ineligible during cooldown.
3. Advance test clock beyond cooldown.
4. Call actual router selection.
5. Assert A is eligible again.
6. Assert authentication-failed account does not auto-recover.

## Section 3 acceptance criteria

- Cooldown expiration is observed by the actual routing path.
- Temporary and permanent failures remain distinct.

---

# 4. Prevent expiry of active pending requests

## Goal

Do not expire reservations belonging to requests that are still pending and actively executing.

## Files

```text
src/go_aggregator/background/cleanup.py
src/go_aggregator/db/repositories.py
src/go_aggregator/request/coordinator.py
tests/integration/test_reservation_expiry_pending_request.py
```

## Task 4.1: Change expiry SQL

Use:

```sql
UPDATE reservations
SET status = 'expired',
    released_at = CURRENT_TIMESTAMP,
    release_reason = 'expired'
WHERE status = 'active'
  AND expires_at IS NOT NULL
  AND expires_at < CURRENT_TIMESTAMP
  AND NOT EXISTS (
      SELECT 1
      FROM requests
      WHERE requests.id = reservations.request_id
        AND requests.status = 'pending'
  )
RETURNING id, account_id, estimated_microdollars;
```

This is the required minimum fix.

## Task 4.2: Increase reservation TTL

Set default reservation TTL to comfortably exceed the longest upstream timeout.

Current request timeout is approximately 300 seconds. Use at least:

```text
reservation_ttl_seconds = 900
```

Prefer a configuration field:

```python
reservation_ttl_seconds: int = 900
```

Pass it into `ReservationRepository.create()`.

## Task 4.3: Keep startup recovery authoritative

Do not use expiry cleanup to recover pending requests.

Startup crash recovery remains responsible for:

- Marking stale pending requests interrupted.
- Releasing their active reservations.
- Completing incomplete attempts.

## Required tests

### Long-running request

1. Create pending request and active reservation.
2. Set reservation expiry in the past.
3. Run cleanup.
4. Assert reservation remains active because request is pending.
5. Complete request normally.
6. Assert finalizer releases reservation once.

### Orphaned terminal request

1. Create terminal request with active expired reservation.
2. Run cleanup.
3. Assert reservation transitions to expired and in-memory state decrements once.

## Section 4 acceptance criteria

- Active long-running requests cannot lose reservations to cleanup.
- Orphaned terminal reservations are still reconciled.

---

# 5. Preserve cancelled request cost in usage windows

## Goal

Ensure proxy-observed cost remains durable across refresh and restart regardless of terminal status.

## Files

```text
src/go_aggregator/db/repositories.py
tests/integration/test_usage_windows.py
tests/integration/test_cancelled_cost_persistence.py
```

## Task 5.1: Replace status filter

In all usage-window SQL, replace:

```sql
status != 'cancelled'
```

with:

```sql
status != 'pending'
AND cost_microdollars > 0
```

This includes:

- completed requests
- client errors
- upstream errors
- cancelled requests
- interrupted requests

when a nonzero observed or estimated cost exists.

## Task 5.2: Define accounting semantics in code comments

Document:

```text
Usage windows represent proxy-observed or conservatively estimated upstream spend, not only successful completions.
```

## Required tests

1. Cancelled request with estimated cost appears in 5h/7d/30d totals.
2. Pending request does not appear.
3. Zero-cost terminal request does not affect totals.
4. Restart/reload preserves cancelled cost.

## Section 5 acceptance criteria

- Cancelled billable work does not disappear from quota routing.

---

# 6. Support cache-only price snapshots

## Goal

Allow a snapshot to be created when only cache rates changed or only cache rates are known.

## Files

```text
src/go_aggregator/catalog/service.py
src/go_aggregator/db/repositories.py
tests/integration/test_price_snapshot_writes.py
```

## Task 6.1: Replace early-return condition

Use:

```python
if all(
    value is None
    for value in (
        input_price,
        output_price,
        cache_read_price,
        cache_write_price,
    )
):
    return
```

## Task 6.2: Compare all price fields

Before skipping insertion, compare:

```text
input price
output price
cache read price
cache write price
source
```

A source change should create a new immutable snapshot even when numeric values remain equal.

## Task 6.3: Permit null input/output rates

`PriceSnapshotRepository.record()` already supports nullable input/output values. Preserve that behavior.

## Required tests

1. Cache-read-only metadata inserts a snapshot.
2. Cache-write-only override inserts a snapshot.
3. Cache-rate-only change creates a new snapshot.
4. No price fields returns without insertion.

## Section 6 acceptance criteria

- Cache-only pricing information is not discarded.

---

# 7. Calculate cost for cache-only usage

## Goal

Invoke cost calculation whenever any billable token category is nonzero.

## Files

```text
src/go_aggregator/request/finalizer.py
src/go_aggregator/catalog/pricing.py
tests/unit/test_pricing.py
tests/integration/test_cache_accounting.py
```

## Task 7.1: Expand cost-calculation condition

Replace:

```python
if input_tokens > 0 or output_tokens > 0:
```

with:

```python
if any(
    (
        data.input_tokens,
        data.output_tokens,
        data.cache_read_tokens,
        data.cache_write_tokens,
    )
):
```

## Task 7.2: Preserve conservative fallback

If required cache rates are missing:

- exactness must be `estimated`
- final cost must be at least the reservation estimate

## Required tests

1. Cache-read-only usage with rate calculates nonzero derived cost.
2. Cache-write-only usage with rate calculates nonzero derived cost.
3. Cache-only usage without rate falls back to estimated reservation cost.

## Section 7 acceptance criteria

- Cache-only billable usage is accounted for explicitly.

---

# 8. Normalize health categories across runtime state

## Goal

Stop passing exception class names into `AccountRuntimeState.record_failure()`.

## Files

```text
src/go_aggregator/health/categories.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/request/finalizer.py
src/go_aggregator/accounts/state.py
src/go_aggregator/health/health_manager.py
tests/unit/test_health_categories.py
tests/integration/test_health_state_consistency.py
```

## Task 8.1: Add normalized failure categories

Create constants or `StrEnum`:

```python
class FailureCategory(StrEnum):
    AUTHENTICATION_FAILED = "authentication_failed"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    CONNECT_TIMEOUT = "connect_timeout"
    CONNECTION_FAILURE = "connection_failure"
    UPSTREAM_SERVER_ERROR = "upstream_server_error"
    PROTOCOL_ERROR = "protocol_error"
    UNKNOWN = "unknown"
```

## Task 8.2: Map exceptions once

Add:

```python
def classify_failure_category(
    error_class: str | None,
    status_code: int | None,
) -> FailureCategory:
```

Use it in both:

- `HealthManager`
- `AccountRuntimeState`

## Task 8.3: Remove raw exception strings from state transitions

Replace:

```python
state.record_failure(error_class)
```

with:

```python
state.record_failure(category.value)
```

## Task 8.4: Keep diagnostic error class separately

Persist raw `error_class` in request/attempt telemetry, but do not use it as mutable health-state vocabulary.

## Required tests

1. `QuotaExhaustedError` maps to `quota_exhausted` in both health systems.
2. `RateLimitError` maps to `rate_limited`.
3. Connect timeout maps consistently.
4. Dashboard/runtime health state matches routing eligibility.

## Section 8 acceptance criteria

- Routing health and displayed runtime health cannot diverge because of naming mismatch.

---

# 9. Resolve `resolution_status` schema intent

## Goal

Avoid carrying a misleading unused schema field.

## Files

```text
src/go_aggregator/catalog/service.py
src/go_aggregator/db/repositories.py
README.md
```

## Required choice

Choose one of these two options. Prefer Option A.

### Option A: integrate the field

When persisting resolved models:

```sql
resolution_status = 'resolved'
```

If unresolved models are intentionally skipped, document that the field is reserved for future quarantine persistence.

Add repository reads/writes so the field is not completely dead.

### Option B: document as reserved

If schema rollback is undesirable, add a clear comment and README note:

```text
models.resolution_status is reserved for future unresolved-model persistence; unresolved models are currently skipped.
```

Do not create another migration solely to remove the column.

## Section 9 acceptance criteria

- The schema field has a documented purpose and does not imply behavior that does not exist.

---

# 10. Final integration matrix

Create:

```text
tests/integration/test_phase15_end_to_end.py
```

The following tests are mandatory.

## A. Shared connection serialization

- Transaction and ordinary read run concurrently.
- Transaction and ordinary write run concurrently.
- Child task inherits context but does not inherit ownership.
- No uncommitted data is observed.

## B. Exhausted retry cleanup

- Two concurrent requests share an account.
- One exhausts retries.
- The other remains active.
- Exhausted request does not reduce the other request's reservation or active count.

## C. Cooldown recovery

- 402 creates cooldown.
- Account is excluded during cooldown.
- Account is selected again after cooldown expiration.

## D. Long-running reservation

- Pending request exceeds reservation TTL.
- Cleanup does not expire it.
- Normal completion releases it once.

## E. Cancelled accounting

- Cancelled request with estimated cost remains in usage windows after refresh and simulated restart.

## F. Cache-only price update

- Cache-only rate snapshot is persisted.

## G. Cache-only usage

- Cache-only tokens produce nonzero cost.

## H. Health consistency

- Health manager, runtime state, readiness, and router agree on quota-exhausted and recovered states.

## I. Privacy regression

Search database and logs for known prompt, completion, API-key, and authorization markers. None may appear.

---

# 11. Recommended implementation order

Follow this order exactly:

1. Shared connection operation serialization.
2. Conditional reservation/active-count cleanup.
3. Quota cooldown recovery.
4. Pending-request-safe reservation expiry.
5. Cancelled request usage-window persistence.
6. Cache-only price snapshot support.
7. Cache-only cost calculation.
8. Health-category normalization.
9. `resolution_status` documentation/integration.
10. Full Phase 15 test matrix.

Do not begin health cleanup before Sections 1–4 pass.

---

# 12. Suggested commit sequence

```text
fix: serialize all sqlite connection operations
fix: gate in-memory cleanup on reservation transition
fix: restore quota-exhausted accounts after cooldown
fix: preserve reservations for pending requests
fix: retain cancelled request cost in usage windows
fix: persist cache-only price snapshots
fix: calculate cache-only request cost
refactor: normalize health failure categories
chore: document model resolution status field
test: add phase 15 concurrency and accounting matrix
```

---

# 13. Definition of done

Phase 15 is complete only when all statements below are true:

1. Every SQL operation on the shared connection is serialized.
2. No task can execute SQL inside another task's transaction.
3. Child tasks cannot inherit transaction ownership.
4. Reservation and active-count cleanup occur only when the database reservation transitions.
5. Exhausted retries cannot corrupt another request's in-memory state.
6. Quota-exhausted accounts recover after cooldown expiration.
7. Pending active requests are excluded from expiry cleanup.
8. Cancelled nonzero-cost requests remain in usage windows.
9. Cache-only rate changes create snapshots.
10. Cache-only token usage invokes cost calculation.
11. Health systems use one normalized category vocabulary.
12. `resolution_status` has an explicit documented purpose.
13. Full tests, formatting, linting, and type checking pass.
14. No request content or secrets are persisted.
