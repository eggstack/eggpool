# Phase 14: Deployment Blockers and Operational Hardening

## Audience

This plan is written for a smaller coding model. Follow the sections in order. Do not skip the deployment blockers. Do not introduce unrelated features or broad refactors.

Implementation rules:

- Make one coherent behavioral change per commit.
- Add or update tests in the same commit as the behavior change.
- Run the focused tests after each section.
- Run the full test suite before beginning the next section.
- Do not modify old migrations in place.
- Preserve the existing privacy invariant: prompts, responses, tool arguments, API keys, and authorization headers must not be persisted.

---

## Current state

Phase 13 completed most of the request lifecycle and transaction work. The remaining issues are narrower but still important for deployment:

1. The readiness savepoint is not released and bypasses transaction ownership.
2. MiniMax and Qwen model families are mapped to the wrong endpoint protocol.
3. An unresolved model can cause the entire catalog persistence transaction to roll back.
4. Database operations outside `Database.transaction()` can still run on the shared connection during another task's transaction.
5. Retry classification retries unknown 4xx responses too aggressively.
6. A 402 quota-exhausted account is not placed into a durable runtime cooldown.
7. Anthropic cache-creation tokens are discarded instead of counted as cache-write tokens.
8. Price discovery does not capture cache read/write rates.
9. Expired-reservation cleanup can race normal request completion and subtract the wrong in-memory amount.
10. The final exhausted attempt can receive duplicate health failures.
11. Duplicate request finalization can still create duplicate account events.
12. Streaming pre-body error responses are not always explicitly closed.
13. Protocol-resolution and readiness behavior need targeted regression tests.

---

# Phase exit outcomes

Phase 14 is complete when all of the following are true:

1. Calling `/v1/readyz` never leaves SQLite inside a transaction.
2. A readiness probe cannot commit, roll back, or observe another task's uncommitted request lifecycle writes.
3. Current OpenCode Go model families resolve to the correct local endpoint family.
4. Unresolved models are quarantined without rolling back resolved catalog updates.
5. Arbitrary 4xx responses are not retried across all subscriptions.
6. A 402 response places the failing account into a bounded cooldown that affects subsequent independent requests.
7. Cache creation/read usage is represented in persisted telemetry and cost calculations.
8. Expired-reservation reconciliation only adjusts reservations it actually transitions.
9. Each upstream attempt affects health exactly once.
10. Duplicate request finalization cannot create duplicate account events.
11. Every manually opened streaming response is explicitly closed on pre-body failure.
12. Full CI and the Phase 14 integration matrix pass.

---

# 1. Fix readiness transaction ownership

## Goal

Make the database writeability probe safe, complete, and serialized with all other writes.

## Files

```text
src/go_aggregator/db/connection.py
src/go_aggregator/app.py
tests/integration/test_readiness_transactions.py
tests/integration/test_database_transactions.py
```

## Task 1.1: Add a database writeability probe helper

Add this method to `Database`:

```python
async def probe_writable(self) -> bool:
    ...
```

Do not implement the probe directly in `app.py`.

Use a private sentinel exception:

```python
class _RollbackProbe(Exception):
    pass
```

Required implementation:

```python
async def probe_writable(self) -> bool:
    try:
        async with self.transaction():
            await self.execute(
                "INSERT INTO health_probe (probe_at) "
                "VALUES (CURRENT_TIMESTAMP)"
            )
            raise _RollbackProbe
    except _RollbackProbe:
        return True
    except Exception:
        return False
```

This deliberately rolls back only the transaction owned by the probe task.

Do not use a bare `SAVEPOINT` from application code.

## Task 1.2: Replace readiness SQL

In `readyz()` remove all direct `SAVEPOINT`, `ROLLBACK TO`, and raw probe writes.

Use:

```python
if not await db.probe_writable():
    return degraded_response(...)
```

## Task 1.3: Add operation ownership protection

The shared connection must not allow another task to issue operations while a different task owns the transaction.

Add task ownership tracking:

```python
self._transaction_owner: asyncio.Task[object] | None = None
```

Inside the outermost transaction:

```python
owner = asyncio.current_task()
self._transaction_owner = owner
...
self._transaction_owner = None
```

Add a helper:

```python
async def _wait_for_connection_access(self) -> None:
```

Behavior:

- If no transaction owner exists, proceed.
- If the current task owns the transaction, proceed.
- Otherwise wait for `_transaction_lock` to become available, then release it immediately and proceed.

Call this helper at the beginning of:

```text
execute
fetch_one
fetch_all
```

Do not acquire the transaction lock recursively when the current task owns it.

## Task 1.4: Prevent child-task ownership inheritance

`ContextVar` depth alone is insufficient because newly created child tasks inherit context values.

Nested transaction detection must require both:

```python
self._transaction_depth.get() > 0
and self._transaction_owner is asyncio.current_task()
```

If depth is nonzero but the current task is not the owner, wait for the transaction lock and start a new outer transaction after the owner completes.

## Required tests

### Readiness followed by normal request transaction

1. Call `db.probe_writable()`.
2. Open a normal transaction.
3. Insert and commit a row.
4. Assert no `cannot start a transaction within a transaction` error.

### Readiness concurrent with request transaction

Task A:

- Starts transaction.
- Inserts a request row.
- Waits.

Task B:

- Calls `probe_writable()`.

Assert:

- Task B waits until Task A completes.
- Task B cannot roll back Task A's row.
- Task B returns `True` afterward.

### Child task isolation

Inside an active transaction, create a child task that calls `db.transaction()`.

Assert the child task waits rather than being treated as a nested transaction owner.

## Section 1 acceptance criteria

- `/readyz` cannot leave an open transaction.
- Shared connection operations respect transaction ownership.
- Child tasks do not inherit transaction ownership accidentally.

---

# 2. Correct OpenCode Go protocol mappings

## Goal

Map current model families to the endpoint family actually used by OpenCode Go.

## Files

```text
src/go_aggregator/catalog/protocols.py
tests/unit/test_protocol_resolution.py
tests/integration/test_catalog_mixed_protocol.py
```

## Task 2.1: Correct family mappings

Use these mappings:

```python
FAMILY_PROTOCOLS = {
    "gpt-": "openai",
    "o1-": "openai",
    "o3-": "openai",
    "claude-": "anthropic",
    "glm-": "openai",
    "kimi-": "openai",
    "mimo-": "openai",
    "deepseek-": "openai",
    "minimax-": "anthropic",
    "qwen3.": "anthropic",
}
```

Do not retain `"qwen-"`; it does not match IDs such as `qwen3.7-max`.

## Task 2.2: Add representative exact tests

Required assertions:

```text
minimax-m3 -> anthropic
minimax-m2.7 -> anthropic
minimax-m2.5 -> anthropic
qwen3.7-max -> anthropic
qwen3.7-plus -> anthropic
qwen3.6-plus -> anthropic
glm-5.1 -> openai
kimi-k2.7 -> openai
mimo-v2.5 -> openai
deepseek-v4-pro -> openai
```

## Task 2.3: Test endpoint validation

For one MiniMax and one Qwen model:

- `/messages` protocol validation succeeds.
- `/chat/completions` validation raises `ProtocolMismatchError`.

## Section 2 acceptance criteria

- Current MiniMax and Qwen models route through the Anthropic-compatible path.
- Representative OpenAI-compatible families remain unchanged.

---

# 3. Quarantine unresolved models safely

## Goal

An unresolved model must not be exposed or routed, but it also must not roll back persistence of resolved models.

## Files

```text
src/go_aggregator/db/schema/0011_model_resolution_status.sql
src/go_aggregator/catalog/cache.py
src/go_aggregator/catalog/service.py
src/go_aggregator/db/repositories.py
tests/integration/test_catalog_unresolved_models.py
```

## Task 3.1: Add a forward migration

Create:

```text
src/go_aggregator/db/schema/0011_model_resolution_status.sql
```

Add:

```sql
ALTER TABLE models ADD COLUMN resolution_status TEXT NOT NULL DEFAULT 'resolved';
```

Keep the existing protocol column unchanged for compatibility in this phase.

## Task 3.2: Skip unresolved model upserts

