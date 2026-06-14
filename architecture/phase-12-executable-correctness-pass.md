# Phase 12: Executable Correctness Pass

## Audience

This plan is intentionally explicit enough to be followed by a smaller coding model without making architectural decisions on its own.

The implementing model should:

- Follow the tasks in order.
- Avoid unrelated refactors.
- Avoid renaming public APIs unless the plan explicitly requires it.
- Add tests with each task before proceeding.
- Run the full test suite after every numbered section.
- Stop and fix failures before beginning the next section.

Do not mark this phase complete based only on unit tests. The final integration matrix at the end is mandatory.

---

## Current problems to correct

The current repository still has these concrete defects:

1. Runtime quota capacities are not wired from configuration, so deployed routing scores can remain zero.
2. Utilization is clamped to 1.0, hiding how far over quota an account is.
3. Selection, reservation, request assignment, and attempt creation are not committed before upstream dispatch.
4. The proxy request UUID is not stored in SQLite.
5. Non-streaming pass-through 4xx responses do not finalize the request or release reservations.
6. Retry loops can select the same failed account again.
7. Authentication, quota, and model-specific errors stop failover too early.
8. Some failed attempts are not guaranteed to receive terminal attempt records.
9. `IncrementalSSEObserver` exists but is not used by the streaming coordinator.
10. The observer itself does not correctly assemble complete SSE events or preserve UTF-8 decoder state.
11. `asyncio.CancelledError` bypasses request finalization.
12. Migration `0005` converts pricing units incorrectly by a factor of one million.
13. Cache-read/cache-write token pricing is not represented in the price model.
14. Final observed cost is not immediately fed back into live routing state.
15. Reservation removal recalculates the estimate instead of removing the exact amount originally reserved.
16. First-byte latency, retry count, cache tokens, reasoning tokens, and upstream request ID are not consistently persisted.
17. Non-streaming responses are decoded and reserialized through `JSONResponse` rather than passed through as raw bytes.
18. Model protocol resolution still assumes one protocol for an entire `/models` response.
19. Body-size middleware only trusts `Content-Length` and does not bound chunked uploads.
20. Readiness does not actually verify a usable model/account pairing.
21. The database “writable” check only performs `SELECT 1`.

---

# Execution rules

## Rule 1: One behavioral change per commit

Use one commit for each major section below. Suggested commit sequence:

```text
1. fix: correct pricing migration and cache rates
2. fix: wire explicit quota capacities and projected scoring
3. fix: persist proxy request identity and add transaction support
4. fix: make pre-dispatch selection durable and atomic
5. fix: add idempotent request finalization
6. fix: complete account-aware failover
7. fix: integrate incremental SSE observer and cancellation cleanup
8. fix: feed final cost and usage into live routing
9. fix: preserve raw upstream responses
10. fix: add mixed-protocol model resolver
11. fix: enforce streamed request size and readiness semantics
12. test: add final lifecycle and soak coverage
```

## Rule 2: Do not edit old migrations

Never modify migrations `0001` through `0005`.

Add forward-only migrations.

## Rule 3: SQLite is the source of truth

The database is authoritative for:

- Requests.
- Attempts.
- Reservations.
- Usage windows.
- Price snapshots.
- Account events.

In-memory state may cache database state, but it must be invalidated or updated after every lifecycle change.

## Rule 4: Do not store user content

Do not persist:

- Prompt text.
- Response text.
- Tool arguments.
- Request bodies.
- API keys.
- Authorization headers.

## Rule 5: No upstream call without committed accounting

Before any request reaches OpenCode Go, the following rows must already be committed:

- Pending request.
- Active reservation.
- Current attempt.

---

# Section 1: Correct pricing units and schema

## Goal

Repair the incorrect pricing conversion and add cache pricing support.

## Files

```text
src/go_aggregator/db/schema/0006_correct_price_microdollars.sql
src/go_aggregator/db/schema/0007_price_cache_rates.sql
src/go_aggregator/catalog/pricing.py
src/go_aggregator/db/repositories.py
tests/integration/test_price_migrations.py
tests/unit/test_pricing.py
```

## Task 1.1: Add corrective migration `0006`

Create:

```text
src/go_aggregator/db/schema/0006_correct_price_microdollars.sql
```

The source unit is:

```text
dollars per 1,000 tokens
```

The destination unit is:

```text
microdollars per 1,000,000 tokens
```

Correct formula:

```text
microdollars_per_million = dollars_per_thousand * 1_000_000_000
```

Required SQL behavior:

```sql
UPDATE model_price_snapshots
SET input_per_million_microdollars =
        CAST(input_price_per_1k * 1000000000 AS INTEGER)
WHERE input_price_per_1k IS NOT NULL;

UPDATE model_price_snapshots
SET output_per_million_microdollars =
        CAST(output_price_per_1k * 1000000000 AS INTEGER)
WHERE output_price_per_1k IS NOT NULL;
```

Do not condition this only on integer columns being null. Existing rows may contain the bad values from migration `0005` and must be overwritten from the legacy float columns.