In `_persist_catalog()`:

```python
protocol = model_info.get("protocol")
if protocol not in ("openai", "anthropic"):
    logger.warning(
        "Skipping unresolved model during catalog persistence: %s",
        model_id,
    )
    continue
```

Do not attempt to insert `NULL` into `models.protocol`.

## Task 3.3: Persist a bounded unresolved event

Add an account or catalog event with only:

```text
model_id
resolution_status = unresolved
metadata key names
```

Do not persist the complete raw model metadata if it can contain uncontrolled provider data.

If adding event persistence is too broad, log the bounded event and defer database tracking. Skipping unresolved rows is mandatory; event persistence is secondary.

## Task 3.4: Ensure resolved models still commit

The transaction must continue and persist all resolved models even when unresolved models are present in the cache.

## Task 3.5: Keep unresolved models hidden

Confirm all of these remain true:

- `get_models_for_exposure()` excludes unresolved models.
- `is_model_available()` returns false for unresolved-only models.
- Request validation fails before lifecycle persistence.

## Required test

Build one refresh containing:

```text
known resolved model
unknown unresolved model
second known resolved model
```

Assert:

- Both resolved models are persisted.
- The unresolved model is not persisted as OpenAI.
- The refresh transaction commits.
- Only resolved models are exposed.

## Section 3 acceptance criteria

- One unknown model cannot roll back an otherwise valid catalog refresh.
- No unresolved model receives an implicit OpenAI protocol.

---

# 4. Tighten retry classification defaults

## Goal

Retry only statuses explicitly known to be retryable.

## Files

```text
src/go_aggregator/retry/classification.py
tests/unit/test_retry_classification.py
tests/integration/test_failover_matrix.py
```

## Task 4.1: Replace fallback behavior

Use this policy:

```text
400-499 -> BAD_REQUEST unless explicitly handled
500-599 -> TEMPORARY unless explicitly handled
other statuses -> NEVER
```

Suggested implementation after explicit branches:

```python
if 400 <= status_code < 500:
    return RetryableError(
        status_code=status_code,
        category=RetryCategory.BAD_REQUEST,
        message=f"Non-retryable client error: {status_code}",
    )

if 500 <= status_code < 600:
    return RetryableError(
        status_code=status_code,
        category=RetryCategory.TEMPORARY,
        message=f"Temporary upstream error: {status_code}",
    )

return RetryableError(
    status_code=status_code,
    category=RetryCategory.NEVER,
    message=f"Unclassified upstream status: {status_code}",
)
```

## Task 4.2: Keep explicit statuses

Preserve explicit handling for:

```text
400
401
402
403
404 semantic model-unavailable
429
500
502
503
504
```

Optional explicit retry handling for 408 or 425 may be added only with tests.

## Required tests

Assert no retry for:

```text
405
409
413
415
422
```

Assert retry remains enabled for:

```text
429
500
502
503
504
```

## Section 4 acceptance criteria

- Arbitrary client errors do not consume every subscription.

---

# 5. Implement authoritative 402 cooldown

## Goal

A subscription that returns 402 must remain ineligible for a bounded period after the current request ends.

## Files

```text
src/go_aggregator/models/config.py
config.example.toml
src/go_aggregator/health/health_manager.py
src/go_aggregator/accounts/state.py
src/go_aggregator/request/coordinator.py
tests/integration/test_quota_cooldown.py
```

## Task 5.1: Add configuration

Add:

```python
quota_exhausted_cooldown_seconds: float = 300.0
```

under routing or health configuration.

Expose it in `config.example.toml`.

## Task 5.2: Add explicit health transition

Add to `HealthManager`:

```python
def record_quota_exhausted(
    self,
    account_name: str,
    cooldown_seconds: float,
) -> None:
```

Required state:

```text
health_state = quota_exhausted
cooldown_until = now + cooldown_seconds
```

The account must be ineligible while the cooldown remains active.

## Task 5.3: Normalize runtime state

Call:

```python
state.record_failure("quota_exhausted")
```

Do not pass class names such as `QuotaExhaustedError` into runtime state transitions.

## Task 5.4: Use the explicit transition

In `_apply_health_transition()`:

```python
elif "QuotaExhausted" in error_class:
    health_manager.record_quota_exhausted(
        account_name,
        self._quota_exhausted_cooldown_seconds,
    )
    state.record_failure("quota_exhausted")
```

Avoid recording the same transition twice.

## Required test

1. Account A returns 402.
2. Account B succeeds.
3. Submit a new independent request immediately.
4. Assert A is not selected.
5. Advance the test clock beyond cooldown.
6. Assert A becomes eligible again unless another health rule blocks it.

## Section 5 acceptance criteria

- A 402 response changes routing behavior beyond the current request.

---

# 6. Complete cache usage accounting and pricing

## Goal

Persist and price cache-read and cache-write usage correctly.

## Files

```text
src/go_aggregator/request/coordinator.py
src/go_aggregator/proxy/usage.py
src/go_aggregator/proxy/sse_observer.py
src/go_aggregator/models/config.py
src/go_aggregator/catalog/service.py
src/go_aggregator/catalog/pricing.py
tests/unit/test_pricing.py
tests/integration/test_cache_accounting.py
```

## Task 6.1: Pass cache creation as cache write

In non-streaming finalization:

```python
cache_write_tokens=(usage.cache_creation_tokens if usage else 0)
```

In streaming finalization, cancellation, and midstream error paths:

```python
cache_write_tokens=usage_result.cache_creation_tokens
```

Do this consistently in every `FinalizationData` construction.

## Task 6.2: Extend model override configuration

Add integer canonical fields:

```python
cache_read_per_million_microdollars: int | None = None
cache_write_per_million_microdollars: int | None = None
```

Prefer canonical integer units over adding another float-per-token representation.

## Task 6.3: Extract upstream cache rates

In `_maybe_insert_price_snapshot()`, recognize provider metadata fields only when their units are explicit.

Support these normalized keys if present:

```text
cache_read_per_million_microdollars
cache_write_per_million_microdollars
```

If upstream metadata exposes dollars per token or dollars per thousand, convert only in a unit-specific helper with tests.

Do not guess units from ambiguous field names.

## Task 6.4: Persist cache rates

Pass cache rates into `PriceSnapshotRepository.record()`.

When comparing the latest snapshot, include cache rates and source. A change in cache price must create a new snapshot even if input/output rates are unchanged.

## Task 6.5: Document tiered pricing limitation

The current flat snapshot cannot represent context-dependent tiers such as prices changing above a token threshold.

Add a README limitation:

```text
Context-tiered prices are conservatively estimated until pricing-rule support is added.
```

Do not attempt a broad pricing-rule engine in this phase.

## Required tests

- Anthropic non-streaming cache creation reaches `cache_write_tokens`.
- Streaming cache creation reaches `cache_write_tokens`.
- Cache read/write rates affect final microdollar cost.
- A cache-rate-only price change creates a new snapshot.
- Missing cache-write rate with nonzero cache creation produces `estimated`, not `derived`.

## Section 6 acceptance criteria

- Cache usage is no longer silently discarded or priced as zero.

---

# 7. Make expired-reservation cleanup race-safe

## Goal

Reconcile only reservations that the cleanup task itself transitions from active to expired.

## Files

```text
src/go_aggregator/background/cleanup.py
src/go_aggregator/db/repositories.py
src/go_aggregator/app.py
tests/integration/test_reservation_expiry_race.py
```

## Task 7.1: Use `UPDATE ... RETURNING`

Inside one transaction:

```sql
UPDATE reservations
SET status = 'expired',
    released_at = CURRENT_TIMESTAMP,
    release_reason = 'expired'
WHERE status = 'active'
  AND expires_at IS NOT NULL
  AND expires_at < CURRENT_TIMESTAMP
RETURNING id, account_id, estimated_microdollars;
```

Fetch the returned rows before leaving the transaction.

Do not select candidates outside the transaction.

## Task 7.2: Reconcile only returned rows

After commit, for each returned row:

- Resolve account name.
- Remove exactly that reservation estimate from in-memory reserved totals.
- Decrement active request count exactly once.

Pass both the quota estimator and router into cleanup.