## Task 1.2: Add cache-rate migration `0007`

Create:

```text
src/go_aggregator/db/schema/0007_price_cache_rates.sql
```

Add:

```sql
ALTER TABLE model_price_snapshots
    ADD COLUMN cache_read_per_million_microdollars INTEGER;

ALTER TABLE model_price_snapshots
    ADD COLUMN cache_write_per_million_microdollars INTEGER;
```

## Task 1.3: Expand `PriceSnapshot`

In `src/go_aggregator/catalog/pricing.py`, add:

```python
cache_read_per_million_microdollars: int | None = None
cache_write_per_million_microdollars: int | None = None
```

Update all `SELECT` statements and constructors.

## Task 1.4: Expand `CostCalculator.calculate_cost`

Change the signature to:

```python
async def calculate_cost(
    self,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> tuple[int, str]:
```

Use one numerator before division:

```python
total_numerator = (
    input_tokens * input_rate
    + output_tokens * output_rate
    + cache_read_tokens * cache_read_rate
    + cache_write_tokens * cache_write_rate
)
cost_microdollars = total_numerator // 1_000_000
```

Rules:

- Missing cache rates count as zero only when cache token count is zero.
- If cache tokens are nonzero but the corresponding rate is missing, return an estimated result rather than claiming a derived result.
- Built-in fallback prices must return exactness `estimated`.
- Snapshot-based complete prices return exactness `derived`.

## Task 1.5: Update repositories

Update `PriceSnapshotRepository` and `PriceRepository` to read and write the new fields.

Avoid duplicate price repository implementations if possible. If both remain, make them use the same schema and units.

## Required tests

Add exact migration assertions:

```text
0.003 dollars/1K -> 3_000_000 microdollars/1M
3.0 dollars/1K   -> 3_000_000_000 microdollars/1M
15.0 dollars/1K  -> 15_000_000_000 microdollars/1M
```

Add cost tests:

```text
1,000 input tokens at 3,000,000 microdollars/1M -> 3,000 microdollars
1,000 output tokens at 15,000,000 microdollars/1M -> 15,000 microdollars
cache-read and cache-write values contribute correctly
missing cache rate with nonzero cache usage returns estimated
```

## Section 1 acceptance criteria

- Migration from database version `0005` produces correct integer values.
- Cache rate columns exist.
- Cost calculations are dimensionally correct.
- No existing migration file was edited.

---

# Section 2: Replace quota capacity wiring

## Goal

Make the deployed application use actual five-hour, seven-day, and thirty-day quota capacities.

## Files

```text
src/go_aggregator/quota/estimation.py
src/go_aggregator/quota/scorer.py
src/go_aggregator/routing/router.py
src/go_aggregator/app.py
tests/unit/test_routing.py
tests/integration/test_startup_lifecycle.py
```

## Task 2.1: Rename capacity fields

In `AccountQuota`, replace:

```text
max_hourly_cost_microdollars
max_daily_cost_microdollars
max_monthly_cost_microdollars
```

with:

```text
capacity_5h_microdollars
capacity_7d_microdollars
capacity_30d_microdollars
```

Do not preserve the old names in scoring code.

## Task 2.2: Replace generic offset remnants

Keep only:

```text
offset_5h_microdollars
offset_7d_microdollars
offset_30d_microdollars
```

Remove the old generic `manual_offset` from routing calculations.

It may remain temporarily for unrelated backwards compatibility, but scorer code must not read it.

## Task 2.3: Add one explicit policy setter

Add to `QuotaEstimator`:

```python
def configure_account_policy(
    self,
    account_name: str,
    *,
    weight: float,
    capacity_5h_microdollars: int,
    capacity_7d_microdollars: int,
    capacity_30d_microdollars: int,
    offset_5h_microdollars: int,
    offset_7d_microdollars: int,
    offset_30d_microdollars: int,
) -> None:
```

This method must create the account quota if missing and set all seven values.

## Task 2.4: Wire policy during startup

In `app.py`, immediately after persisted windows are loaded, loop over configured accounts:

```python
for acct in config.accounts:
    router.configure_account_policy(
        account_name=acct.name,
        weight=acct.weight,
        capacity_5h_microdollars=(
            config.limits.five_hour_microdollars * acct.weight
        ),
        capacity_7d_microdollars=(
            config.limits.weekly_microdollars * acct.weight
        ),
        capacity_30d_microdollars=(
            config.limits.monthly_microdollars * acct.weight
        ),
        offset_5h_microdollars=acct.five_hour_offset_microdollars,
        offset_7d_microdollars=acct.weekly_offset_microdollars,
        offset_30d_microdollars=acct.monthly_offset_microdollars,
    )
```

Convert weighted capacities to integers explicitly:

```python
int(config.limits.five_hour_microdollars * acct.weight)
```

## Task 2.5: Include projected request estimate in scoring

Change scorer input so it receives:

```text
request_estimates: dict[account_name, estimated_microdollars]
```

For each window:

```text
used + offset + active_reserved + request_estimate
```

Do not score only the current usage before the new request.

## Task 2.6: Remove utilization clamp

Replace:

```python
return min(total / max_cost, 1.0)
```

with:

```python
return total / max_cost
```

## Task 2.7: Active request count

When a reservation is created, increment the chosen account's `active_request_count`.

When the request finalizes, decrement it exactly once.

Never allow the count to become negative.

## Required tests

Test all of these through real application startup, not only direct scorer construction:

1. Five-hour usage affects selection.
2. Seven-day usage affects selection.
3. Thirty-day usage affects selection.
4. Account weight `2.0` doubles all capacities.
5. Offset 5h does not affect 7d or 30d.
6. Active reservation affects the next request.
7. Incoming request estimate affects projected score.
8. 150% utilization scores higher than 110% utilization.
9. Active request count increases and returns to zero.

## Section 2 acceptance criteria

- Startup configures all three capacities.
- Production routing does not receive `None` capacities.
- Projected request cost is part of selection.
- Utilization above 1.0 remains visible.

---

# Section 3: Add database transactions and proxy request identity

## Goal

Create a durable transaction API and store the public proxy UUID.

## Files

```text
src/go_aggregator/db/schema/0008_proxy_request_identity.sql
src/go_aggregator/db/connection.py
src/go_aggregator/db/repositories.py
tests/integration/test_database_transactions.py
tests/integration/test_persistence_health.py
```

## Task 3.1: Add migration `0008`

Add:

```sql
ALTER TABLE requests ADD COLUMN proxy_request_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_proxy_request_id
    ON requests(proxy_request_id);
```

Then backfill existing rows:

```sql
UPDATE requests
SET proxy_request_id = 'legacy-' || id
WHERE proxy_request_id IS NULL;
```

SQLite cannot easily add `NOT NULL` after the fact. Enforce non-null in repository code for all new requests.

## Task 3.2: Store UUID in `create_pending`

Update the SQL insert to include `proxy_request_id`.

The method argument `request_id` must be stored, not ignored.

## Task 3.3: Add transaction context manager

In `db/connection.py`, add:

```python
@asynccontextmanager
async def transaction(self):
    await self.connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        await self.connection.rollback()
        raise
    else:
        await self.connection.commit()
```

Rules:

- Use `BEGIN IMMEDIATE` to serialize writers predictably.
- Do not call repository-level commits inside this context.
- If nested transaction support does not exist, detect nesting and raise a clear error.

## Task 3.4: Repository methods must not auto-commit

Audit repository methods used during selection and finalization.

They should execute SQL only. The coordinator/finalizer owns commit boundaries.

## Required tests

1. Transaction commits on success.
2. Transaction rolls back all changes on exception.
3. The proxy UUID is stored exactly.
4. Duplicate proxy UUID fails.
5. No partial reservation remains after transaction rollback.

## Section 3 acceptance criteria

- `x-proxy-request-id` can be queried directly in `requests.proxy_request_id`.
- Selection code can commit all rows in one transaction.
- Rollback is verified in tests.

---

# Section 4: Make pre-dispatch selection atomic and durable

## Goal

No upstream request may be sent before request, reservation, and attempt records are committed.

## Files

```text
src/go_aggregator/request/coordinator.py
src/go_aggregator/request/models.py or coordinator.py dataclasses
src/go_aggregator/routing/router.py
src/go_aggregator/db/repositories.py
tests/integration/test_atomic_selection.py
```

## Task 4.1: Add `SelectedAttempt`

Define:

```python
@dataclass(frozen=True)
class SelectedAttempt:
    proxy_request_id: str
    db_request_id: str
    attempt_id: int
    reservation_id: str
    account_id: int
    account_name: str
    api_key: str
    estimated_tokens: int
    estimated_microdollars: int
    attempt_number: int
```

## Task 4.2: Add attempted-account exclusions

Add to request context:

```python
attempted_accounts: set[str] = field(default_factory=set)
```

Update router selection methods to accept:

```python
exclude_accounts: set[str] | None = None
```

Excluded accounts must be removed before scoring.

## Task 4.3: Replace `_attempt_request` selection block

Create one method:

```python
async def _select_and_persist_attempt(
    self,
    context: ProxyRequestContext,
    attempt_number: int,
) -> SelectedAttempt:
```

Required implementation order:

```text
acquire _select_lock
enter db.transaction()
create pending request if first attempt
load candidate accounts excluding attempted_accounts
calculate per-account request estimate
select account
resolve account ID and API key
create reservation
create attempt row
update request account/reserved amount/retry count
increment runtime active count
add exact reserved amount to in-memory reservation cache
commit transaction
release _select_lock
return SelectedAttempt
```

Attempt creation must be inside the lock and transaction.

## Task 4.4: Commit before network call

The very next operation after `_select_and_persist_attempt()` returns may be the upstream HTTP call.

Do not call upstream before this method returns.

## Task 4.5: Use exact reservation amount

Never recalculate the reservation estimate during cleanup.

Use:

```python
selected.estimated_microdollars
```

for both add and remove operations.

## Task 4.6: Failure before upstream call

If selection transaction raises:

- no upstream request is made;
- no active reservation remains;
- no attempt row remains unless the entire transaction committed;
- active request count is unchanged.

## Required tests

### Concurrent test

Use at least 20 concurrent requests and two equal accounts.

Instrument the mock upstream so all requests remain open long enough for reservations to influence later selections.

Assert:

- Both accounts receive traffic.
- The first account does not receive all 20 requests.
- Active reservations exist before upstream completion.

### Durability test

Inside the upstream mock, open a second SQLite connection and verify:

- Pending request exists.
- Reservation is active.
- Attempt exists.

This proves the rows were committed before the upstream call.

### Rollback test

Force attempt insertion to fail.

Assert:

- No upstream request occurred.
- No request/reservation rows remain from the failed transaction.

## Section 4 acceptance criteria

- Selection and reservation observe one coherent state.
- Attempt creation is in the same committed unit.
- Upstream dispatch only happens after commit.

---

# Section 5: Add one idempotent finalizer

## Goal

Every terminal path must finalize request, attempt, reservation, in-memory state, and usage exactly once.

## Files

```text
src/go_aggregator/request/finalizer.py
src/go_aggregator/request/coordinator.py
src/go_aggregator/db/repositories.py
src/go_aggregator/accounts/state.py
tests/integration/test_finalization_matrix.py
```

## Task 5.1: Add terminal outcome enum

Create:

```python
class FinalizationOutcome(StrEnum):
    COMPLETED = "completed"
    CLIENT_ERROR = "client_error"
    UPSTREAM_ERROR = "upstream_error"
    MIDSTREAM_ERROR = "midstream_error"
    CLIENT_CANCELLED = "client_cancelled"
    TIMEOUT = "timeout"
    INTERRUPTED = "interrupted"
```

## Task 5.2: Add finalization input model

```python
@dataclass
class FinalizationData:
    outcome: FinalizationOutcome
    status_code: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    thinking_characters: int = 0
    first_byte_ms: int | None = None
    bytes_emitted: int = 0
    upstream_request_id: str | None = None
    error_class: str | None = None
    error_detail: str | None = None
```

## Task 5.3: Add `RequestFinalizer`

The finalizer must receive:

- Database.
- Request repository.
- Attempt repository.
- Reservation repository.
- Cost calculator.
- Quota estimator/router.
- Registry/runtime state.
- Health manager.

## Task 5.4: Idempotent request update

Add repository method:

```python
async def finalize_if_pending(...) -> bool:
```

SQL must include:

```sql
WHERE id = ? AND status = 'pending'
```

Return whether one row was updated.

If the row was already terminal, finalizer returns without applying state changes again.

## Task 5.5: Finalization transaction

Inside one `db.transaction()`:

1. Calculate exact/derived/estimated cost.
2. If no usable usage exists, use `selected.estimated_microdollars` and exactness `estimated`.
3. Finalize request only if pending.
4. Finalize attempt.
5. Release active reservation.
6. Insert account event for significant failures if needed.
7. Commit.

After commit, and only when this call performed the transition:

1. Remove exactly `selected.estimated_microdollars` from in-memory reservations.
2. Decrement active request count.
3. Add final cost to live quota state.
4. Update EWMA from exact or derived observations.
5. Update health state.

## Task 5.6: Pass-through 4xx must finalize

Before returning a non-retryable upstream 4xx:

```text
finalize outcome CLIENT_ERROR
release reservation
complete attempt
persist status/body metadata only
return raw upstream body
```

Do not penalize account health for client-generated 400-series errors unless error semantics explicitly identify account authentication, quota, rate limiting, or model availability.

## Task 5.7: Remove scattered cleanup

After `RequestFinalizer` is used, delete duplicate cleanup from:

- Non-streaming success path.
- Streaming success path.
- Exception branches.
- Outer retry loop.

Each path should call the finalizer once with the correct outcome.

## Required tests

Build a table-driven test for:

```text
200 success
400 client error
401 authentication failure
402 quota exhausted
404 model unavailable
429 rate limit
500 upstream failure
connect timeout
midstream reset
client cancellation
```

For every terminal request, assert:

- Request is not pending.
- Reservation is not active.
- Attempt is completed.
- Active request count is zero.
- In-memory reserved amount is zero.
- Finalizer called twice does not change totals.

## Section 5 acceptance criteria

- No terminal path leaks a reservation.
- Finalization is exactly-once.
- Pass-through errors are lifecycle-complete.

---

# Section 6: Correct failover and health behavior

## Goal

Use another subscription when the current account fails before downstream body commitment.

## Files

```text
src/go_aggregator/request/coordinator.py
src/go_aggregator/routing/router.py
src/go_aggregator/health/health_manager.py
src/go_aggregator/retries/classifier.py or equivalent
tests/integration/test_failover_matrix.py
```

## Task 6.1: Use configured retry count

Replace hard-coded `MAX_RETRY_ATTEMPTS` with configuration.

Use internal value:

```python
max_attempts = 1 + config.routing.max_retries_before_stream
```

Inject this into coordinator construction.