## Task 7.3: Prevent negative or duplicate reconciliation

All in-memory decrement methods must remain clamped at zero.

Add debug logging when cleanup tries to reconcile an amount larger than the current tracked reservation total.

## Required race test

1. Create an expired active reservation.
2. Start cleanup and pause before the update.
3. Finalize the request normally, releasing the reservation.
4. Resume cleanup.
5. Assert cleanup returns no transitioned row for that reservation.
6. Assert no other reservation amount or active count was decremented.

## Section 7 acceptance criteria

- Expiry cleanup cannot subtract cost belonging to another request.

---

# 8. Make health and event updates exactly once

## Goal

One upstream attempt must affect health once, and one request finalization must create at most one terminal event.

## Files

```text
src/go_aggregator/request/attempt_finalizer.py
src/go_aggregator/request/finalizer.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/db/repositories.py
tests/integration/test_health_idempotency.py
tests/integration/test_event_idempotency.py
```

## Task 8.1: Assign health ownership to attempt finalization

For upstream failures before downstream commitment:

- Apply health transition when the attempt finalizer successfully transitions the attempt.
- Do not apply health if the attempt was already terminal.

The coordinator should call `_apply_health_transition()` only when:

```python
finalized is True
```

## Task 8.2: Stop request finalizer from duplicating upstream failure health

Add to `FinalizationData`:

```python
health_already_applied: bool = False
```

In `RequestFinalizer`, apply failure health only when:

```python
not data.health_already_applied
```

When `_handle_exhausted()` finalizes a request after the final attempt was already attempt-finalized, pass:

```python
health_already_applied=True
```

## Task 8.3: Guard account event creation

Move account-event insertion inside:

```python
if transitioned:
```

A duplicate call to `RequestFinalizer.finalize()` must not create another event.

## Task 8.4: Add event uniqueness if practical

Optional but preferred: include `request_id` and event type in the event details and add a repository-level idempotent insert or unique index if schema changes are straightforward.

Do not make this optional work block the core `if transitioned` fix.

## Required tests

### Exhausted final attempt

One attempt returns 500 and no alternate account exists.

Assert:

- Consecutive failure count increments once.
- Circuit-breaker failure count increments once.
- One account event is created.

### Duplicate finalization

Call request finalizer twice.

Assert terminal state, health counters, and account-event count remain unchanged after the second call.

## Section 8 acceptance criteria

- Health and event side effects are exactly once per relevant transition.

---

# 9. Close streaming pre-body responses explicitly

## Goal

Every manually opened HTTPX streaming response must be closed when no downstream stream is returned.

## Files

```text
src/go_aggregator/request/coordinator.py
tests/integration/test_streaming_error_response_closure.py
```

## Task 9.1: Wrap pre-body error handling

After:

```python
response = await client.send(request, stream=True)
```

For `response.status_code >= 400`, use:

```python
try:
    await response.aread()
    ...classify or finalize...
finally:
    await response.aclose()
```

Ensure the close occurs before raising `_RetryableUpstreamError` or `_NonRetryableUpstreamError`.

## Task 9.2: Preserve successful stream ownership

Do not close a successful streaming response before returning the generator. The generator's existing `finally` remains responsible for closure.

## Required tests

Use a custom/mock transport response that records `aclose()` calls.

Assert closure for:

```text
401
402
429
500
non-retryable 400
model-specific 404
```

Assert successful streaming response is closed only when generator completion/cancellation occurs.

## Section 9 acceptance criteria

- Repeated streaming error failovers do not leak pool connections.

---

# 10. Align catalog cache and persistence metadata

## Goal

Ensure cached models loaded from SQLite retain protocol source and resolution state consistently.

## Files

```text
src/go_aggregator/catalog/cache.py
src/go_aggregator/catalog/service.py
tests/integration/test_catalog_cache_reload.py
```

## Task 10.1: Load protocol source from SQLite

`_load_cached_models()` currently queries `protocol_source` but must pass it into `cache.load_model()`.

Extend `load_model()` if necessary:

```python
protocol_source: str | None = None
```

Store it in cache model metadata.