## Task 6.2: Exclude attempted accounts

Immediately after a durable selection succeeds:

```python
context.attempted_accounts.add(selected.account_name)
```

Every later selection excludes that set.

Do not retry an account already attempted for the same request.

## Task 6.3: Retry these errors when another account exists

Retry before response commitment:

- 401 authentication failure.
- 402 quota/balance exhaustion.
- 404 only when classified as account-specific model unavailability.
- 429 rate limit.
- Connect failure.
- Connect timeout.
- Read timeout before body emission.
- Selected 5xx responses.

Do not retry:

- Client 400 validation errors.
- Cancellation.
- Midstream failure after bytes emitted.
- Internal persistence failure.

## Task 6.4: Apply exact health transitions

### 401

```text
health_state = authentication_failed
is_healthy = false
no automatic restore during same process
```

### 402

```text
health_state = quota_exhausted
cooldown_until = configured or default interval
```

### 429

```text
health_state = rate_limited
cooldown_until = parsed Retry-After
```

### Account/model 404

```text
disable only model_id for account_name
trigger targeted catalog refresh asynchronously
```

### Transport/5xx

```text
record circuit breaker failure
apply cooldown only when threshold opens breaker
```

### Success

```text
reset transient failures
preserve permanent authentication failure rules
```

## Task 6.5: Preserve final upstream error

Track the last useful upstream response:

```text
status code
filtered headers
bounded raw body
```

If failover exhausts all accounts, return that response rather than:

```json
{"error": "stringified exception"}
```

Use proxy-generated error envelopes only when no upstream response exists.

## Required tests

1. Account A returns 401, B returns 200.
2. Account A returns 402, B returns 200.
3. Account A returns model-specific 404, B returns 200.
4. Account A returns 429, B returns 200.
5. Account A connect fails, B returns 200.
6. Account A returns 500, B returns 200.
7. Same account is never attempted twice.
8. Client 400 does not retry.
9. Midstream failure does not retry.
10. Exhausted retries return final upstream status/body.

## Section 6 acceptance criteria

- Multi-subscription failover works for account-specific failures.
- Attempt history reflects every selected account.
- Failed account is excluded from the current request.

---

# Section 7: Integrate and repair SSE observation

## Goal

Extract streaming usage correctly across arbitrary HTTP chunk boundaries while forwarding exact bytes.

## Files

```text
src/go_aggregator/proxy/sse_observer.py
src/go_aggregator/request/coordinator.py
tests/unit/test_sse_observer.py
tests/integration/test_streaming_edge_cases.py
```

## Task 7.1: Use incremental UTF-8 decoder

In `IncrementalSSEObserver`, use:

```python
codecs.getincrementaldecoder("utf-8")()
```

Do not decode each chunk independently with `errors="replace"`.

## Task 7.2: Parse complete SSE events

Maintain:

```text
current event name
current list of data lines
text buffer
```

Processing rules:

- Normalize CRLF and lone CR to LF.
- A blank line terminates one SSE event.
- Multiple `data:` lines are joined with `\n`.
- Accept both `data:value` and `data: value`.
- Ignore comments beginning with `:`.
- Ignore unknown fields.
- Process JSON only when an event terminates.
- `[DONE]` is terminal but not an error.

## Task 7.3: Bound memory

If one incomplete event exceeds `MAX_INCOMPLETE_FRAME_BYTES`:

- Increment observer error count.
- Discard telemetry buffer for that event.
- Continue forwarding bytes.
- Do not raise into the downstream stream.

## Task 7.4: Integrate observer into coordinator

Replace per-chunk line parsing with:

```python
observer = IncrementalSSEObserver(context.protocol)

async for chunk in response.aiter_bytes():
    observer.observe(chunk)
    yield chunk

observer.flush()
usage = observer.usage
```

Do not maintain a second usage extractor in the stream generator.

## Task 7.5: Explicit cancellation branch

Add before `except Exception`:

```python
except asyncio.CancelledError:
    await finalizer.finalize(
        selected,
        FinalizationData(
            outcome=FinalizationOutcome.CLIENT_CANCELLED,
            bytes_emitted=observer.bytes_emitted,
            ...observer usage fields...
        ),
    )
    raise
```

Do not mark account unhealthy for client cancellation.

## Task 7.6: Midstream error branch

On other exceptions after any bytes were emitted:

- Outcome `MIDSTREAM_ERROR`.
- No retry.
- Preserve partial usage.
- Record emitted bytes.
- Mark account failure only when error is attributable to upstream.

## Required tests

Generate one known SSE sequence and split it:

- At every byte boundary.
- Inside JSON strings.
- Inside multibyte UTF-8 characters.
- Between `data:` and payload.
- Between `\r` and `\n`.
- Across blank-line event boundaries.

Assert:

- Downstream bytes equal upstream bytes exactly.
- Usage result is identical for every split.
- Cancellation leaves no active reservation.
- Midstream reset produces one attempt only.

## Section 7 acceptance criteria

- `IncrementalSSEObserver` is used by the coordinator.
- Usage parsing survives arbitrary chunking.
- Cancellation finalizes correctly.

---

# Section 8: Feed final usage into live routing

## Goal

The next request must immediately observe the previous request's final cost.

## Files

```text
src/go_aggregator/request/finalizer.py
src/go_aggregator/quota/estimation.py
src/go_aggregator/catalog/estimator.py
src/go_aggregator/app.py
tests/integration/test_live_routing_feedback.py
```

## Task 8.1: Update live quota state after commit

After successful finalization transaction:

```python
quota_estimator.record_usage(
    account_name,
    tokens=input_tokens + output_tokens,
    cost_microdollars=final_cost,
    model_id=model_id,
)
```

Use final cost, not zero.

## Task 8.2: Refresh persisted snapshot locally

Either:

- Increment the account's cached 5h/7d/30d values by final cost; or
- Invalidate the account snapshot and reload it before the next selection.

Do not wait 60 seconds.

The periodic refresh may remain as reconciliation.

## Task 8.3: Update EWMA

Use exact or derived final observations.

Do not learn from unknown usage.

For estimated observations, either skip EWMA update or use a clearly reduced weight.

## Task 8.4: Remove duplicate estimator classes if possible

There is both quota EWMA state and `EWMACostEstimator` in the catalog package.

Choose one as canonical for request-cost estimates.

Minimum acceptable outcome:

- Coordinator uses one estimator.
- Startup hydrates that estimator.
- Finalizer updates that same estimator.

## Required tests

1. Request 1 completes with high cost on account A.
2. Request 2 begins immediately without waiting for background refresh.
3. Request 2 selects account B due to Request 1 cost.
4. Restart preserves learned or persisted behavior.

## Section 8 acceptance criteria

- Final cost affects immediate next selection.
- Reservation removal uses exact original value.
- No 60-second stale-routing window remains.

---

# Section 9: Persist all telemetry fields

## Goal

Populate the existing schema consistently.

## Files

```text
src/go_aggregator/request/finalizer.py
src/go_aggregator/db/repositories.py
src/go_aggregator/stats/queries.py
tests/integration/test_request_telemetry.py
```

## Task 9.1: Persist fields

Ensure finalizer passes:

```text
input_tokens
output_tokens
cache_read_tokens
cache_write_tokens
reasoning_tokens
thinking_characters
cost_microdollars
exactness
first_byte_ms
retry_count
upstream_request_id
status_code
error_class
error_detail
```

## Task 9.2: Attempt count semantics

Store:

```text
retry_count = total_attempts - 1
```

Return response header:

```text
x-proxy-attempt-count = total_attempts
```

## Task 9.3: Error detail bounds

Truncate persisted `error_detail` to a configured safe maximum, such as 2,048 characters.

Never persist raw response bodies as error detail.

## Required tests

Use one fixture containing cache and reasoning usage.

Assert every database column receives the expected value.

## Section 9 acceptance criteria

- Dashboard queries can rely on populated fields.
- First-byte and retry metrics are real, not always zero.

---

# Section 10: Preserve raw non-streaming responses

## Goal

Stop decoding and reserializing upstream response bodies.

## Files

```text
src/go_aggregator/api/chat_completions.py
src/go_aggregator/api/messages.py
src/go_aggregator/request/coordinator.py
tests/integration/test_response_transparency.py
```

## Task 10.1: Return raw `Response`

Change endpoint return types to include `Response`.

For non-streaming prepared responses:

```python
return Response(
    content=result.body,
    status_code=result.status_code,
    headers=result.headers,
    media_type=None,
)
```

Do not call `json.loads()`.

Let the upstream `content-type` header pass through.

## Task 10.2: Preserve status and headers

Ensure filtered upstream headers are retained.

Do not overwrite content type with JSON unless the error is generated by the proxy.

## Required tests

1. JSON whitespace is byte-identical.
2. Non-JSON text error body is preserved.
3. Binary body is preserved.
4. Charset content type is preserved.
5. Proxy-generated validation errors still use protocol-specific JSON.

## Section 10 acceptance criteria

- Non-streaming body bytes are transparent.
- Non-JSON upstream errors do not crash rendering.

---

# Section 11: Add per-model protocol resolution

## Goal

Support a catalog containing both OpenAI-compatible and Anthropic-compatible models.

## Files

```text
src/go_aggregator/catalog/protocols.py
src/go_aggregator/catalog/normalizer.py
src/go_aggregator/catalog/service.py
src/go_aggregator/db/schema/0009_model_protocol_source.sql
src/go_aggregator/db/repositories.py
tests/unit/test_protocol_resolution.py
tests/integration/test_catalog_mixed_protocol.py
```

## Task 11.1: Add migration `0009`

Add model columns:

```sql
ALTER TABLE models ADD COLUMN protocol_source TEXT;
```

Optional:

```sql
ALTER TABLE models ADD COLUMN endpoint_path TEXT;
```

## Task 11.2: Add `ModelProtocolResolver`

Resolution order must be exactly:

1. Explicit TOML override.
2. Explicit per-model metadata from upstream.
3. Exact known-model mapping.
4. Known family mapping.
5. Previously persisted protocol.
6. Unresolved error.