## Task 10.2: Preserve source through refresh fallback

When persisted protocol is used as the final resolver fallback, mark source as `persisted`.

When an existing cache entry was originally resolved from upstream metadata and the new refresh contains no resolution metadata, choose deliberately:

- Use persisted protocol with source `persisted`, or
- Preserve the prior source separately as provenance.

For this phase, source `persisted` is acceptable and clearer.

## Required test

1. Persist a model with protocol source.
2. Restart and load cache.
3. Refresh with metadata lacking protocol hints.
4. Assert protocol remains stable and the model remains routable.

## Section 10 acceptance criteria

- Restart and refresh do not silently lose protocol provenance or routability.

---

# 11. Final integration matrix

Create or expand:

```text
tests/integration/test_phase14_end_to_end.py
```

The following scenarios are mandatory.

## A. Readiness safety

- `/readyz` followed by a normal proxy request succeeds.
- Concurrent readiness and request transaction do not interfere.

## B. Current protocol families

- MiniMax and Qwen route through `/messages`.
- GLM/Kimi/MiMo/DeepSeek route through `/chat/completions`.

## C. Unresolved model quarantine

- Mixed refresh with resolved and unresolved models commits resolved rows.
- Unresolved model is not exposed or persisted as OpenAI.

## D. Retry default safety

- 422 from account A is passed through without trying account B.
- 500 from account A still fails over to account B.

## E. 402 cooldown

- Account A returns 402.
- Account B succeeds.
- Immediate next request excludes A.

## F. Cache accounting

- Anthropic cache creation persists as cache-write tokens.
- Final cost includes cache read/write rates.

## G. Expiry race

- Normal release racing expiry cleanup does not double-decrement memory state.

## H. Health idempotency

- One final failed attempt increments health once.
- Duplicate request finalization creates no duplicate event.

## I. Streaming response closure

- Repeated pre-body streaming failures close every upstream response.

## J. Privacy regression

Search database and logs for known prompt, completion, API-key, and authorization marker strings. None may appear.

---

# 12. Recommended implementation order

Follow this order exactly:

1. Readiness and database operation ownership.
2. Protocol-family mapping correction.
3. Unresolved-model persistence quarantine.
4. Retry classification defaults.
5. 402 cooldown.
6. Cache usage and cache pricing.
7. Race-safe reservation expiry cleanup.
8. Health/event exactly-once behavior.
9. Streaming pre-body response closure.
10. Cache reload/protocol-source consistency.
11. Full Phase 14 integration matrix.

Do not begin pricing or dashboard-related follow-up work before Sections 1–3 pass.

---

# 13. Suggested commit sequence

```text
fix: make readiness probe transaction-safe
fix: correct opencode go protocol family mappings
fix: quarantine unresolved models during catalog persistence
fix: make unknown 4xx responses non-retryable
fix: add authoritative quota-exhausted cooldown
fix: persist and price cache write usage
fix: make reservation expiry reconciliation race-safe
fix: make health and event side effects idempotent
fix: close streaming pre-body error responses
fix: preserve protocol source across cache reload
test: add phase 14 deployment hardening matrix
```

---

# 14. Definition of done

Phase 14 is complete only when all of these statements are true:

1. `/readyz` cannot leave an open transaction.
2. Readiness cannot interfere with another task's database work.
3. Child tasks cannot inherit transaction ownership accidentally.
4. MiniMax and Qwen resolve to the Anthropic-compatible endpoint.
5. Current OpenAI-compatible families remain correct.
6. Unresolved models do not roll back resolved catalog persistence.
7. Unresolved models are not exposed or routed.
8. Unknown 4xx responses are not retried across subscriptions.
9. A 402 response causes a bounded cooldown affecting later requests.
10. Cache creation is stored as cache-write usage.
11. Cache read/write rates affect final cost and exactness.
12. Expiry cleanup reconciles only rows it transitions.
13. One attempt changes health once.
14. Duplicate finalization creates no duplicate account event.
15. Streaming pre-body failures close upstream responses.
16. Protocol source survives restart and refresh.
17. Full test suite, linting, formatting, and type checking pass.
18. No user content or secrets are persisted.