Do not use top-level response shape as the final protocol for every model.

## Task 11.3: Persist resolution source

Examples:

```text
config_override
upstream_metadata
exact_mapping
family_mapping
persisted
```

## Task 11.4: Validate local endpoint

When a model resolved as Anthropic is requested through `/chat/completions`, return a protocol-compatible 400 telling the client to use `/messages`.

Likewise reject OpenAI models through `/messages`.

Do not automatically translate protocols in this phase.

## Required tests

1. One catalog response produces both protocol families.
2. Explicit override wins.
3. Persisted value is used only after stronger sources fail.
4. Unresolved model is not silently guessed.
5. Wrong endpoint returns 400.

## Section 11 acceptance criteria

- Mixed protocol catalog is supported.
- Protocol source is observable.

---

# Section 12: Enforce bounded request bodies and real readiness

## Goal

Make middleware and readiness claims accurate.

## Files

```text
src/go_aggregator/app.py
src/go_aggregator/request/body.py or app.py middleware
src/go_aggregator/routing/router.py
tests/integration/test_request_limits.py
tests/integration/test_startup_lifecycle.py
```

## Task 12.1: Replace content-length-only body middleware

Preferred approach: add helper used by both endpoints:

```python
async def read_body_limited(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > max_bytes:
        raise RequestTooLargeError

    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise RequestTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)
```

Replace `await request.body()` in both model endpoints.

The existing middleware may remain as an early rejection optimization, but it is not sufficient alone.

## Task 12.2: Real eligible-pairing readiness

Add router method:

```python
def has_eligible_pairing(self) -> bool:
```

It must verify at least one combination where:

- Account enabled.
- Credential loaded.
- Account healthy.
- Model available to account.
- Model protocol resolved.
- Account not excluded by quota policy.

Use this method in `/readyz`.

## Task 12.3: Real writeability probe

Implement:

```text
SAVEPOINT readiness_probe
CREATE TEMP TABLE if needed or write to dedicated probe table
ROLLBACK TO readiness_probe
RELEASE readiness_probe
```

Simpler acceptable implementation:

- Create a dedicated `health_probe` table in a migration.
- Begin transaction.
- Insert one row.
- Roll back.

Do not call `SELECT 1` a writeability test.

## Required tests

1. Oversized request with `Content-Length` is rejected.
2. Oversized chunked request without `Content-Length` is rejected before full buffering.
3. No eligible model/account pair produces readiness 503.
4. Read-only database produces readiness 503.
5. Healthy usable configuration produces 200.

## Section 12 acceptance criteria

- Request memory is bounded.
- Readiness reflects actual service usability.

---

# Section 13: Final integration matrix

This section is mandatory.

Create:

```text
tests/integration/test_phase12_end_to_end.py
```

Use two accounts and at least one model for each protocol family.

## Test A: Atomic durability

1. Submit request.
2. During upstream mock execution, inspect SQLite from another connection.
3. Assert committed pending request, reservation, and attempt exist.

## Test B: Quota balancing

1. Seed account A with high 5h cost.
2. Verify B selected.
3. Seed B with high 7d cost.
4. Verify score changes.
5. Seed A with high 30d cost.
6. Verify score changes.

## Test C: Concurrent reservations

1. Launch 20 concurrent long-running requests.
2. Verify both accounts receive requests.
3. Verify active reservation totals match in-flight requests.
4. Complete all requests.
5. Verify all active reservations are zero.

## Test D: Failover

For each first-account failure:

```text
401
402
404 model unavailable
429
connect error
500
```

verify second account succeeds and exactly two attempts are stored.

## Test E: Pass-through client error

1. Upstream returns 400 with non-JSON body.
2. Verify raw status/body returned.
3. Verify request terminal.
4. Verify reservation released.
5. Verify account health unchanged.

## Test F: Streaming fragmentation

1. Return SSE containing terminal usage.
2. Split every byte independently.
3. Verify exact downstream bytes.
4. Verify usage persisted.

## Test G: Cancellation

1. Begin stream.
2. Consume one chunk.
3. Cancel client task.
4. Verify terminal cancelled state.
5. Verify reservation released.
6. Verify account not penalized.

## Test H: Midstream failure

1. Emit one chunk.
2. Raise upstream protocol error.
3. Verify no retry.
4. Verify one attempt.
5. Verify partial/estimated accounting.

## Test I: Immediate routing feedback

1. Complete expensive request on A.
2. Immediately submit another request.
3. Verify B selected without waiting for background refresh.

## Test J: Restart recovery

1. Commit a pending request/reservation/attempt.
2. Simulate process death before finalization.
3. Run startup recovery.
4. Verify interrupted terminal state.
5. Verify reservation released.
6. Verify recovery event exists.

## Test K: Mixed protocols

1. Refresh mixed catalog.
2. Send OpenAI model through `/chat/completions`.
3. Send Anthropic model through `/messages`.
4. Verify wrong endpoints return 400.

## Test L: Privacy

Use known markers in prompt, response, API key, and Authorization header.

Search:

- All database text columns.
- Captured logs.

Assert none of the markers appear.

---

# Section 14: Required cleanup after tests pass

Only perform this cleanup after all sections pass.

## Remove dead code

Remove or clearly deprecate:

- Unused in-memory `ReservationManager` if SQLite reservations are canonical.
- Unused old generic manual offset logic.
- Per-chunk streaming usage parsing.
- Duplicate cost estimators that are no longer canonical.
- Unused runtime reload scaffolding if it remains unsupported and confusing.

## Update documentation

Update README to state:

- Configuration reload requires restart.
- Usage is proxy-observed.
- Weekly/monthly windows are rolling approximations unless OpenCode exposes authoritative resets.
- Mixed protocol endpoints are both required.
- Dashboard is unauthenticated by default and intended for trusted LAN use.

## Add CI

If no GitHub Actions workflow currently runs tests, add one.

Minimum jobs:

```text
ruff format --check
ruff check
pyright or mypy
pytest
```

Run on Python 3.12.

---

# File-by-file implementation checklist

## `src/go_aggregator/app.py`

- [ ] Configure explicit 5h/7d/30d capacities.
- [ ] Inject configured retry count into coordinator.
- [ ] Use real eligible-pairing readiness.
- [ ] Use real writeability probe.
- [ ] Keep periodic usage refresh only as reconciliation.

## `src/go_aggregator/request/coordinator.py`

- [ ] Use `SelectedAttempt`.
- [ ] Exclude attempted accounts.
- [ ] Call durable selection transaction before network I/O.
- [ ] Route all terminal outcomes through finalizer.
- [ ] Do not directly release reservations.
- [ ] Do not directly decrement active counts.
- [ ] Integrate SSE observer.
- [ ] Handle `CancelledError` explicitly.
- [ ] Preserve final upstream error body.

## `src/go_aggregator/request/finalizer.py`

- [ ] Idempotent request transition.
- [ ] Attempt completion.
- [ ] Reservation release.
- [ ] Cost calculation including cache.
- [ ] Live quota feedback.
- [ ] EWMA update.
- [ ] Health update.
- [ ] Exact original reservation removal.

## `src/go_aggregator/quota/estimation.py`

- [ ] Explicit capacities.
- [ ] Explicit offsets.
- [ ] Request estimate in projected score.
- [ ] No generic offset in scorer path.
- [ ] Immediate final-cost update.

## `src/go_aggregator/quota/scorer.py`

- [ ] No clamp at 1.0.
- [ ] Uses active reserved cost.
- [ ] Uses incoming request estimate.
- [ ] Uses active request penalty.

## `src/go_aggregator/db/connection.py`

- [ ] Add transaction context manager.
- [ ] Rollback on `BaseException`.

## `src/go_aggregator/db/repositories.py`

- [ ] Store proxy UUID.
- [ ] Add conditional terminal update.
- [ ] Add coherent usage snapshot query if needed.
- [ ] Avoid internal commits in shared transactions.

## `src/go_aggregator/proxy/sse_observer.py`

- [ ] Incremental UTF-8 decoder.
- [ ] Complete SSE event assembly.
- [ ] Multiple data lines.
- [ ] CRLF/LF support.
- [ ] Memory bound.

## `src/go_aggregator/catalog/pricing.py`

- [ ] Correct units.
- [ ] Cache rates.
- [ ] Integer arithmetic.

## `src/go_aggregator/catalog/normalizer.py`

- [ ] Stop assigning one protocol from top-level response shape.

## `src/go_aggregator/catalog/protocols.py`

- [ ] Implement resolution priority exactly as specified.

## API endpoint files

- [ ] Use bounded body reader.
- [ ] Return raw `Response` for non-streaming upstream output.
- [ ] Keep protocol-specific proxy-generated errors.

---

# Definition of done

Phase 12 is complete only when all of these statements are true:

1. Real application startup configures three quota capacities for every account.
2. Account selection uses observed usage, offsets, active reservations, incoming request estimate, active request count, and health.
3. Utilization greater than one is preserved.
4. Request, reservation, and attempt rows are committed before upstream dispatch.
5. The proxy UUID is stored and queryable.
6. Every terminal path releases its reservation exactly once.
7. Pass-through 4xx responses finalize correctly.
8. Client cancellation finalizes correctly.
9. Failover uses another account for 401/402/model-404/429/transport/5xx when possible.
10. No account is attempted twice for one request.
11. SSE usage parsing works across arbitrary byte boundaries.
12. Price conversion is dimensionally correct.
13. Cache token costs are represented.
14. Final cost immediately affects the next routing decision.
15. First-byte, retries, cache usage, reasoning usage, and upstream request ID are persisted.
16. Non-streaming response bodies are byte-transparent.
17. Mixed protocol models are resolved individually.
18. Chunked oversized requests are bounded.
19. Readiness verifies a real usable pairing and actual database writeability.
20. Full end-to-end tests pass.
21. No user content or secrets are persisted.
22. CI runs formatting, linting, type checking, and tests.
